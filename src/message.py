"""Titles for the scheduled messages, in one place.

Four different reports arrive in the same Telegram thread, some of them twice a
day, and until now each opened with its own terse variant — "nse-assist brief",
"nse-assist weekly", "nse-assist —". Scrolling back a week, they were hard to tell
apart, and the bare ISO date gave no clue which run produced a message or whether
it was the one you already read.

So: one helper, one shape. Kind first because that is what you are scanning for,
then the day written out, then the IST clock time — which is the thing that says
whether a report arrived when it was supposed to.

    NSE-ASSIST · EVENING REPORT
    Monday 3 August 2026 · 19:31 IST

Deliberately not emoji-prefixed and deliberately not shouting. The tone rules that
apply to the body apply to the header: it identifies, it does not sell.
"""

from datetime import datetime, timedelta, timezone

EVENING = "Evening report"
MORNING = "Morning brief"
WEEKLY = "Weekly review"
GATE = "Evaluation gate"
FUNDS = "Parked cash"

_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def ist_now():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def long_date(day):
    """`Monday 3 August 2026` — spelled out, and without a leading zero.

    Built by hand rather than with strftime: `%-d` is a GNU extension that is not
    portable, and `%d` gives "03 August" which reads like a form field.
    """
    return f"{_DAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]} {day.year}"


def title(kind, when=None):
    """The two-line header every scheduled message opens with."""
    now = when or ist_now()
    return f"NSE-ASSIST · {kind.upper()}\n{long_date(now)} · {now.strftime('%H:%M')} IST"
