"""Stage 1 — pulls daily OHLCV bars for the universe into the `prices` table.

Source is Yahoo's public chart endpoint: no key, no registration, and it serves
NSE equities as SYMBOL.NS. It is rate-limited and occasionally flaky, so every
request retries with backoff and one symbol failing never aborts the rest — a
missing bar degrades that symbol's features, it does not break the run.

Incremental by default: the first sight of a symbol backfills
config.INGEST_LOOKBACK_DAYS, afterwards only the days since its newest stored bar
are requested.
"""

import time
from datetime import datetime, timedelta, timezone

import requests

from src import config, universe
from src.db import get_connection, init_db

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

# Yahoo returns 403 to the default python-requests agent.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) nse-assist/0.1"

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.5

# Yahoo throttles aggressively above roughly one request per second sustained.
# 100 symbols at this pace is a little under a minute, which is fine for a
# once-a-day job and keeps the endpoint from starting to 429.
PAUSE_BETWEEN_SYMBOLS_SECONDS = 0.4


def _fetch_json(url, params):
    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(BACKOFF_BASE_SECONDS**attempt)
        try:
            response = requests.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            last_error = RuntimeError(f"network error: {exc}")
            continue
        # 4xx other than 429 will not improve on retry — a delisted or renamed
        # symbol answers 404 and should surface immediately.
        if response.status_code == 429 or response.status_code >= 500:
            last_error = RuntimeError(f"HTTP {response.status_code}")
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:80]}")
        return response.json()
    raise last_error or RuntimeError("request failed")


def fetch_bars(symbol, start_date, end_date=None):
    """Daily bars for one symbol as a list of dicts, oldest first.

    Yahoo returns parallel arrays with nulls on non-trading days that slipped into
    the range; those rows are dropped rather than stored as zero-priced bars.
    """
    end_date = end_date or datetime.now(timezone.utc).date()
    payload = _fetch_json(
        CHART_URL.format(ticker=universe.yahoo_ticker(symbol)),
        {
            "period1": int(datetime.combine(start_date, datetime.min.time()).timestamp()),
            "period2": int(datetime.combine(end_date, datetime.max.time()).timestamp()),
            "interval": "1d",
            "events": "div,splits",
        },
    )

    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        error = (payload.get("chart") or {}).get("error")
        raise RuntimeError(f"no data returned ({error})")

    stamps = result[0].get("timestamp") or []
    quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]

    bars = []
    for index, stamp in enumerate(stamps):
        values = {key: (quote.get(key) or [None] * len(stamps))[index] for key in ("open", "high", "low", "close", "volume")}
        if values["close"] is None:
            continue
        bars.append(
            {
                "date": datetime.fromtimestamp(stamp, tz=timezone.utc).date().isoformat(),
                "open": values["open"],
                "high": values["high"],
                "low": values["low"],
                "close": values["close"],
                "volume": int(values["volume"] or 0),
            }
        )
    return bars


def latest_stored_date(conn, symbol):
    row = conn.execute("SELECT MAX(date) FROM prices WHERE symbol = ?", (symbol,)).fetchone()
    return row[0] if row and row[0] else None


def store_bars(conn, symbol, bars, source=None):
    """Upserts bars. INSERT OR REPLACE against the (symbol, date) key makes a
    re-run of the same day a no-op rather than a duplicate."""
    conn.executemany(
        """INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, volume, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                symbol,
                bar["date"],
                bar["open"],
                bar["high"],
                bar["low"],
                bar["close"],
                bar["volume"],
                source or config.PRICE_SOURCE,
            )
            for bar in bars
        ],
    )
    conn.commit()
    return len(bars)


def run(dry_run=False, symbols=None, backfill=False, **kwargs):
    """Extends each symbol's history to today.

    Incremental: only the days after a symbol's newest stored bar are requested.
    That means raising config.INGEST_LOOKBACK_DAYS does *not* retroactively deepen
    symbols already in the table — the window only ever grows forward. Pass
    backfill=True (or --backfill) to re-request the full lookback for every symbol;
    the upsert makes it safe, it is just slow.
    """
    symbols = symbols or universe.UNIVERSE
    conn = get_connection()
    try:
        init_db(conn)
        today = datetime.now(timezone.utc).date()
        stored = 0
        failures = []

        for index, symbol in enumerate(symbols):
            latest = None if backfill else latest_stored_date(conn, symbol)
            if latest:
                start = datetime.fromisoformat(latest).date() + timedelta(days=1)
                if start > today:
                    continue
            else:
                start = today - timedelta(days=config.INGEST_LOOKBACK_DAYS)

            try:
                bars = fetch_bars(symbol, start, today)
            except Exception as exc:
                failures.append(f"{symbol}: {exc}")
                continue

            if dry_run:
                print(f"[ingest] {symbol}: {len(bars)} bar(s) from {start} (dry run, not stored)")
            else:
                stored += store_bars(conn, symbol, bars)

            if index < len(symbols) - 1:
                time.sleep(PAUSE_BETWEEN_SYMBOLS_SECONDS)

        print(f"[ingest] {stored} bar(s) stored across {len(symbols)} symbol(s)")
        if failures:
            print(f"[ingest] {len(failures)} symbol(s) failed: {'; '.join(failures[:5])}")
            # Partial data is the normal case for a flaky free feed. Only a total
            # wipeout means the feed itself is down and the run should be believed
            # to have failed.
            if len(failures) == len(symbols):
                raise RuntimeError(f"all {len(symbols)} symbols failed — feed is down?")
        return stored
    finally:
        conn.close()
