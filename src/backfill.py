"""Stage — three years of daily OHLCV for the universe, via yfinance.

    python main.py --stage backfill

Prices here are ADJUSTED (`auto_adjust=True`): splits and bonus issues are applied
backwards through the series, so a 1:5 split does not appear as an 80% overnight
crash. This matters more than it sounds. Every indicator in features.py reads a
window of past closes, so one unadjusted split inside a 200-day lookback corrupts
the moving average, the ATR, and the RSI for the whole window after it — and the
rules then fire on an artefact. Adjusted history is the only version where a
backtest is measuring the strategy rather than the corporate actions.

Adjustment is backward-looking: Yahoo scales *past* bars so the most recent one is
the real traded price. This source therefore outranks raw bhavcopy in
ingest.SOURCE_RANK — the table feeds indicators rather than settlement, and only an
adjusted series makes a lookback window mean anything.

The corollary is that this stage owns the history and ingest.py owns the right
edge. Adjustment factors are 1.0 until a corporate action, so the raw bars daily
ingest writes are correct as written; they go stale only when a universe member
splits, at which point the adjusted history and the raw tail meet at a cliff.
symbols_needing_readjustment() finds exactly those symbols, and re-running this
stage repairs them.

Resumable per symbol. A symbol whose stored history already reaches back far
enough is skipped, so a run interrupted at symbol 60 continues from there instead
of re-downloading the first 59. Resumability reads the prices table rather than a
checkpoint file — the data itself is the progress record, and it cannot go stale.
"""

import time
from datetime import date, datetime, timedelta, timezone

from src import universe
from src.db import get_connection, init_db
from src.ingest import SOURCE_ADJUSTED, is_phantom, store_bars

# Re-exported for callers that think of it as this module's output. The constant
# lives in ingest.py because store_bars() has to rank it. Distinct from
# SOURCE_YFINANCE: same provider, different adjustment basis, and conflating them
# would hide exactly the problem verify-data looks for.
__all__ = ["SOURCE_ADJUSTED", "run"]

BACKFILL_YEARS = 3

# yfinance batches tickers into one HTTP call. Small batches are gentler and, more
# usefully, bound how much work an interruption throws away.
BATCH_SIZE = 15
PAUSE_BETWEEN_BATCHES_SECONDS = 2.0

# A symbol counts as covered when its oldest stored bar is within this margin of
# the target start. Listings younger than the window (a recent IPO or a demerged
# entity) can never reach it, so an exact match would retry them on every run.
COVERAGE_SLACK_DAYS = 20


def target_start(conn=None, today=None):
    """Start of the window to adjust: BACKFILL_YEARS back, extended to cover any
    older bars already stored.

    The extension matters. Anything left outside the window keeps whatever basis it
    was written with, which puts an unadjusted stretch on the far side of a seam —
    the exact defect this stage exists to remove, just relocated. Covering the whole
    stored span means one basis end to end.
    """
    today = today or datetime.now(timezone.utc).date()
    start = today - timedelta(days=365 * BACKFILL_YEARS)
    if conn is not None:
        earliest = conn.execute("SELECT MIN(date) FROM prices").fetchone()[0]
        if earliest:
            start = min(start, date.fromisoformat(earliest))
    return start


def coverage(conn, symbols):
    """{symbol: (earliest, latest)} over stored bars, for resumability."""
    rows = conn.execute(
        "SELECT symbol, MIN(date) AS lo, MAX(date) AS hi FROM prices "
        f"WHERE symbol IN ({','.join('?' * len(symbols))}) GROUP BY symbol",
        list(symbols),
    ).fetchall()
    return {r["symbol"]: (r["lo"], r["hi"]) for r in rows}


def pending_symbols(conn, symbols, start, end):
    """Symbols whose stored history does not already span the window.

    This is the resumability check. A symbol is skipped when it already reaches
    back to `start` (within COVERAGE_SLACK_DAYS) and forward to `end`, which is
    what makes an interrupted run continue rather than restart.
    """
    have = coverage(conn, symbols)
    deadline = (start + timedelta(days=COVERAGE_SLACK_DAYS)).isoformat()
    pending = []
    for symbol in symbols:
        span = have.get(symbol)
        if not span or not span[0]:
            pending.append(symbol)
        elif span[0] > deadline or span[1] < end.isoformat():
            pending.append(symbol)
    return pending


def symbols_needing_readjustment(conn, symbols, threshold=0.25):
    """Symbols whose stored series contains a corporate-action-shaped cliff.

    Date coverage alone cannot answer "is this symbol's history still correct?".
    After a split, ingest.py keeps writing raw bhavcopy bars while the adjusted
    history sits on the old basis, and the two meet at a cliff. The symbol has every
    date it needs, so a coverage check calls it done — it is the *basis* that went
    stale, and the cliff is the only visible symptom.

    Detected as a close-to-close move past `threshold` where at least one side is a
    raw source. A move that large entirely inside adjusted data is a real event
    (ADANIENT in February 2023), not an artefact, and re-fetching would not change it.
    """
    rows = conn.execute(
        f"SELECT symbol, date, close, source FROM prices "
        f"WHERE symbol IN ({','.join('?' * len(symbols))}) ORDER BY symbol, date",
        list(symbols),
    ).fetchall()

    flagged, previous = {}, {}
    for row in rows:
        prior = previous.get(row["symbol"])
        if prior and prior["close"]:
            change = (row["close"] - prior["close"]) / prior["close"]
            raw_involved = SOURCE_ADJUSTED not in (prior["source"], row["source"]) or (
                prior["source"] != row["source"]
            )
            if abs(change) >= threshold and raw_involved:
                flagged.setdefault(row["symbol"], (row["date"], change))
        previous[row["symbol"]] = row
    return flagged


def fetch_adjusted(symbols, start, end):
    """Adjusted daily bars as {symbol: [bar, ...]}, oldest first."""
    import yfinance as yf

    tickers = {universe.yahoo_ticker(s): s for s in symbols}
    frame = yf.download(
        list(tickers),
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),  # yfinance treats end as exclusive
        interval="1d",
        # The point of this module. See the docstring.
        auto_adjust=True,
        actions=False,
        group_by="ticker",
        progress=False,
        threads=False,
    )
    if frame is None or frame.empty:
        raise RuntimeError("yfinance returned no data")

    out = {}
    for ticker, symbol in tickers.items():
        try:
            sub = frame[ticker] if hasattr(frame.columns, "levels") else frame
        except KeyError:
            continue
        series = []
        for stamp, row in sub.iterrows():
            close = row.get("Close")
            if close is None or close != close:  # NaN
                continue
            bar = {
                "date": stamp.date().isoformat(),
                "open": _round(row.get("Open")),
                "high": _round(row.get("High")),
                "low": _round(row.get("Low")),
                "close": _round(close),
                "volume": int(row.get("Volume") or 0),
            }
            # Adjusted prices carry more decimals than the exchange prints, so this
            # rounds to paise; and Yahoo's zero-volume holiday placeholders are
            # rejected here exactly as they are in ingest.py.
            if bar["close"] is None or is_phantom(bar):
                continue
            series.append(bar)
        if series:
            out[symbol] = series
    return out


def _round(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value != value else round(value, 2)


def run(dry_run=False, symbols=None, force=False, **kwargs):
    """Re-fetches adjusted history for symbols that need it.

    `force` re-adjusts the whole universe regardless of what is stored — needed
    after a change to which source outranks which, since nothing about the stored
    dates reflects that.
    """
    symbols = tuple(symbols or universe.UNIVERSE)
    today = datetime.now(timezone.utc).date()

    conn = get_connection()
    try:
        init_db(conn)
        start = target_start(conn, today)
        print(f"[backfill] {BACKFILL_YEARS}y window {start} to {today}, adjusted prices")

        if force:
            pending = list(symbols)
            print(f"[backfill] force: re-adjusting all {len(pending)} symbol(s)")
        else:
            pending = pending_symbols(conn, symbols, start, today)
            done = len(symbols) - len(pending)
            if done:
                print(f"[backfill] {done} symbol(s) already cover the window — resuming with {len(pending)}")

            # Covered but stale in basis: a split has put a cliff in the series.
            stale = symbols_needing_readjustment(conn, symbols)
            extra = [s for s in stale if s not in set(pending)]
            if extra:
                detail = ", ".join(f"{s} ({stale[s][0]} {stale[s][1]:+.0%})" for s in extra[:4])
                print(f"[backfill] {len(extra)} symbol(s) need re-adjustment after a corporate action: {detail}")
                pending += extra
        if not pending:
            print("[backfill] nothing to do")
            return 0

        stored = 0
        failures = []
        batches = [pending[i : i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]

        for index, batch in enumerate(batches, start=1):
            try:
                series = fetch_adjusted(batch, start, today)
            except Exception as exc:
                # One dead batch must not lose the batches already stored — the
                # whole point of committing per batch is that a failure here is
                # resumable rather than total.
                failures.append(f"batch {index}: {exc}")
                print(f"[backfill] batch {index}/{len(batches)} FAILED ({exc})")
                continue

            rows = [(sym, bar, SOURCE_ADJUSTED) for sym, bars in series.items() for bar in bars]
            if dry_run:
                print(f"[backfill] batch {index}/{len(batches)}: {len(rows)} bar(s) (dry run)")
            else:
                stored += store_bars(conn, rows)
                got = ", ".join(sorted(series)[:4])
                print(
                    f"[backfill] batch {index}/{len(batches)}: {len(rows):,} bar(s) stored "
                    f"across {len(series)} symbol(s) ({got}…)"
                )
            missing = set(batch) - set(series)
            if missing:
                print(f"[backfill]   no data for: {', '.join(sorted(missing))}")

            if index < len(batches):
                time.sleep(PAUSE_BETWEEN_BATCHES_SECONDS)

        by_source = dict(conn.execute("SELECT source, COUNT(*) FROM prices GROUP BY source").fetchall())
        print(f"[backfill] {stored:,} bar(s) written; table by source: {by_source}")

        remaining = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM prices WHERE source != ?", (SOURCE_ADJUSTED,)
        ).fetchone()[0]
        if remaining:
            # Expected for dates after the window's end — daily ingest writes those
            # raw, and they are correct until the symbol's next corporate action.
            print(f"[backfill] {remaining} date(s) still on a raw source (newer than the window)")
        print("[backfill] run --stage verify-data to confirm one basis end to end")

        if failures:
            print(f"[backfill] {len(failures)} batch(es) failed: {'; '.join(failures[:3])}")
            if stored == 0:
                raise RuntimeError(f"every batch failed: {failures[0]}")
        return stored
    finally:
        conn.close()
