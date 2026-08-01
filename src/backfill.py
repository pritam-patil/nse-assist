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
the real traded price. The series is internally consistent, but it is a different
basis from ingest.py's raw bhavcopy bars. See verify_data.py — the mixed-basis
check exists precisely because these two stages disagree by construction, and the
disagreement grows each time a universe member splits.

Resumable per symbol. A symbol whose stored history already reaches back far
enough is skipped, so a run interrupted at symbol 60 continues from there instead
of re-downloading the first 59. Resumability reads the prices table rather than a
checkpoint file — the data itself is the progress record, and it cannot go stale.
"""

import time
from datetime import date, datetime, timedelta, timezone

from src import universe
from src.db import get_connection, init_db
from src.ingest import SOURCE_BHAVCOPY, is_phantom, store_bars

# Distinct from ingest.py's SOURCE_YFINANCE: same provider, different adjustment
# basis, and conflating them would hide exactly the problem verify-data looks for.
SOURCE_ADJUSTED = "yfinance-adj"

BACKFILL_YEARS = 3

# yfinance batches tickers into one HTTP call. Small batches are gentler and, more
# usefully, bound how much work an interruption throws away.
BATCH_SIZE = 15
PAUSE_BETWEEN_BATCHES_SECONDS = 2.0

# A symbol counts as covered when its oldest stored bar is within this margin of
# the target start. Listings younger than the window (a recent IPO or a demerged
# entity) can never reach it, so an exact match would retry them on every run.
COVERAGE_SLACK_DAYS = 20


def target_start(today=None):
    today = today or datetime.now(timezone.utc).date()
    return today - timedelta(days=365 * BACKFILL_YEARS)


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


def run(dry_run=False, symbols=None, **kwargs):
    symbols = tuple(symbols or universe.UNIVERSE)
    today = datetime.now(timezone.utc).date()
    start = target_start(today)

    conn = get_connection()
    try:
        init_db(conn)
        pending = pending_symbols(conn, symbols, start, today)
        done = len(symbols) - len(pending)

        print(f"[backfill] {BACKFILL_YEARS}y window {start} to {today}, adjusted prices")
        if done:
            print(f"[backfill] {done} symbol(s) already cover the window — resuming with {len(pending)}")
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

        # Not a silent success: bhavcopy outranks this source, so dates it already
        # owns keep raw prices and this stage's writes to them are discarded.
        kept = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM prices WHERE source = ?", (SOURCE_BHAVCOPY,)
        ).fetchone()[0]
        if kept:
            print(
                f"[backfill] {kept} date(s) remain on {SOURCE_BHAVCOPY} (raw, unadjusted) — "
                f"it outranks this source. Run --stage verify-data for the basis check."
            )

        if failures:
            print(f"[backfill] {len(failures)} batch(es) failed: {'; '.join(failures[:3])}")
            if stored == 0:
                raise RuntimeError(f"every batch failed: {failures[0]}")
        return stored
    finally:
        conn.close()
