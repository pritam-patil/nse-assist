"""NSE trading holidays for calendar 2026 — a committed constant.

Source: NSE's own holiday-master API, capital-market ("CM") segment:

    https://www.nseindia.com/api/holiday-master?type=trading

Committed rather than fetched at runtime for the same reason as the universe: the
ingest stage decides "was there a session on this date?" and that answer must not
change because an API was briefly unreachable. A wrong "no session" silently skips
a day of bars; a wrong "session" makes ingest chase a file that will never exist.

NSE publishes the next year's calendar in December. This file covers 2026 ONLY,
and `assert_covers()` raises for any date outside it rather than defaulting to
"open" — an uncovered year would otherwise turn every holiday into a phantom
trading day, which is exactly the failure this module exists to prevent.

Dates that fall on a weekend are kept as published; they are redundant against the
weekday check but make the list diffable against NSE's page.
"""

from datetime import date

YEAR = 2026

# (ISO date, NSE's own description) — kept as published so a diff against the
# source page is a straight comparison.
TRADING_HOLIDAYS = (
    ("2026-01-15", "Municipal Corporation Election - Maharashtra"),
    ("2026-01-26", "Republic Day"),
    ("2026-02-15", "Mahashivratri"),
    ("2026-03-03", "Holi"),
    ("2026-03-21", "Id-Ul-Fitr (Ramadan Eid)"),
    ("2026-03-26", "Shri Ram Navami"),
    ("2026-03-31", "Shri Mahavir Jayanti"),
    ("2026-04-03", "Good Friday"),
    ("2026-04-14", "Dr. Baba Saheb Ambedkar Jayanti"),
    ("2026-05-01", "Maharashtra Day"),
    ("2026-05-28", "Bakri Id"),
    ("2026-06-26", "Muharram"),
    ("2026-08-15", "Independence Day"),
    ("2026-09-14", "Ganesh Chaturthi"),
    ("2026-10-02", "Mahatma Gandhi Jayanti"),
    ("2026-10-20", "Dussehra"),
    ("2026-11-08", "Diwali Laxmi Pujan"),
    ("2026-11-10", "Diwali-Balipratipada"),
    ("2026-11-24", "Prakash Gurpurb Sri Guru Nanak Dev"),
    ("2026-12-25", "Christmas"),
)

HOLIDAY_DATES = frozenset(day for day, _ in TRADING_HOLIDAYS)
HOLIDAY_NAMES = dict(TRADING_HOLIDAYS)

# Diwali Muhurat trading is a special ~1 hour evening session. NSE publishes a
# bhavcopy for it, but it is not a normal session and its thin, unrepresentative
# bars would distort every indicator that assumes a full day. Excluded via the
# holiday list above (08-Nov falls on a Sunday in 2026 anyway).


def assert_covers(day):
    """Raise if `day` is outside the year this file describes.

    Deliberately loud. Silently answering "not a holiday" for 2027 would make every
    2027 holiday look like a session with a missing bhavcopy, and ingest would retry
    a file that is never going to exist.
    """
    if day.year != YEAR:
        raise RuntimeError(
            f"{day} is outside {YEAR}: src/holidays_{YEAR}.py needs replacing with the "
            f"calendar NSE publishes for {day.year} "
            f"(https://www.nseindia.com/api/holiday-master?type=trading)"
        )


def is_holiday(day):
    assert_covers(day)
    return day.isoformat() in HOLIDAY_DATES


def is_trading_day(day):
    """True when NSE's cash market holds a normal session on `day`."""
    assert_covers(day)
    if day.weekday() >= 5:  # Saturday, Sunday
        return False
    return day.isoformat() not in HOLIDAY_DATES


def describe(day):
    """Why `day` is not a session, or None when it is one."""
    if day.weekday() >= 5:
        return "weekend"
    name = HOLIDAY_NAMES.get(day.isoformat())
    return f"holiday: {name}" if name else None


def trading_days_between(start, end):
    """Every session from `start` to `end` inclusive, oldest first."""
    from datetime import timedelta

    days, current = [], start
    while current <= end:
        if is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def assert_consistent():
    """Guards the edits that quietly break a calendar: a duplicate date, a date from
    the wrong year, or an unparseable one. Called by the doctor stage."""
    seen = [day for day, _ in TRADING_HOLIDAYS]
    duplicates = sorted({d for d in seen if seen.count(d) > 1})
    if duplicates:
        raise RuntimeError(f"duplicate holidays: {', '.join(duplicates)}")
    for day in seen:
        parsed = date.fromisoformat(day)
        if parsed.year != YEAR:
            raise RuntimeError(f"{day} is not in {YEAR}")
    weekday_holidays = sum(1 for d in seen if date.fromisoformat(d).weekday() < 5)
    return f"{len(seen)} holidays in {YEAR}, {weekday_holidays} on weekdays"
