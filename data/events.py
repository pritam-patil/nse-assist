"""The dividend event table — one row per payout, from the research cache.

    python -m data.events

Reads every per-symbol parquet under data/cache/, extracts the dividend events,
and writes data/events.parquet: symbol, ex-date, amount, the close of the session
before ex-date, the yield that implies, and 60-session liquidity context. This is
the input burst 2 onward queries; nothing here touches the trading pipeline.

ONE BASIS — AND YAHOO ALREADY CHOSE IT

Everything the cache holds is split-adjusted to the current share basis AT THE
SOURCE: closes, volumes, and dividend amounts alike. Verified against
TATASTEEL's 2022 1:10 split — the June 2022 closes read ~99 (the exchange
printed ~990), volumes read ~80M (~8M traded), and the 51-rupee payout reads
5.1, with no cliff at the split date. Adj Close differs from Close only by
dividend adjustment. So amount-over-prev-close is already a consistent yield and
NO adjustment happens here.

The first version of this module did not believe that. It divided closes by the
product of later split ratios — a second time — and understated every pre-split
yield by exactly the split factor, surfacing as TATASTEEL "yielding" 51% on a
close of 9.96. The regression test pins that shape so the double-adjustment
cannot come back.

adj_close is deliberately not used for yields either way: it folds the dividend
itself into the price, and a yield computed on a dividend-adjusted price is
circular.

THE VALIDATION EVENTS ARE THE POINT, NOT AN AFTERTHOUGHT

params.yaml pins three payouts transcribed from announcements — amounts and
ex-dates as reported, not as any feed served them. build-then-validate asserts
the feed agrees with the record on all three before the table is trusted at all.
A miss fails the run loudly: if Yahoo is wrong about an event we can check, the
6,000 we cannot check inherit the doubt.
"""

import argparse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

from data import fetch

PARAMS_PATH = Path(__file__).resolve().parent.parent / "params.yaml"
EVENTS_PATH = Path(__file__).resolve().parent / "events.parquet"

PRIOR_SESSIONS = 60

# Validation compares a transcribed rupee amount against a float that has been
# through Yahoo's pipeline; half a paisa separates rounding from disagreement.
AMOUNT_TOLERANCE = 0.005

# When an expected ex-date misses, nearby events make the failure diagnosable —
# "found 25.0 on 2025-08-05" points at a date-convention gap, silence points
# nowhere.
NEARBY_DAYS = 5

COLUMNS = ("symbol", "ex_date", "amount", "prev_date", "prev_close", "ex_close",
           "yield_pct", "avg_volume_60d", "avg_price_60d", "prior_sessions")


# --- extraction ---------------------------------------------------------------


def events_for(symbol, frame):
    """Event rows for one symbol's cached frame.

    prev_close is the last SESSION before ex-date, not the calendar day — that is
    the price a buyer paid to be on the register. An event on the first cached
    bar has no such session; the row stays (the event happened) with NaN price
    context, and prior_sessions says how much history the averages actually saw.

    Prices and volumes are used exactly as cached — see the module docstring on
    why adjusting them here would be adjusting them twice.
    """
    frame = frame.sort_values("date").reset_index(drop=True)
    rows = []
    for position in frame.index[frame["dividend"] != 0]:
        amount = float(frame.at[position, "dividend"])
        prior = frame.iloc[max(0, position - PRIOR_SESSIONS):position]
        prev_close = float(prior["close"].iloc[-1]) if len(prior) else float("nan")
        rows.append({
            "symbol": symbol,
            "ex_date": frame.at[position, "date"],
            "amount": amount,
            # The prior session's DATE rides along so a study aligning against an
            # index series can ask for the same two sessions, not "the day before".
            "prev_date": prior["date"].iloc[-1] if len(prior) else pd.NaT,
            "prev_close": prev_close,
            "ex_close": float(frame.at[position, "close"]),
            "yield_pct": amount / prev_close * 100 if prev_close == prev_close else float("nan"),
            "avg_volume_60d": float(prior["volume"].mean()) if len(prior) else float("nan"),
            "avg_price_60d": float(prior["close"].mean()) if len(prior) else float("nan"),
            "prior_sessions": len(prior),
        })
    return rows


def cached_symbols():
    """Symbols with a parquet in the cache.

    Underscore files are metadata; caret files (^NSEI) are indices that share the
    cache machinery but are not universe members — an index pays no dividend and
    must not sit in the coverage denominator pretending to be a stock.
    """
    return sorted(
        path.stem for path in fetch.CACHE_DIR.glob("*.parquet")
        if not path.stem.startswith(("_", "^"))
    )


def build_events(symbols=None):
    """The full event table, one row per (symbol, ex-date), oldest first."""
    rows = []
    for symbol in (symbols or cached_symbols()):
        cached = fetch.read_cache(symbol)
        if cached is None or cached.empty:
            continue
        rows.extend(events_for(symbol, cached))
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.DataFrame(rows, columns=list(COLUMNS))
    return frame.sort_values(["ex_date", "symbol"]).reset_index(drop=True)


# --- summary ------------------------------------------------------------------


def summarize(events, universe_size):
    """The printed sanity report: volume of events, shape of yields, coverage."""
    lines = [f"[events] {len(events)} dividend event(s) across "
             f"{events['symbol'].nunique()} symbol(s)"]
    if events.empty:
        return lines

    per_year = events.groupby(events["ex_date"].dt.year).size()
    lines.append("[events] per year: "
                 + ", ".join(f"{year}: {count}" for year, count in per_year.items()))

    yields = events["yield_pct"].dropna()
    if len(yields):
        quantiles = yields.quantile([0.25, 0.5, 0.75, 0.9])
        lines.append(
            f"[events] yield %: median {quantiles[0.5]:.2f}, "
            f"p25 {quantiles[0.25]:.2f}, p75 {quantiles[0.75]:.2f}, "
            f"p90 {quantiles[0.9]:.2f}, max {yields.max():.2f} "
            f"({events['yield_pct'].isna().sum()} event(s) without a prior close)"
        )

    covered = events["symbol"].nunique()
    lines.append(
        f"[events] coverage: {covered} of {universe_size} cached symbol(s) have at "
        f"least one event — the rest paid nothing in the window, which for a "
        f"dividend study is information, not absence."
    )
    return lines


# --- validation ---------------------------------------------------------------


def validation_events(path=None):
    params = yaml.safe_load(Path(path or PARAMS_PATH).read_text())
    return params.get("validation_events") or []


def validate(events, expected):
    """[{symbol, expected_date, expected_amount, ok, detail}] per pinned event.

    Matching is exact on date and paise-tolerant on amount. A near-miss is
    reported with what WAS found — a wrong amount on the right day and a right
    amount on a neighbouring day are different feed defects, and the detail
    string should say which one this is.
    """
    results = []
    for item in expected:
        symbol = item["symbol"]
        when = pd.Timestamp(item["ex_date"])
        amount = float(item["amount"])
        mine = events[events["symbol"] == symbol]

        ok, detail = False, ""
        if mine.empty:
            detail = "symbol not in the event table — is it cached?"
        else:
            exact = mine[mine["ex_date"] == when]
            if not exact.empty:
                found = float(exact["amount"].iloc[0])
                if abs(found - amount) <= AMOUNT_TOLERANCE:
                    ok, detail = True, f"found {found:.2f} on {when.date()}"
                else:
                    detail = f"date matches but amount is {found:.2f}, expected {amount:.2f}"
            else:
                window = mine[abs(mine["ex_date"] - when) <= timedelta(days=NEARBY_DAYS)]
                if not window.empty:
                    near = window.iloc[0]
                    detail = (f"no event on {when.date()}; nearest is {near['amount']:.2f} "
                              f"on {near['ex_date'].date()}")
                else:
                    detail = f"no event within {NEARBY_DAYS} day(s) of {when.date()}"
        results.append({"symbol": symbol, "expected_date": when.date(),
                        "expected_amount": amount, "ok": ok, "detail": detail})
    return results


# --- CLI ----------------------------------------------------------------------


def run(out_path=None):
    symbols = cached_symbols()
    if not symbols:
        print("[events] cache is empty — run `python -m data.fetch` first")
        return 1

    events = build_events(symbols)
    out = Path(out_path or EVENTS_PATH)
    events.to_parquet(out, index=False)
    print(f"[events] wrote {out} ({len(events)} row(s))")

    for line in summarize(events, universe_size=len(symbols)):
        print(line)

    results = validate(events, validation_events())
    if not results:
        print("[events] no validation_events in params.yaml — nothing pinned to check")
        return 0
    failed = [r for r in results if not r["ok"]]
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"[events] validate {mark}  {r['symbol']} {r['expected_date']} "
              f"{r['expected_amount']:.2f} — {r['detail']}")
    if failed:
        print(f"[events] {len(failed)} of {len(results)} validation event(s) FAILED — "
              f"the table is written but should not be trusted until this is explained")
        return 1
    print(f"[events] all {len(results)} validation event(s) match the record")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build the dividend event table from the research cache and "
                    "validate it against the events pinned in params.yaml.")
    parser.add_argument("--out", metavar="PATH",
                        help=f"where to write the table (default {EVENTS_PATH})")
    args = parser.parse_args(argv)
    return run(out_path=args.out)


if __name__ == "__main__":
    raise SystemExit(main())
