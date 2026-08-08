"""Burst 3 — the dividend-capture grid: every event, every timing, all-in costs.

    python -m data.backtest             # resumes where it left off
    python -m data.backtest --force     # discard cells and start clean

For each dividend event and each (entry, exit) cell of the grid in params.yaml,
simulate one fixed-notional round trip and pass it through the friction model —
brokerage to 94(7) — then aggregate per cell. Output is a per-cell trade log
parquet (the audit trail) and summary.parquet; the printed matrices are the
in-sample view of the same numbers.

THE FILL CONVENTION, STATED ONCE

Offsets count TRADING SESSIONS on each symbol's own bar series, and every leg
fills at that session's close:

    entry_days_before = e >= 1   buy at the close e sessions before the ex-date
                                 bar; the position is cum-dividend, the payout
                                 arrives, 94(7) is in play.
    entry_days_before = 0        buy at the EX-DATE close itself: post-drop, no
                                 dividend. A deliberate control row — it prices
                                 the post-ex drift alone.
    exit_days_after   = x        sell at the close x sessions after the ex-date
                                 bar; x = 0 is the ex-date close.

Session-space arithmetic is what makes holiday handling automatic: "20 days
before" landing on a holiday IS the next traded session's close, because
non-sessions never had an index. There is no lookahead in the schedule — both
legs are anchored to a dividend calendar published weeks ahead, and neither is
conditioned on the fill day's own price. The (0, 0) cell buys and sells the
same close and therefore measures pure friction; it stays in the grid as the
cost floor every other cell must clear.

Entries 10/15/20 sessions out are announcement-timing proxies: declarations
typically precede ex-dates by two to six weeks, so those cells ask whether the
anticipation drift is tradable at all, not merely the ex-day mechanics.

record_date is approximated by the ex-date when applying 94(7). Under T+1 the
record date is the next settlement day; the shift moves both three-month
windows by one day, which at that scale changes nothing.

DETERMINISTIC AND RESUMABLE, WITH AN HONESTY GUARD

Same inputs, same outputs: events are processed in sorted order, sizing is
integer floor division, nothing consults the clock or a random source. Each
cell lands in its own parquet, so an interrupted run resumes at the first
missing cell — but ONLY under the same inputs. meta.json pins a fingerprint of
params.yaml and events.parquet; if either changed, every stored cell is stale
and the run starts clean, loudly. A resumable cache without that guard would
happily mix two configurations into one summary.

THE SPLIT IS ENFORCED HERE, NOT REMEMBERED LATER

params.yaml pins train_until; every trade is labeled in- or out-of-sample and
the printed matrices show IN-SAMPLE ONLY. The out-of-sample rows sit in the
same logs but no aggregate of them is printed by this stage: picking a cell
from a table that already showed you the OOS answer is the exact snooping this
project's walk-forward discipline exists to prevent.
"""

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from data import events, fetch, frictions

PARAMS_PATH = Path(__file__).resolve().parent.parent / "params.yaml"
# "grid", not "backtest": a data/backtest/ directory beside data/backtest.py
# would shadow the module name in every conversation about it.
BACKTEST_DIR = Path(__file__).resolve().parent / "grid"
META_PATH_NAME = "meta.json"
SUMMARY_NAME = "summary.parquet"

TRADE_COLUMNS = (
    "entry_days_before", "exit_days_after", "symbol", "ex_date", "amount",
    "entry_date", "exit_date", "entry_close", "exit_close", "quantity",
    "deployed", "charges", "capital_pnl", "capital_tax", "dividend_gross",
    "dividend_tax", "disallowed_loss", "section_94_7_applied", "net",
    "net_return", "in_sample",
)


def load_backtest_params(path=None):
    params = yaml.safe_load(Path(path or PARAMS_PATH).read_text())["backtest"]
    return {
        "entries": [int(v) for v in params["entry_days_before"]],
        "exits": [int(v) for v in params["exit_days_after"]],
        "notional": float(params["notional_per_trade_inr"]),
        "train_until": params["train_until"],
    }


def fingerprint(params_path=None, events_path=None):
    """Bytes-level hash of the two inputs a stored cell depends on. A comment
    edit in params.yaml invalidates the cache too — a false positive that costs
    a thirty-second rerun, against silent staleness that costs a wrong table."""
    digest = hashlib.sha256()
    digest.update(Path(params_path or PARAMS_PATH).read_bytes())
    digest.update(Path(events_path or events.EVENTS_PATH).read_bytes())
    return digest.hexdigest()


def cell_path(entry, exit_after):
    return BACKTEST_DIR / f"trades_e{entry}_x{exit_after}.parquet"


# --- preparation --------------------------------------------------------------


def sessions_by_symbol(symbols):
    """{symbol: (dates list, closes list, {date: index})} — the session series
    each event's offsets walk on. Loaded once; 35 cells reuse it."""
    series = {}
    for symbol in symbols:
        frame = fetch.read_cache(symbol)
        if frame is None or frame.empty:
            continue
        frame = frame.sort_values("date").reset_index(drop=True)
        dates = list(frame["date"])
        series[symbol] = (dates, list(frame["close"]),
                          {stamp: index for index, stamp in enumerate(dates)})
    return series


def locate_events(table, series):
    """[(symbol, ex_date, amount, ex_index)] for events whose ex-date bar exists,
    sorted for determinism; the count of the rest, by reason."""
    located, missing = [], 0
    ordered = table.sort_values(["symbol", "ex_date"])
    for row in ordered.itertuples():
        symbol_series = series.get(row.symbol)
        if symbol_series is None or row.ex_date not in symbol_series[2]:
            missing += 1
            continue
        located.append((row.symbol, row.ex_date, row.amount,
                        symbol_series[2][row.ex_date]))
    return located, missing


# --- simulation ---------------------------------------------------------------


def simulate_cell(entry, exit_after, located, series, cfg, notional, train_until):
    """Every event once through this cell. Returns (frame, skip counts)."""
    skips = {"insufficient history": 0, "price above notional": 0}
    rows = []
    for symbol, ex_date, amount, ex_index in located:
        dates, closes, _ = series[symbol]
        entry_index = ex_index - entry if entry >= 1 else ex_index
        exit_index = ex_index + exit_after
        if entry_index < 0 or exit_index >= len(dates):
            skips["insufficient history"] += 1
            continue
        entry_close = closes[entry_index]
        quantity = int(notional // entry_close)
        if quantity < 1:
            skips["price above notional"] += 1
            continue

        cum_dividend = entry >= 1   # bought before ex-date, so the payout arrives
        entry_date = dates[entry_index]
        exit_date = dates[exit_index]
        result = frictions.trade(
            cfg, quantity=quantity,
            buy_price=entry_close, sell_price=closes[exit_index],
            buy_date=entry_date.date(), sell_date=exit_date.date(),
            dividend_per_share=amount if cum_dividend else 0.0,
            record_date=ex_date.date() if cum_dividend else None)

        deployed = result["buy_exec"] * quantity
        rows.append({
            "entry_days_before": entry, "exit_days_after": exit_after,
            "symbol": symbol, "ex_date": ex_date, "amount": amount,
            "entry_date": entry_date, "exit_date": exit_date,
            "entry_close": entry_close, "exit_close": closes[exit_index],
            "quantity": quantity, "deployed": round(deployed, 2),
            "charges": result["charges"], "capital_pnl": result["capital_pnl"],
            "capital_tax": result["capital_tax"],
            "dividend_gross": result["dividend_gross"],
            "dividend_tax": result["dividend_tax"],
            "disallowed_loss": result["disallowed_loss"],
            "section_94_7_applied": result["section_94_7_applied"],
            "net": result["net"],
            "net_return": round(result["net"] / deployed, 6),
            "in_sample": ex_date.date() <= train_until,
        })
    return pd.DataFrame(rows, columns=list(TRADE_COLUMNS)), skips


# --- aggregation --------------------------------------------------------------


def aggregate(trades):
    """Per (cell, sample) stats. Both samples are computed and stored; what gets
    PRINTED is the in-sample slice only — see the module docstring."""
    if trades.empty:
        return pd.DataFrame(columns=["entry_days_before", "exit_days_after",
                                     "in_sample", "trades"])
    grouped = trades.groupby(["entry_days_before", "exit_days_after", "in_sample"])
    rows = []
    for (entry, exit_after, in_sample), cell in grouped:
        returns = cell["net_return"]
        rows.append({
            "entry_days_before": entry, "exit_days_after": exit_after,
            "in_sample": in_sample, "trades": len(cell),
            "hit_rate": round(float((cell["net"] > 0).mean()), 4),
            "median_return": round(float(returns.median()), 6),
            "mean_return": round(float(returns.mean()), 6),
            "p25_return": round(float(returns.quantile(0.25)), 6),
            "p75_return": round(float(returns.quantile(0.75)), 6),
            "mean_net": round(float(cell["net"].mean()), 2),
            "total_net": round(float(cell["net"].sum()), 2),
            "bitten_by_94_7": int((cell["disallowed_loss"] > 0).sum()),
        })
    return pd.DataFrame(rows).sort_values(
        ["entry_days_before", "exit_days_after", "in_sample"]).reset_index(drop=True)


def print_matrices(summary, entries, exits):
    """Two in-sample matrices: median net return and hit rate, entries down,
    exits across. Medians, because per-event returns carry special-dividend
    tails that a mean would let one event own."""
    inside = summary[summary["in_sample"]] if len(summary) else summary
    if inside.empty:
        print("[backtest] no in-sample trades to print — check train_until "
              "against the event window")
        return
    by_cell = {(row.entry_days_before, row.exit_days_after): row
               for row in inside.itertuples()}

    for title, field, form in (("median net return", "median_return", "{:+.2%}"),
                               ("hit rate", "hit_rate", "{:.0%}")):
        print(f"\n[backtest] {title}, IN-SAMPLE — entry sessions before ex (rows) "
              f"x exit sessions after (cols)")
        header = "        " + "".join(f"x={x:<8}" for x in exits)
        print(header + "\n" + "-" * len(header))
        for entry in sorted(entries, reverse=True):
            cells = []
            for exit_after in exits:
                row = by_cell.get((entry, exit_after))
                cells.append(form.format(getattr(row, field)) if row else "-")
            print(f"e={entry:<4}  " + "".join(f"{cell:<9}" for cell in cells))
    counts = inside["trades"]
    print(f"\n[backtest] trades per cell: {counts.min():,} to {counts.max():,}; "
          f"out-of-sample rows are stored but deliberately not shown.")


# --- CLI ----------------------------------------------------------------------


def run(force=False):
    if not events.EVENTS_PATH.exists():
        print("[backtest] no events.parquet — run `python -m data.events` first "
              "(its validation is part of the deal)")
        return 1

    grid = load_backtest_params()
    cfg = frictions.Config.from_params()
    table = pd.read_parquet(events.EVENTS_PATH)
    series = sessions_by_symbol(sorted(table["symbol"].unique()))
    located, missing = locate_events(table, series)
    print(f"[backtest] {len(located):,} event(s) located on their session series"
          + (f"; {missing} without an ex-date bar" if missing else ""))

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = BACKTEST_DIR / META_PATH_NAME
    stamp = fingerprint()
    stored = json.loads(meta_path.read_text()) if meta_path.exists() else None
    if force or stored is None or stored.get("fingerprint") != stamp:
        if stored is not None and stored.get("fingerprint") != stamp and not force:
            print("[backtest] inputs changed since the stored cells were written — "
                  "starting clean (a resume would mix two configurations)")
        for stale in BACKTEST_DIR.glob("*.parquet"):
            stale.unlink()
        meta_path.write_text(json.dumps({"fingerprint": stamp}))

    cells = [(entry, exit_after) for entry in grid["entries"]
             for exit_after in grid["exits"]]
    frames, computed = [], 0
    for entry, exit_after in cells:
        path = cell_path(entry, exit_after)
        if path.exists():
            frames.append(pd.read_parquet(path))
            continue
        frame, skips = simulate_cell(entry, exit_after, located, series, cfg,
                                     grid["notional"], grid["train_until"])
        frame.to_parquet(path, index=False)
        computed += 1
        skipped = ", ".join(f"{count} {reason}" for reason, count in skips.items()
                            if count)
        print(f"[backtest] cell e={entry} x={exit_after}: {len(frame):,} trade(s)"
              + (f" ({skipped})" if skipped else ""))
        frames.append(frame)

    trades = pd.concat(frames, ignore_index=True)
    summary = aggregate(trades)
    summary.to_parquet(BACKTEST_DIR / SUMMARY_NAME, index=False)
    print(f"[backtest] {computed} cell(s) computed, {len(cells) - computed} reused; "
          f"{len(trades):,} trades logged under {BACKTEST_DIR}")
    print_matrices(summary, grid["entries"], grid["exits"])

    bitten = trades[trades["disallowed_loss"] > 0]
    if len(bitten):
        print(f"[backtest] 94(7) bit {len(bitten):,} trade(s) for "
              f"{bitten['disallowed_loss'].sum():,.0f} of disallowed losses")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Simulate the dividend-capture grid over every cached event.")
    parser.add_argument("--force", action="store_true",
                        help="discard stored cells and recompute everything")
    args = parser.parse_args(argv)
    return run(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
