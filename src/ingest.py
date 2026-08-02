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

Adjustment bases are never mixed within a date. Which source wins is SOURCE_RANK,
and split-adjusted history outranks even the exchange's own file — see the comment
there for why a raw series cannot back a lookback window.

This module also recovers sessions the price feed simply lacks. yfinance emits a
zero-volume flat bar for a session it has no data for, byte-identical to the
placeholder it emits for a genuine holiday; is_phantom() rejects both, correctly,
which means the feed itself can never tell us a trading day went missing. Only the
exchange can, so fill_gaps() asks it. See fill_session() for why those recovered
bars are rescaled rather than stored raw.
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
# Bars recovered from bhavcopy for a session the adjusted feed lacks, rescaled onto
# the adjusted basis. Same rank as SOURCE_ADJUSTED because it is the same basis —
# the label records where the numbers came from, not how they compare.
SOURCE_BHAVCOPY_ADJUSTED = "bhavcopy-adj"

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
    SOURCE_BHAVCOPY_ADJUSTED: 3,
}

# What a source says about comparability. Two sources sharing a basis can sit in
# one series; two bases cannot. This is what the integrity check actually cares
# about — the source label is only a provenance note.
SOURCE_BASIS = {
    SOURCE_YFINANCE: "raw",
    SOURCE_BHAVCOPY: "raw",
    SOURCE_ADJUSTED: "adjusted",
    SOURCE_BHAVCOPY_ADJUSTED: "adjusted",
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
    # Only consult the calendar for the year it covers. Outside that the archive
    # itself is the arbiter — a 404 means there was no session — and refusing to ask
    # would make older gaps permanently unrecoverable.
    if calendar.covers(day) and not calendar.is_trading_day(day):
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
    before = conn.total_changes
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
    # Rows actually written, not rows offered. The conflict clause silently drops a
    # write that loses on rank, so len(rows) would report imaginary work — which is
    # exactly how "100 bars stored" gets printed for a batch that stored nothing.
    return conn.total_changes - before


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
    # Only dates bhavcopy could actually win. Offering it a date already held by a
    # higher-ranked source is a guaranteed-rejected write, and re-fetching those
    # every run means ten pointless NSE requests a day.
    beatable = [name for name, rank in SOURCE_RANK.items() if rank < SOURCE_RANK[SOURCE_BHAVCOPY]]
    if not beatable:
        return []
    rows = conn.execute(
        f"""SELECT DISTINCT date FROM prices
            WHERE source IN ({','.join('?' * len(beatable))})
            ORDER BY date DESC LIMIT ?""",
        (*beatable, window_sessions),
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


# --- gap filling --------------------------------------------------------------

# A session is incomplete when fewer than this share of the symbols that were
# listed at the time have a bar. Well below 1.0 so a single genuinely suspended
# stock does not trigger a refetch of the whole day.
SESSION_COVERAGE_FLOOR = 0.9

# Bounded per run: each gap costs two NSE requests, and an unbounded loop over a
# long history would hammer the archive on a schedule.
MAX_GAP_FILLS = 5


def incomplete_sessions(conn, symbols, floor=SESSION_COVERAGE_FLOOR):
    """Stored dates whose coverage is short, newest first.

    Expected coverage is per-date: only symbols whose own history brackets the date
    are counted, so a 2022 session is not marked incomplete for lacking a company
    that listed in 2024.

    This catches the partial holes. A session missing for *every* symbol leaves no
    row to count and is invisible here — that one is found by comparing stored dates
    against the calendar, which run() does separately.
    """
    spans = {
        r["symbol"]: (r["lo"], r["hi"])
        for r in conn.execute(
            "SELECT symbol, MIN(date) lo, MAX(date) hi FROM prices "
            f"WHERE symbol IN ({','.join('?' * len(symbols))}) GROUP BY symbol",
            list(symbols),
        )
    }
    actual = dict(
        conn.execute(
            "SELECT date, COUNT(*) FROM prices "
            f"WHERE symbol IN ({','.join('?' * len(symbols))}) GROUP BY date",
            list(symbols),
        ).fetchall()
    )

    short = []
    for day, count in actual.items():
        expected = sum(1 for lo, hi in spans.values() if lo and lo <= day <= hi)
        if expected and count < expected * floor:
            short.append((day, count, expected))
    return sorted(short, reverse=True)


def _adjustment_ratios(conn, raw_bars, reference_day):
    """Per-symbol adjusted/raw ratio measured on `reference_day`.

    Bhavcopy carries raw traded prices. Dropping those straight into an adjusted
    series would put a step wherever the symbol has since split — filling a 2025 gap
    for KOTAKBANK with a raw bar would leave it five times its neighbours. So the
    ratio is measured against a session where both a raw and a stored adjusted price
    exist, and the recovered bars are scaled by it.

    Measured per symbol, because each has its own split history, and on the nearest
    session, so no corporate action falls between the two dates.
    """
    stored = {
        r["symbol"]: r["close"]
        for r in conn.execute(
            "SELECT symbol, close FROM prices WHERE date = ?", (reference_day.isoformat(),)
        )
    }
    ratios = {}
    for symbol, bar in raw_bars.items():
        adjusted, raw = stored.get(symbol), bar["close"]
        if adjusted and raw:
            ratios[symbol] = adjusted / raw
    return ratios


def fill_session(conn, day, symbols, session=None, dry_run=False):
    """Recovers one session from bhavcopy, rescaled onto the adjusted basis.

    Returns (stored, skipped_symbols). Raises when the reference session needed for
    the ratio cannot be fetched — filling without it would silently write raw prices
    into an adjusted series, which is worse than leaving the gap.
    """
    session = session or nse_session()
    wanted = set(symbols)

    target = fetch_bhavcopy(day, session=session)
    time.sleep(PAUSE_BETWEEN_DAYS_SECONDS)

    # Nearest stored session after `day`, for the ratio.
    row = conn.execute("SELECT MIN(date) FROM prices WHERE date > ?", (day.isoformat(),)).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"no later session stored to measure an adjustment ratio against")
    reference = date.fromisoformat(row[0])

    reference_raw = fetch_bhavcopy(reference, session=session)
    ratios = _adjustment_ratios(conn, reference_raw, reference)
    if not ratios:
        raise RuntimeError(f"no overlapping symbols on {reference} to measure a ratio")

    rows, skipped = [], []
    for symbol in sorted(wanted & set(target)):
        ratio = ratios.get(symbol)
        if ratio is None:
            skipped.append(symbol)
            continue
        bar = target[symbol]
        rows.append(
            (
                symbol,
                {
                    "date": bar["date"],
                    "open": round(bar["open"] * ratio, 2),
                    "high": round(bar["high"] * ratio, 2),
                    "low": round(bar["low"] * ratio, 2),
                    "close": round(bar["close"] * ratio, 2),
                    # Volume is a share count, not a price — a split changes it too,
                    # but the adjusted feed's own volumes are left untouched by
                    # yfinance, so leaving this raw keeps it consistent with them.
                    "volume": bar["volume"],
                },
                SOURCE_BHAVCOPY_ADJUSTED,
            )
        )

    spread = sorted(ratios.values())
    median_ratio = spread[len(spread) // 2]
    if dry_run:
        print(f"[ingest] {day}: {len(rows)} bar(s) recoverable (dry run), median ratio {median_ratio:.4f}")
        return 0, skipped
    stored = store_bars(conn, rows)
    print(
        f"[ingest] {day}: recovered {stored} bar(s) from bhavcopy, rescaled via {reference} "
        f"(median ratio {median_ratio:.4f})"
    )
    return stored, skipped


def fill_gaps(conn, symbols, session=None, dry_run=False, limit=MAX_GAP_FILLS):
    """Recovers sessions the price feed dropped, from the exchange's own file.

    Two shapes, found two ways. A partially missing session still has rows, so it is
    found by coverage. A wholly missing one leaves nothing to count, so it is found
    by asking the calendar which sessions should exist and diffing against what does.

    The wholly-missing case is not hypothetical: yfinance emits a zero-volume flat
    bar for a session it lacks, which is byte-identical to the placeholder it emits
    for a real holiday. is_phantom() rejects both, correctly — and the price feed
    therefore cannot tell us the difference. Only the exchange can.
    """
    session = session or nse_session()
    gaps = []

    for day, count, expected in incomplete_sessions(conn, symbols):
        parsed = date.fromisoformat(day)
        if calendar.covers(parsed) and calendar.is_trading_day(parsed):
            gaps.append((parsed, f"{count}/{expected} symbols"))
        elif not calendar.covers(parsed):
            # Outside the calendar's year the exchange file is still the arbiter;
            # a 404 below simply means it was not a session after all.
            gaps.append((parsed, f"{count}/{expected} symbols, outside calendar"))

    stored_dates = {
        r[0] for r in conn.execute("SELECT DISTINCT date FROM prices WHERE date >= ?",
                                   (calendar.COVERAGE_START.isoformat(),))
    }
    today = datetime.now(timezone.utc).date()
    for day in calendar.trading_days_between(calendar.COVERAGE_START, min(today, calendar.COVERAGE_END)):
        if day.isoformat() not in stored_dates:
            gaps.append((day, "no bars at all"))

    gaps = sorted(set(gaps), reverse=True)[:limit]
    if not gaps:
        return 0

    print(f"[ingest] {len(gaps)} incomplete session(s) to recover from bhavcopy")
    total = 0
    for day, why in gaps:
        try:
            recovered, skipped = fill_session(conn, day, symbols, session=session, dry_run=dry_run)
            total += recovered
            if skipped:
                print(f"[ingest]   {day}: {len(skipped)} symbol(s) had no ratio reference: "
                      f"{', '.join(skipped[:5])}")
        except NoSession:
            print(f"[ingest]   {day}: not a session after all ({why})")
        except Exception as exc:
            print(f"[ingest]   {day}: could not recover ({exc}) [{why}]")
        time.sleep(PAUSE_BETWEEN_DAYS_SECONDS)
    return total


# --- stage --------------------------------------------------------------------


def run(dry_run=False, symbols=None, backfill=False, require_bhavcopy=False, **kwargs):
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

        if fallback_days and require_bhavcopy:
            missing = ", ".join(str(d) for d in sorted(fallback_days)[:5])
            raise RuntimeError(
                f"bhavcopy unavailable for {len(fallback_days)} session(s) ({missing}) "
                f"and the yfinance fallback is suppressed — retry later, or drop "
                f"require_bhavcopy to accept the fallback"
            )

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

        stored += fill_gaps(conn, symbols, session=session, dry_run=dry_run)

        by_source = dict(
            conn.execute("SELECT source, COUNT(*) FROM prices GROUP BY source").fetchall()
        )
        print(f"[ingest] {stored} bar(s) written this run; table by source: {by_source}")
        return stored
    finally:
        conn.close()
