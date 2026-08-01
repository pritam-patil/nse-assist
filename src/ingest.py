"""Stage 1 — daily OHLCV for the universe, from NSE's bhavcopy with a yfinance fallback.

Primary source is NSE's own end-of-day file, because it is the exchange's record
rather than a redistribution of it, and one request covers every symbol for a day
instead of one request per symbol.

Since 2024 that file is the UDiFF "common bhavcopy", a zipped CSV at:

    https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip

NSE blocks non-browser clients, so every request goes through a session that has
first loaded the homepage to pick up cookies and carries full browser headers.
Requests are spaced out; this is someone else's free infrastructure.

Fallback is yfinance when bhavcopy fails — a blocked IP, a format change, or a
backfill too large to fetch a day at a time. Two things about it are load-bearing:

  auto_adjust=False   yfinance defaults this to True and would return split- and
                      dividend-adjusted prices. Bhavcopy carries raw traded prices,
                      so the default would put two silently incompatible price
                      series in one column.
  phantom bars        Yahoo emits a bar for NSE holidays with zero volume and
                      open=high=low=close. Stored, those flatten RSI and drag the
                      20-day average volume down, so they are rejected on sight.

Sources are never mixed for a date: bhavcopy overwrites a day yfinance filled, and
yfinance never overwrites a day bhavcopy filled. See store_bars().
"""

import csv
import io
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import requests

from src import config, holidays_2026 as calendar, universe
from src.db import get_connection, init_db

SOURCE_BHAVCOPY = "bhavcopy"
SOURCE_YFINANCE = "yfinance"
# Defined here rather than in backfill.py so store_bars() can rank it without
# importing the module that writes it.
SOURCE_ADJUSTED = "yfinance-adj"

# Which source wins when two of them have bars for the same (symbol, date).
#
# Split-adjusted history outranks everything, including the exchange's own file.
# That looks backwards for about a second — bhavcopy is the authoritative record of
# what actually traded — but the table feeds indicators, not settlement, and every
# indicator reads a window of past closes. A raw series is only correct at its right
# edge; the moment a symbol splits, its entire history is wrong by the split factor
# and the 200-day average is meaningless. Adjusted history is the only basis on
# which a lookback window means anything.
#
# Raw bhavcopy still outranks unadjusted yfinance: same basis, better provenance.
#
# The consequence to keep in mind: adjustment factors are 1.0 until a corporate
# action happens, so daily bhavcopy bars are correct as written and only go stale
# when a universe member splits. That is what --stage verify-data detects and what
# a re-run of --stage backfill repairs.
SOURCE_RANK = {
    SOURCE_YFINANCE: 1,
    SOURCE_BHAVCOPY: 2,
    SOURCE_ADJUSTED: 3,
}

# Inlined into the conflict clause below. An unknown source ranks 0, so anything
# beats it — a stray label cannot pin a row in place.
_RANK_CASE = "CASE prices.source " + " ".join(
    f"WHEN '{name}' THEN {rank}" for name, rank in SOURCE_RANK.items()
) + " ELSE 0 END"

NSE_HOME = "https://www.nseindia.com"
NSE_REFERER = "https://www.nseindia.com/all-reports"
BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{stamp}_F_0000.csv.zip"
)

# Full browser headers: NSE serves 403 to anything that looks automated. The
# homepage GET before the first archive request is what seeds the cookies the
# archive host expects — without it the download 403s even with these headers.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# UDiFF columns. `SctySrs == EQ` drops the SME, trust and debt series; the CM
# segment file also carries ETFs and index products, which FinInstrmTp filters out.
EQUITY_SERIES = "EQ"
EQUITY_INSTRUMENT = "STK"
COLUMN_MAP = {
    "date": "TradDt",
    "symbol": "TckrSymb",
    "open": "OpnPric",
    "high": "HghPric",
    "low": "LwPric",
    "close": "ClsPric",
    "volume": "TtlTradgVol",
}

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0
PAUSE_BETWEEN_DAYS_SECONDS = 1.5

# Above this many missing sessions, fetching a day at a time stops being reasonable
# — a 370-session backfill would be 370 requests against NSE for data yfinance
# returns in one batched call. The bulk goes to the fallback and the most recent
# BHAVCOPY_RECENT_DAYS are then re-fetched from bhavcopy, which upgrades them to the
# authoritative source under the precedence rule in store_bars().
BHAVCOPY_MAX_DAYS = 15
BHAVCOPY_RECENT_DAYS = 10

# How far back to re-offer fallback-filled dates to bhavcopy. Covers a normal
# outage (NSE down for a day or two, or a weekend of blocked requests) without
# re-requesting years of backfilled history on every run.
BHAVCOPY_UPGRADE_WINDOW = 10


class NoSession(Exception):
    """Raised for a date NSE never held a session on — a weekend or a holiday."""


# --- NSE bhavcopy -------------------------------------------------------------


def nse_session():
    """A requests Session primed with NSE's cookies. Build once and reuse."""
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    try:
        # The response itself is unimportant (NSE often answers the bot-detection
        # page here); the Set-Cookie headers are the point, and they arrive either way.
        session.get(NSE_HOME, timeout=config.REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException:
        # A failed prime is not fatal — the archive fetch may still work off a
        # cached cookie, and it will raise its own clear error if not.
        pass
    return session


def fetch_bhavcopy(day, session=None):
    """Parsed bars for one session as {symbol: bar}. Raises NoSession for a
    non-trading day and RuntimeError when NSE will not serve the file."""
    if not calendar.is_trading_day(day):
        raise NoSession(f"{day} is not a session ({calendar.describe(day)})")

    session = session or nse_session()
    url = BHAVCOPY_URL.format(stamp=day.strftime("%Y%m%d"))

    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(BACKOFF_BASE_SECONDS**attempt)
        try:
            response = session.get(
                url, timeout=config.REQUEST_TIMEOUT_SECONDS, headers={"Referer": NSE_REFERER}
            )
        except requests.RequestException as exc:
            last_error = RuntimeError(f"network error: {exc}")
            continue

        # 404 is the exchange saying the file does not exist. On a date the calendar
        # calls a session that means the calendar is wrong or the file is not published
        # yet — either way retrying will not help.
        if response.status_code == 404:
            raise RuntimeError(f"no bhavcopy published for {day} (HTTP 404)")
        if response.status_code == 403:
            last_error = RuntimeError("HTTP 403 — NSE refused the request (bot detection)")
            session = nse_session()  # re-prime and try again
            continue
        if response.status_code >= 400:
            last_error = RuntimeError(f"HTTP {response.status_code}")
            continue

        return parse_bhavcopy(response.content, day)

    raise last_error or RuntimeError(f"bhavcopy fetch failed for {day}")


def parse_bhavcopy(payload, day=None):
    """Extracts equity bars from the zipped UDiFF CSV."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        members = archive.namelist()
        if not members:
            raise RuntimeError("zip contains no files")
        text = archive.read(members[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        # Almost always NSE serving an HTML block page with a 200.
        raise RuntimeError(f"response was not a zip ({exc}) — bot detection?") from exc

    reader = csv.DictReader(text.splitlines())
    missing = [c for c in COLUMN_MAP.values() if c not in (reader.fieldnames or [])]
    if missing:
        raise RuntimeError(f"unexpected bhavcopy columns, missing: {', '.join(missing)}")

    bars = {}
    for row in reader:
        if row.get("SctySrs") != EQUITY_SERIES or row.get("FinInstrmTp") != EQUITY_INSTRUMENT:
            continue
        symbol = row[COLUMN_MAP["symbol"]].strip()
        try:
            bar = {
                "date": row[COLUMN_MAP["date"]].strip(),
                "open": float(row[COLUMN_MAP["open"]]),
                "high": float(row[COLUMN_MAP["high"]]),
                "low": float(row[COLUMN_MAP["low"]]),
                "close": float(row[COLUMN_MAP["close"]]),
                "volume": int(float(row[COLUMN_MAP["volume"]] or 0)),
            }
        except (ValueError, KeyError):
            continue  # a suspended or otherwise blank row
        if is_phantom(bar):
            continue
        bars[symbol] = bar

    if not bars:
        raise RuntimeError("bhavcopy parsed to zero equity rows — format changed?")
    if day and (sample := next(iter(bars.values())))["date"] != day.isoformat():
        raise RuntimeError(f"bhavcopy for {day} contains bars dated {sample['date']}")
    return bars


# --- yfinance fallback --------------------------------------------------------


def is_phantom(bar):
    """A non-session bar dressed as a session one: no volume and no range.

    Yahoo emits exactly this for NSE holidays, carrying the previous close forward.
    Stored, it flattens RSI, contributes a zero to the 20-day average volume, and
    adds a bar that no trade ever happened in.
    """
    if bar["volume"]:
        return False
    return bar["open"] == bar["high"] == bar["low"] == bar["close"]


def fetch_yfinance(symbols, start, end):
    """Batched daily bars as {symbol: [bar, ...]}, oldest first.

    One call for the whole universe rather than one per symbol. `end` is inclusive
    here; yfinance treats its own `end` as exclusive, so it is bumped by a day.
    """
    import yfinance as yf

    tickers = {universe.yahoo_ticker(s): s for s in symbols}
    frame = yf.download(
        list(tickers),
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        # Load-bearing: the default is True, which returns split/dividend-adjusted
        # prices. Bhavcopy carries raw traded prices, so leaving this on would put
        # two incompatible series in the same column.
        auto_adjust=False,
        actions=False,
        group_by="ticker",
        progress=False,
        threads=False,
    )
    if frame is None or frame.empty:
        raise RuntimeError("yfinance returned no data")

    bars = {}
    for ticker, symbol in tickers.items():
        # group_by="ticker" nests columns under the ticker even for a single symbol,
        # so this indexes by ticker regardless of batch size. The flat fallback
        # covers older yfinance versions that collapsed the level for one ticker.
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
                # Rounded because yfinance hands back float32-widened values —
                # 1307.800048828125 where the exchange printed 1307.80. Left alone,
                # a date would change price by a hair when bhavcopy later replaces
                # it, and no comparison between the two sources would ever be exact.
                "open": _f(row.get("Open"), round_to=2),
                "high": _f(row.get("High"), round_to=2),
                "low": _f(row.get("Low"), round_to=2),
                "close": _f(close, round_to=2),
                "volume": int(row.get("Volume") or 0),
            }
            if bar["close"] is None or is_phantom(bar):
                continue
            series.append(bar)
        if series:
            bars[symbol] = series
    return bars


def _f(value, round_to=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return round(value, round_to) if round_to is not None else value


# --- storage ------------------------------------------------------------------


def store_bars(conn, rows):
    """Upserts (symbol, bar, source) tuples under SOURCE_RANK precedence.

    A write lands only when its source ranks at or above the one already stored, so
    a lower-ranked feed can fill a gap but never degrade a bar. Equal rank is allowed
    on purpose: re-running --stage backfill has to be able to replace adjusted bars
    with freshly adjusted ones, which is the whole repair path after a split.

    Expressed in the conflict clause rather than in Python so a concurrent writer
    cannot interleave a read-then-write and lose the rule.
    """
    conn.executemany(
        f"""INSERT INTO prices (symbol, date, open, high, low, close, volume, source)
            VALUES (:symbol, :date, :open, :high, :low, :close, :volume, :source)
            ON CONFLICT (symbol, date) DO UPDATE SET
                open = excluded.open, high = excluded.high, low = excluded.low,
                close = excluded.close, volume = excluded.volume, source = excluded.source
            WHERE :rank >= ({_RANK_CASE})""",
        [
            {**bar, "symbol": symbol, "source": source, "rank": SOURCE_RANK.get(source, 0)}
            for symbol, bar, source in rows
        ],
    )
    conn.commit()
    return len(rows)


def purge_phantom_bars(conn):
    """Removes zero-volume, zero-range bars already stored.

    These predate the is_phantom() check at ingest. Left in place they keep
    depressing average volume and flattening RSI for a year of history.
    """
    cursor = conn.execute(
        "DELETE FROM prices WHERE volume = 0 AND open = high AND high = low AND low = close"
    )
    conn.commit()
    return cursor.rowcount


def dates_needing_upgrade(conn, window_sessions=None):
    """Recent dates still held by the fallback, newest first.

    Without this, the source-precedence rule in store_bars() is unreachable through
    the normal run: once yfinance fills a gap, the stored-date watermark moves past
    it and bhavcopy is never asked for that date again. The requirement is that
    bhavcopy replaces a fallback-filled day when it becomes available, so those days
    have to be deliberately revisited.

    Bounded to a window because the alternative is re-requesting every day of a
    multi-year fallback backfill on every run — hundreds of NSE requests daily to
    re-fetch history that is not going to change.
    """
    window_sessions = window_sessions or BHAVCOPY_UPGRADE_WINDOW
    rows = conn.execute(
        """SELECT DISTINCT date FROM prices
           WHERE source IS NOT ? AND source != ?
           ORDER BY date DESC LIMIT ?""",
        (SOURCE_BHAVCOPY, SOURCE_BHAVCOPY, window_sessions),
    ).fetchall()
    return sorted(date.fromisoformat(row["date"]) for row in rows)


def latest_stored_date(conn, symbols):
    """Newest date that has bars for most of the universe.

    MAX(date) over the whole table would be wrong: one recently-listed symbol with a
    single fresh bar would make every other symbol look up to date. The median is
    robust to both a thin new listing and a symbol that stopped trading.
    """
    rows = conn.execute(
        "SELECT symbol, MAX(date) AS latest FROM prices WHERE symbol IN "
        f"({','.join('?' * len(symbols))}) GROUP BY symbol",
        list(symbols),
    ).fetchall()
    if not rows:
        return None
    latest = sorted(row["latest"] for row in rows if row["latest"])
    return latest[len(latest) // 2] if latest else None


# --- stage --------------------------------------------------------------------


def run(dry_run=False, symbols=None, backfill=False, **kwargs):
    """Brings the universe's bars up to the most recent completed session.

    Exits cleanly with a "no session" line on weekends and holidays — that is the
    normal state of a scheduled job on Sunday, not a failure.
    """
    symbols = tuple(symbols or universe.UNIVERSE)
    wanted = set(symbols)
    today = datetime.now(timezone.utc).date()

    conn = get_connection()
    try:
        init_db(conn)

        removed = purge_phantom_bars(conn)
        if removed:
            print(f"[ingest] purged {removed} phantom bar(s) (zero volume, zero range)")

        latest = None if backfill else latest_stored_date(conn, symbols)
        start = (
            date.fromisoformat(latest) + timedelta(days=1)
            if latest
            else today - timedelta(days=config.INGEST_LOOKBACK_DAYS)
        )

        # Bhavcopy can only be fetched for dates the calendar can classify — asking
        # for a session on a day it cannot rule on would mean guessing whether the
        # exchange was open. Older history stays on whatever source already filled
        # it; each date is still wholly one source, so the no-blending rule holds.
        clamped = max(start, calendar.COVERAGE_START)
        if clamped > start:
            print(
                f"[ingest] range starts {start}, but the holiday calendar only covers "
                f"{calendar.COVERAGE_START} onward — fetching from {clamped}; earlier "
                f"history keeps its existing source"
            )
        start = clamped
        if start > today:
            print("[ingest] nothing to fetch inside the calendar's coverage")
            return 0
        sessions = calendar.trading_days_between(start, min(today, calendar.COVERAGE_END))

        # Dates the fallback filled are re-offered to bhavcopy so the authoritative
        # source can replace them; store_bars() enforces which write wins.
        upgrades = [] if backfill else dates_needing_upgrade(conn)
        upgrades = [d for d in upgrades if d not in set(sessions) and calendar.is_trading_day(d)]
        if upgrades:
            print(
                f"[ingest] {len(upgrades)} date(s) still on the fallback source — "
                f"re-offering to bhavcopy: {upgrades[0]} to {upgrades[-1]}"
            )
            sessions = sorted(set(sessions) | set(upgrades))

        if not sessions:
            reason = calendar.describe(today) or "nothing newer than what is stored"
            print(f"[ingest] no session to ingest — {reason}")
            return 0

        print(f"[ingest] {len(sessions)} session(s) to fetch, {sessions[0]} to {sessions[-1]}")

        stored = 0
        fallback_days = []

        # An automatic run with a long gap goes straight to the batched fallback:
        # fetching 300+ files one at a time from NSE is neither fast nor polite, and
        # nothing chose it deliberately.
        #
        # An explicit --backfill is different — that IS the deliberate choice, and its
        # whole purpose is to replace fallback history with the exchange's own record.
        # Routing it to yfinance would make the flag a no-op against its own name.
        if not backfill and len(sessions) > BHAVCOPY_MAX_DAYS:
            print(
                f"[ingest] {len(sessions)} sessions exceeds the {BHAVCOPY_MAX_DAYS}-day "
                f"per-day limit — using yfinance for the bulk, then bhavcopy for the "
                f"last {BHAVCOPY_RECENT_DAYS} session(s)"
            )
            fallback_days = sessions[:-BHAVCOPY_RECENT_DAYS]
            sessions = sessions[-BHAVCOPY_RECENT_DAYS:]

        session = nse_session()
        for index, day in enumerate(sessions):
            try:
                bars = fetch_bhavcopy(day, session=session)
            except NoSession as exc:
                print(f"[ingest] {day}: {exc}")
                continue
            except Exception as exc:
                if day in set(upgrades):
                    # Already has fallback data; bhavcopy simply is not serving it yet.
                    # Re-queueing would refetch what is already stored, to no effect.
                    print(f"[ingest] {day}: bhavcopy still unavailable ({exc}) — keeping fallback data")
                else:
                    print(f"[ingest] {day}: bhavcopy failed ({exc}) — queued for yfinance")
                    fallback_days.append(day)
                continue

            rows = [(s, bars[s], SOURCE_BHAVCOPY) for s in wanted if s in bars]
            absent = wanted - set(bars)
            if dry_run:
                print(f"[ingest] {day}: {len(rows)} bhavcopy bar(s) (dry run, not stored)")
            else:
                stored += store_bars(conn, rows)
                print(f"[ingest] {day}: {len(rows)} bhavcopy bar(s) stored")
            if absent:
                preview = ", ".join(sorted(absent)[:5])
                print(f"[ingest] {day}: {len(absent)} universe symbol(s) absent from bhavcopy: {preview}")

            if index < len(sessions) - 1:
                time.sleep(PAUSE_BETWEEN_DAYS_SECONDS)

        if fallback_days:
            first, last = min(fallback_days), max(fallback_days)
            print(f"[ingest] yfinance fallback for {len(fallback_days)} session(s), {first} to {last}")
            try:
                series = fetch_yfinance(symbols, first, last)
            except Exception as exc:
                if stored:
                    # Bhavcopy already delivered something; a dead fallback should not
                    # discard it. Report and let the next run retry the gap.
                    print(f"[ingest] yfinance fallback failed ({exc}) — {len(fallback_days)} session(s) still missing")
                    series = {}
                else:
                    raise RuntimeError(f"both sources failed: {exc}") from exc

            allowed = {d.isoformat() for d in fallback_days}
            rows = [
                (symbol, bar, SOURCE_YFINANCE)
                for symbol, bars in series.items()
                for bar in bars
                if bar["date"] in allowed
            ]
            if dry_run:
                print(f"[ingest] {len(rows)} yfinance bar(s) (dry run, not stored)")
            else:
                stored += store_bars(conn, rows)
                print(f"[ingest] {len(rows)} yfinance bar(s) stored")

        by_source = dict(
            conn.execute("SELECT source, COUNT(*) FROM prices GROUP BY source").fetchall()
        )
        print(f"[ingest] {stored} bar(s) written this run; table by source: {by_source}")
        return stored
    finally:
        conn.close()
