"""Data freshness and endpoint reachability — the staleness the messages must show.

STALENESS HAS TO BE VISIBLE OR IT IS WORSE THAN AN OUTAGE

Every stage in this repo degrades rather than stops: ingest falls back to yfinance,
funds shrugs off a dead mfapi.in, the brief still sends when the scan found nothing.
That is right, and it has one failure mode that undoes all of it. A pipeline that
keeps computing on yesterday's bars produces a message indistinguishable from a
healthy one — same shape, same confidence, same rupee figures — while describing a
market that has moved on. Acting on a stale signal is strictly worse than acting on
none, because "none" is self-announcing and stale is not.

So freshness is computed here, once, and every scheduled message carries the line.
Not an alert that fires on a threshold: a permanent statement of what the numbers
were computed from. An alert you have not seen for a month is indistinguishable
from an alert that is broken.

WHAT "EXPECTED" MEANS PER TABLE

  prices       the last completed session, from the holiday calendar. Bhavcopy is
               published after the close, so today only counts once the evening has
               arrived — before that the expectation is the previous session.
  fund_navs    yesterday, in calendar days. Fund calendars are not the equity one
               and not each other's: liquid schemes price on weekends, arbitrage
               ones do not. So the tolerance is wide and per-scheme detail lives in
               the fund digest rather than here.
  signals      the last session, but only when a rule is enabled. With every rule
               disabled an empty signals table is correct, and flagging it as stale
               would be reporting a decision as a fault.

COVERAGE IS SEPARATE FROM FRESHNESS

A session can be present and thin. Partial ingest — a handful of symbols absent from
the bhavcopy — is normal and is not a reason to withhold a scan: the right response
is to compute on what arrived and say which names were missing, because a signal
that never fired on a symbol you were not looking at is a different thing from one
that did not fire.
"""

from datetime import date, datetime, timedelta, timezone

from src import holidays_2026 as calendar, universe
from src.db import get_connection, init_db

# IST hour after which the exchange's file for the day is expected to exist. The
# close is 15:30 and the UDiFF bhavcopy lands within the hour; 18:00 leaves room
# for a late publication without making a healthy evening run look behind.
BHAVCOPY_PUBLISHED_IST_HOUR = 18

# Share of the universe a session must carry before it counts as a usable date to
# compute on. Matches ingest.SESSION_COVERAGE_FLOOR — the same question ("is this
# session complete enough to trust?") should not have two answers.
COVERAGE_FLOOR = 0.9

# Calendar-day tolerance before fund NAVs are called stale. Wide on purpose: a
# business-day-only scheme going into a long weekend is three days behind while
# working perfectly.
NAV_STALE_DAYS = 4

# How many symbols to name before summarising. A footer that lists forty tickers
# is one nobody reads to the end of, which defeats the point of putting it there.
MAX_NAMED_SYMBOLS = 6


def _ist_now():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def expected_session(now=None):
    """The most recent session whose bars ought to exist by now.

    Returns None outside the calendar's coverage rather than guessing — the
    alternative is inventing an expectation and then reporting a fault against it.
    """
    now = now or _ist_now()
    day = now.date()
    if calendar.covers(day) and calendar.is_trading_day(day):
        if now.hour < BHAVCOPY_PUBLISHED_IST_HOUR:
            day -= timedelta(days=1)
    for _ in range(14):
        if not calendar.covers(day):
            return None
        if calendar.is_trading_day(day):
            return day.isoformat()
        day -= timedelta(days=1)
    return None


def sessions_between(start, end):
    """Trading sessions strictly after `start`, up to and including `end`."""
    if not start or not end:
        return 0
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    if end_date <= start_date:
        return 0
    if not (calendar.covers(start_date) and calendar.covers(end_date)):
        return (end_date - start_date).days
    return len(calendar.trading_days_between(start_date + timedelta(days=1), end_date))


def _rosters(conn, symbols, limit=12):
    """{date: set(symbols)} for the newest stored sessions."""
    placeholders = ",".join("?" * len(symbols))
    days = [
        r[0] for r in conn.execute(
            f"SELECT DISTINCT date FROM prices WHERE symbol IN ({placeholders}) "
            "ORDER BY date DESC LIMIT ?",
            [*symbols, limit],
        )
    ]
    if not days:
        return {}
    day_slots = ",".join("?" * len(days))
    out = {day: set() for day in days}
    for row in conn.execute(
        f"SELECT date, symbol FROM prices WHERE date IN ({day_slots}) "
        f"AND symbol IN ({placeholders})",
        [*days, *symbols],
    ):
        out[row["date"]].add(row["symbol"])
    return out


def session_coverage(conn, symbols=None):
    """{date: (present, expected)} for the newest few sessions.

    Expected comes from the PREVIOUS session's roster, not from the symbols whose
    stored history brackets this date. The bracketing version — which ingest.py uses
    correctly for finding old partial holes — is exactly wrong for the newest
    session: a symbol absent today has its span ending yesterday, so it excludes
    itself from its own expectation and eleven bars score as 100% coverage.

    "These names traded last session; did they arrive this one?" is the question
    that actually detects a partial ingest.
    """
    symbols = tuple(symbols or universe.UNIVERSE)
    rosters = _rosters(conn, symbols)
    if not rosters:
        return {}

    ordered = sorted(rosters, reverse=True)
    out = {}
    for index, day in enumerate(ordered):
        present = len(rosters[day])
        # The oldest session in the window has no predecessor to compare against;
        # measuring it against itself is the honest answer rather than a guess.
        previous = ordered[index + 1] if index + 1 < len(ordered) else None
        expected = len(rosters[previous]) if previous else present
        out[day] = (present, expected)
    return out


def missing_symbols(conn, day, symbols=None):
    """Universe symbols with no bar on `day`, oldest-listed first.

    Only symbols whose stored history brackets the date: a name that has never
    traded in the window is not missing from it.
    """
    if not day:
        return []
    symbols = tuple(symbols or universe.UNIVERSE)
    rosters = _rosters(conn, symbols)
    present = rosters.get(day, set())
    older = sorted((d for d in rosters if d < day), reverse=True)
    if not older:
        # No previous session stored, so nothing establishes what should have been
        # here. Reporting the whole universe as missing would be a fabrication.
        return []
    return sorted(rosters[older[0]] - present)


def data_through(conn, symbols=None):
    """The newest session complete enough to compute on, and why it is that one.

    This is the date the brief must name. It is deliberately not MAX(date): a
    session that arrived for eleven symbols is not a session, and computing a scan
    on it would produce signals for whichever names happened to be in the file.
    """
    coverage = session_coverage(conn, symbols)
    if not coverage:
        return None, "no price bars stored"
    for day in sorted(coverage, reverse=True):
        present, expected = coverage[day]
        if expected and present >= expected * COVERAGE_FLOOR:
            return day, f"{present}/{expected} symbols"
    newest = max(coverage)
    present, expected = coverage[newest]
    return None, f"newest session {newest} has only {present}/{expected} symbols"


def price_status(conn, symbols=None, now=None):
    """Freshness of the price table against the calendar's expectation."""
    usable, detail = data_through(conn, symbols)
    expected = expected_session(now)
    behind = sessions_between(usable, expected) if usable and expected else None
    return {
        "table": "prices",
        "latest": usable,
        "expected": expected,
        "behind": behind,
        "stale": bool(behind),
        "detail": detail,
    }


def nav_status(conn, now=None):
    row = conn.execute("SELECT MAX(date) FROM fund_navs").fetchone()
    latest = row[0] if row else None
    now = now or _ist_now()
    expected = (now.date() - timedelta(days=1)).isoformat()
    behind = (now.date() - date.fromisoformat(latest)).days if latest else None
    return {
        "table": "fund_navs",
        "latest": latest,
        "expected": expected,
        "behind": behind,
        # Calendar days, not sessions: fund schedules differ from the equity one and
        # from each other, so this tolerance is wide by design.
        "stale": behind is not None and behind > NAV_STALE_DAYS,
        "detail": f"{behind} day(s) old" if behind is not None else "empty",
    }


def signal_status(conn, now=None):
    """Signals are only expected when a rule can produce them."""
    from src import signals

    row = conn.execute("SELECT MAX(date) FROM signals").fetchone()
    latest = row[0] if row else None
    expected = expected_session(now)

    if not signals.ENABLED_RULES:
        return {
            "table": "signals", "latest": latest, "expected": None, "behind": None,
            "stale": False,
            "detail": f"all {len(signals.RULES)} rules disabled — none expected",
        }
    behind = sessions_between(latest, expected) if latest and expected else None
    return {
        "table": "signals", "latest": latest, "expected": expected, "behind": behind,
        "stale": bool(behind),
        "detail": "scan has not run" if not latest else f"{behind or 0} session(s) behind",
    }


def freshness(conn, symbols=None, now=None):
    """Every table's freshness, in the order they are produced."""
    return [price_status(conn, symbols, now), nav_status(conn, now), signal_status(conn, now)]


# --- the one line every scheduled message carries -----------------------------


def staleness_note(conn, symbols=None, now=None):
    """The sentence a report puts under its own numbers, or None when current.

    Phrased as what the data IS rather than as a warning. "Signals based on data
    through 2026-07-29" survives being skimmed; "WARNING: stale data" is read once
    and thereafter pattern-matched as decoration.
    """
    prices = price_status(conn, symbols, now)
    if not prices["latest"]:
        return f"No usable price data: {prices['detail']}. Nothing below is computed from bars."
    if prices["stale"]:
        return (
            f"Based on data through {prices['latest']}, "
            f"{prices['behind']} session(s) behind the expected {prices['expected']}. "
            "Ingest has not caught up, so these are not today's numbers."
        )
    return None


def coverage_note(conn, day=None, symbols=None):
    """Which universe symbols were absent from the session that was computed on."""
    if day is None:
        day, _ = data_through(conn, symbols)
    if not day:
        return None
    absent = missing_symbols(conn, day, symbols)
    if not absent:
        return None
    named = ", ".join(absent[:MAX_NAMED_SYMBOLS])
    more = f" and {len(absent) - MAX_NAMED_SYMBOLS} more" if len(absent) > MAX_NAMED_SYMBOLS else ""
    return (
        f"{len(absent)} symbol(s) had no bar on {day} and were not scanned: {named}{more}."
    )


def footer(conn=None, symbols=None, now=None, hours=24, include_staleness=True):
    """The health block appended to every scheduled message.

    Assembled here rather than in each report so the four messages cannot drift into
    saying different things about the same database.

    `include_staleness=False` is for the brief, which prints that sentence under its
    own header where the reader cannot skim past it. Saying it twice in one message
    is how a line stops being read at all — the repetition reads as boilerplate,
    which is exactly the fate this line must avoid.
    """
    from src import runlog

    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        init_db(conn)
        lines = []
        stale = staleness_note(conn, symbols, now)
        if stale and include_staleness:
            lines.append(stale)
        gaps = coverage_note(conn, symbols=symbols)
        if gaps:
            lines.append(gaps)

        run_line = runlog.health_line(hours, conn=conn)
        if run_line:
            lines.append(run_line)

        # The calendar's hard stop, once it is close enough to act on. Carried in
        # every scheduled message rather than only in the weekly doctor, because
        # doctor speaks on failure and this needs saying *before* it becomes one.
        expiry = calendar.expiry_warning()
        if expiry:
            lines.append(expiry)

        if not stale:
            prices = price_status(conn, symbols, now)
            if prices["latest"]:
                lines.append(f"Data current through {prices['latest']} ({prices['detail']}).")
        return "\n".join(lines) if lines else None
    finally:
        if owns_conn:
            conn.close()


# --- endpoint probes (doctor) --------------------------------------------------


def probe(name, fn, timeout_note=None):
    """Run one endpoint check, converting any failure into a reportable row."""
    try:
        return (name, "OK", fn())
    except Exception as exc:
        detail = str(exc).split("(")[0][:90] or exc.__class__.__name__
        return (name, "FAIL", f"{detail}{f' ({timeout_note})' if timeout_note else ''}")
