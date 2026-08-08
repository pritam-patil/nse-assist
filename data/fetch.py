"""Research cache — daily OHLCV plus dividend events, per-symbol parquet, via yfinance.

    python -m data.fetch                        # warm the NIFTY 500 cache
    python -m data.fetch --symbols RELIANCE,TCS
    python -m data.fetch --retry                # re-attempt earlier failures only
    python -m data.fetch --force                # full windows, cache or not

WHY THIS EXISTS BESIDE src/ingest.py AND src/backfill.py

Those two feed the trading pipeline: NIFTY 100, one SQLite table, no dividends —
signals read closes and nothing downstream prices a payout. This is the research
cache for work that needs what the pipeline deliberately excludes: five times the
universe and the dividend record. It lives in its own directory with its own
storage precisely so that experiments against it can never touch the pipeline's
database or trip over its source-ranking rules. Nothing in src/ reads these files.

ONE BASIS, AND WHICH COLUMN CAN LIE

Bars are fetched with auto_adjust=False: open/high/low/close are raw traded
prices, dividends and splits are their own event columns, adj_close is Yahoo's
combined adjustment. Raw prices are append-stable — a new bar never rewrites an
old one — which is what makes incremental refresh sound. adj_close is the
exception: Yahoo rescales the entire history every time an event lands, so a
cache appended across a new dividend would carry two silently incompatible
adj_close bases either side of the seam. The refresh therefore watches newly
fetched rows for events, and a symbol showing one it has not cached gets its full
window refetched instead of appended. That costs a few full downloads per symbol
per year and buys an adj_close column that is one basis end to end — the same
seam src/backfill.py hunts with symbols_needing_readjustment(), prevented here
instead of detected.

THE LAST CACHED BAR IS ALWAYS REFETCHED

A run during market hours caches today's partial bar. If the next refresh started
the day after it, that partial bar would be frozen in place as if it were the
close. Resuming FROM the last cached date costs one duplicate row per symbol —
deduplicated keeping the fresh copy — and removes the hazard.

FAILURES ARE LOGGED, NOT FATAL

One dead symbol must not abort a 500-symbol warm. Failures land in
data/cache/retry.txt with their reason; --retry re-attempts exactly those; a
success (or a clean "nothing new") clears the entry, so the file converges on the
symbols still actually failing rather than growing into a list of everything that
ever hiccuped. Only a run in which nothing at all succeeded exits non-zero.
"""

import argparse
import csv
import io
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from src import config, universe
from src.ingest import BROWSER_HEADERS, NSE_HOME

CACHE_DIR = Path(__file__).resolve().parent / "cache"

# The cache schema. Raw prices plus the two event columns, and Yahoo's combined
# adjustment kept because computing it from events is exactly the wheel this
# module exists not to reinvent.
COLUMNS = ("date", "open", "high", "low", "close", "adj_close", "volume",
           "dividend", "split")
RENAMES = {
    "Open": "open", "High": "high", "Low": "low", "Close": "close",
    "Adj Close": "adj_close", "Volume": "volume",
    "Dividends": "dividend", "Stock Splits": "split",
}

# Ten years rather than the pipeline's three: a research cache is fetched once and
# asked questions for months, and dividend-inclusive questions in particular need
# more than one market cycle to mean anything.
DEFAULT_YEARS = 10

# Same batch size and pause as src/backfill.py, for the same reasons: small
# batches bound what an interruption throws away, and Yahoo tolerates a polite
# client far longer than a hammering one.
BATCH_SIZE = 15
PAUSE_BETWEEN_BATCHES_SECONDS = 2.0
MAX_RETRIES = 3

# NSE publishes the constituent list as a CSV on the same archives host as the
# bhavcopy, with the same 403-unless-you-look-like-a-browser behaviour.
NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY500_MAX_AGE_DAYS = 7
# The index holds ~500 names; far fewer parsed means a truncated or reformatted
# file, and warming a cache against half an index is worse than failing loudly.
NIFTY500_MIN_ROWS = 450


# --- cache files --------------------------------------------------------------


def cache_path(symbol):
    return CACHE_DIR / f"{symbol}.parquet"


def retry_path():
    return CACHE_DIR / "retry.txt"


def nifty500_path():
    return CACHE_DIR / "_nifty500.csv"


def read_cache(symbol):
    path = cache_path(symbol)
    if not path.exists():
        return None
    return pd.read_parquet(path)


def write_cache(symbol, frame):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(cache_path(symbol), index=False)


# --- shaping ------------------------------------------------------------------


def tidy(sub):
    """One symbol's yfinance frame in the cache schema, phantoms dropped.

    Yahoo emits a bar for NSE holidays with zero volume and no range, carrying the
    previous close forward — the same placeholder src/ingest.py rejects, rejected
    here for the same reason. The one exception: a flat bar carrying a dividend or
    split stays, because dropping it would drop the event.
    """
    frame = sub.rename(columns=RENAMES).reset_index()
    frame = frame.rename(columns={frame.columns[0]: "date"})
    stamps = pd.to_datetime(frame["date"])
    if getattr(stamps.dt, "tz", None) is not None:
        stamps = stamps.dt.tz_localize(None)
    frame["date"] = stamps.dt.normalize()

    for name in COLUMNS[1:]:
        if name not in frame.columns:
            frame[name] = 0.0 if name in ("dividend", "split") else float("nan")
    frame = frame[list(COLUMNS)]
    frame = frame[frame["close"].notna()]
    frame["volume"] = frame["volume"].fillna(0).astype("int64")
    frame["dividend"] = frame["dividend"].fillna(0.0).astype(float)
    frame["split"] = frame["split"].fillna(0.0).astype(float)

    flat = (
        (frame["volume"] == 0)
        & (frame["open"] == frame["high"])
        & (frame["high"] == frame["low"])
        & (frame["low"] == frame["close"])
    )
    frame = frame[~flat | (frame["dividend"] != 0) | (frame["split"] != 0)]
    return frame.sort_values("date").reset_index(drop=True)


def _events(frame):
    rows = frame[(frame["dividend"] != 0) | (frame["split"] != 0)]
    return {
        (stamp.date().isoformat(), round(float(div), 6), round(float(split), 6))
        for stamp, div, split in zip(rows["date"], rows["dividend"], rows["split"])
    }


def has_new_events(cached, fresh):
    """True when fresh rows carry an event the cache lacks.

    That event is the signal that Yahoo has rescaled every adj_close before it, so
    appending would put a basis seam in the cached series. Compared as (date,
    amount) sets rather than "any event in fresh" because fresh deliberately
    overlaps the last cached bar — an event already cached on that bar is old news,
    not a rescale.
    """
    return bool(_events(fresh) - _events(cached))


def merge(cached, fresh):
    """Cached history with fresh rows appended; on an overlapping date, fresh wins.

    Fresh winning is what un-freezes a partial bar cached mid-session — the
    overlap exists so this replacement can happen.
    """
    combined = pd.concat([cached, fresh], ignore_index=True)
    combined = combined.drop_duplicates(subset="date", keep="last")
    return combined.sort_values("date").reset_index(drop=True)


# --- retrieval ----------------------------------------------------------------


def _download(symbols, start, end=None):
    """Tidied frames as {symbol: frame} for one batched yfinance call.

    `end` is inclusive when given (yfinance's own is exclusive); omitted, the
    fetch runs through the most recent bar Yahoo has. Timeout and bounded retries
    per src/ingest.py's fetch_yfinance — a hung dependency has to look like a
    failed one.
    """
    import yfinance as yf

    tickers = {universe.yahoo_ticker(s): s for s in symbols}
    frame = None
    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            frame = yf.download(
                list(tickers),
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat() if end else None,
                interval="1d",
                auto_adjust=False,   # raw prices; adjustment stays in its own column
                actions=True,        # the dividend and split columns are the point
                group_by="ticker",
                progress=False,
                threads=False,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            last_error = RuntimeError(f"yfinance download failed: {exc}")
            continue
        if frame is not None and not frame.empty:
            break
    if frame is None or frame.empty:
        raise last_error or RuntimeError("yfinance returned no data")

    out = {}
    for ticker, symbol in tickers.items():
        # group_by="ticker" nests columns under the ticker even for one symbol;
        # the flat fallback covers versions that collapsed the level.
        try:
            sub = frame[ticker] if hasattr(frame.columns, "levels") else frame
        except KeyError:
            continue
        tidied = tidy(sub)
        if not tidied.empty:
            out[symbol] = tidied
    return out


# --- the retry file -----------------------------------------------------------


def read_retry():
    """{symbol: reason} from the retry file; no file means nothing pending."""
    path = retry_path()
    if not path.exists():
        return {}
    entries = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        symbol, _, reason = line.partition("\t")
        entries[symbol.strip()] = reason.strip()
    return entries


def update_retry(attempted, failures):
    """Rewrites the retry file: this run's verdict replaces this run's symbols.

    A symbol attempted and succeeded is cleared; one attempted and failed is
    recorded with its reason; one not attempted at all keeps whatever entry it
    had, so a --symbols run cannot silently absolve the rest of the universe.
    An empty file is deleted rather than left: "no retry file" is the resting
    state, and the file existing is itself the signal that work is pending.
    """
    entries = {s: r for s, r in read_retry().items() if s not in set(attempted)}
    entries.update(failures)
    path = retry_path()
    if not entries:
        if path.exists():
            path.unlink()
        return entries
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"{symbol}\t{reason}" for symbol, reason in sorted(entries.items())]
    path.write_text("\n".join(lines) + "\n")
    return entries


# --- the NIFTY 500 list -------------------------------------------------------


def _parse_nifty500(text):
    rows = list(csv.DictReader(io.StringIO(text)))
    fields = list(rows[0]) if rows else []
    symbol_field = next(
        (f for f in fields if f and f.strip().lower() == "symbol"), None)
    if symbol_field is None:
        raise RuntimeError("constituent CSV has no Symbol column — format changed?")
    series_field = next(
        (f for f in fields if f and f.strip().lower() == "series"), None)
    symbols = [
        row[symbol_field].strip()
        for row in rows
        if (row.get(symbol_field) or "").strip()
        and (series_field is None or (row.get(series_field) or "").strip() == "EQ")
    ]
    if len(symbols) < NIFTY500_MIN_ROWS:
        raise RuntimeError(
            f"only {len(symbols)} symbols parsed of ~500 expected — file looks truncated")
    return symbols


def nifty500_symbols(max_age_days=NIFTY500_MAX_AGE_DAYS):
    """NIFTY 500 constituents from NSE's published CSV, cached beside the bars.

    Fetched rather than committed — the opposite call from src/universe.py, and
    deliberate: the 100 drive live signals, where membership changes deserve a
    reviewable diff, while these 500 only scope a research cache and a week of
    drift is harmless. The cached copy doubles as the offline fallback: when NSE
    is unreachable the warm proceeds on the last list rather than not at all,
    and says so. Parsing happens before the cache is overwritten, so a truncated
    download can never destroy a good copy.
    """
    path = nifty500_path()
    if path.exists():
        age_days = (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 86400
        if age_days <= max_age_days:
            return _parse_nifty500(path.read_text())
    try:
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
        # Cookie priming: the archives host 403s a cold client even with browser
        # headers. Same quirk, same fix, as the bhavcopy in src/ingest.py.
        session.get(NSE_HOME, timeout=config.REQUEST_TIMEOUT_SECONDS)
        response = session.get(
            NIFTY500_URL,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            headers={"Referer": NSE_HOME},
        )
        response.raise_for_status()
        symbols = _parse_nifty500(response.text)
    except Exception as exc:
        if path.exists():
            stale = date.fromtimestamp(path.stat().st_mtime).isoformat()
            print(f"[fetch] NIFTY 500 list fetch failed ({exc}); "
                  f"using the cached copy from {stale}")
            return _parse_nifty500(path.read_text())
        raise
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(response.text)
    return symbols


# --- the refresh --------------------------------------------------------------


def plan(symbols, full_start, force=False):
    """[(symbol, start)] sorted oldest-start-first.

    Cached symbols resume FROM their last bar, not the day after — see the module
    docstring on partial bars. Sorting by start groups the full-window fetches
    (new symbols) into the same batches, so one batch's date range is set by
    symbols that actually need it rather than by one straggler.
    """
    plans = []
    for symbol in symbols:
        cached = None if force else read_cache(symbol)
        if cached is None or cached.empty:
            plans.append((symbol, full_start))
        else:
            plans.append((symbol, cached["date"].max().date()))
    plans.sort(key=lambda pair: pair[1])
    return plans


def refresh(symbols, years=DEFAULT_YEARS, force=False, batch_size=BATCH_SIZE,
            pause=PAUSE_BETWEEN_BATCHES_SECONDS):
    """Warms the cache for `symbols`; returns {"fetched", "unchanged", "failures"}.

    Each batch downloads from the oldest start among its members; a symbol's own
    merge discards nothing, because overlapping raw rows are identical and
    overlapping adj_close rows are only different when an event fired — which
    triggers the full refetch instead.
    """
    today = datetime.now(timezone.utc).date()
    full_start = today - timedelta(days=365 * years)
    plans = plan(symbols, full_start, force)
    print(f"[fetch] {len(plans)} symbol(s), window {full_start} to {today}, "
          f"cache {CACHE_DIR}")

    fetched, unchanged, failures = [], [], {}
    batches = [plans[i:i + batch_size] for i in range(0, len(plans), batch_size)]
    for index, batch in enumerate(batches, start=1):
        names = [symbol for symbol, _ in batch]
        start = min(start for _, start in batch)
        try:
            series = _download(names, start)
        except Exception as exc:
            # The whole batch is unknown, not the whole run. Later batches still
            # get their chance — that is the point of the retry file.
            for symbol in names:
                failures[symbol] = str(exc)
            print(f"[fetch] batch {index}/{len(batches)} FAILED ({exc})")
            if index < len(batches):
                time.sleep(pause)
            continue

        for symbol, _ in batch:
            fresh = series.get(symbol)
            cached = None if force else read_cache(symbol)
            have_cache = cached is not None and not cached.empty
            try:
                if fresh is None or fresh.empty:
                    if have_cache:
                        # A short empty range is a market that has been shut since
                        # the last bar, not a failure.
                        unchanged.append(symbol)
                    else:
                        failures[symbol] = "no data returned for the full window"
                    continue
                if have_cache and has_new_events(cached, fresh):
                    print(f"[fetch]   {symbol}: new dividend/split — refetching the "
                          f"full window (adj_close history was rescaled behind it)")
                    refetched = _download([symbol], full_start).get(symbol)
                    if refetched is None or refetched.empty:
                        failures[symbol] = "refetch after corporate action returned no data"
                        continue
                    write_cache(symbol, refetched)
                elif have_cache:
                    write_cache(symbol, merge(cached, fresh))
                else:
                    write_cache(symbol, fresh)
                fetched.append(symbol)
            except Exception as exc:
                failures[symbol] = f"{type(exc).__name__}: {exc}"

        print(f"[fetch] batch {index}/{len(batches)}: "
              f"{sum(1 for s, _ in batch if s in set(fetched))} refreshed, "
              f"{sum(1 for s, _ in batch if s in set(unchanged))} current, "
              f"{sum(1 for s, _ in batch if s in failures)} failed")
        if index < len(batches):
            time.sleep(pause)

    update_retry([symbol for symbol, _ in plans], failures)
    print(f"[fetch] done: {len(fetched)} refreshed, {len(unchanged)} already "
          f"current, {len(failures)} failed")
    if failures:
        sample = ", ".join(sorted(failures)[:6])
        more = "…" if len(failures) > 6 else ""
        print(f"[fetch] failures logged to {retry_path()} ({sample}{more}) — "
              f"re-attempt with --retry")
    return {"fetched": fetched, "unchanged": unchanged, "failures": failures}


# --- CLI ----------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Warm the per-symbol parquet cache of daily OHLCV and dividend events.")
    which = parser.add_mutually_exclusive_group()
    which.add_argument(
        "--symbols", metavar="A,B,C",
        help="comma-separated NSE symbols (default: the NIFTY 500 list)")
    which.add_argument(
        "--retry", action="store_true",
        help="re-attempt only the symbols in the retry file")
    parser.add_argument(
        "--years", type=int, default=DEFAULT_YEARS,
        help=f"history window for uncached symbols (default {DEFAULT_YEARS})")
    parser.add_argument(
        "--force", action="store_true",
        help="refetch full windows even for cached symbols")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.retry:
        pending = read_retry()
        if not pending:
            print("[fetch] retry file is empty — nothing to re-attempt")
            return 0
        symbols = sorted(pending)
        print(f"[fetch] re-attempting {len(symbols)} symbol(s) from {retry_path()}")
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = nifty500_symbols()
    if not symbols:
        print("[fetch] no symbols to fetch")
        return 1

    result = refresh(symbols, years=args.years, force=args.force)
    if result["failures"] and not result["fetched"] and not result["unchanged"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
