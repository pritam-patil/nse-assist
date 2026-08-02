"""The /whatif command — what a holding period actually did, over and over.

    /whatif 119091 6 25000

Read as: "if I had parked ₹25,000 in scheme 119091 for 6 weeks, how did that turn
out?" The answer is every 6-week window in the past three years, in rupees:
median, best, worst, and how often it lost money.

THIS IS DESCRIPTIVE STATISTICS, NOT A FORECAST

Nothing here is modelled, fitted or projected. It is a count of what already
happened, and the reply says so in those words. The distinction matters because
the output looks exactly like a forecast — a number in rupees attached to a
holding period you are considering — and the only thing keeping it honest is that
it is labelled.

NET OF NOTHING

NAV is already net of the expense ratio, so that much is included whether we like
it or not. Everything else is not: no exit load, no securities transaction tax on
the redemption, no income tax on the gain, no cost of the money being unavailable
meanwhile. A real ₹25,000 for six weeks keeps less than the median below.

OVERLAPPING WINDOWS ARE NOT INDEPENDENT

Six-week windows starting on consecutive days share five weeks and six days of
their history. Two hundred of them is not two hundred independent trials, and the
worst case is very often one bad fortnight showing up in forty windows in a row.
The reply states the count and the caveat together, because the count alone reads
as a much larger sample than it is.
"""

import statistics
from datetime import date, timedelta

from src import config, deliver, funds, fund_watchlist
from src.db import get_connection, get_state, init_db, set_state

COMMAND = "/whatif"

# Three years, as specified. Windows must fit ENTIRELY inside it: a 12-week window
# ending three years ago began three years and three months ago, which is outside
# the span the reply claims to describe.
LOOKBACK_DAYS = 3 * 365

# Below this the quantiles are theatre. Six windows have a median that moves by a
# third when one of them changes, and a "worst case" that is simply the worst of
# six days picked by the calendar.
MIN_WINDOWS = 12

# 1 to 104 weeks. Above two years there is no room left for a second window inside
# a three-year span, so the "distribution" would be a single number.
MIN_WEEKS = 1
MAX_WEEKS = 104

# Not a risk limit — those live in risk_config. This only catches a fat-fingered
# amount, where the reply would otherwise report crores with a straight face.
MIN_AMOUNT = 1.0
MAX_AMOUNT = 1_00_00_000.0

OFFSET_KEY = "telegram_update_offset"


def usage():
    codes = "\n".join(
        f"  {code}  {fund_watchlist.label_for(code)}" for code in fund_watchlist.SCHEME_CODES
    )
    return (
        f"Usage: {COMMAND} SCHEMECODE WEEKS AMOUNT\n"
        f"Example: {COMMAND} 119091 6 25000\n\n"
        f"WEEKS {MIN_WEEKS}-{MAX_WEEKS}, AMOUNT in rupees.\n\n"
        f"Schemes with stored history:\n{codes}\n\n"
        "Answers with how every window of that length behaved over the past three "
        "years. History only — not a forecast."
    )


class CommandError(ValueError):
    """A malformed command. The message is shown to the user, so it says what to fix."""


def _parse_amount(raw):
    """Rupees from what a person types on a phone: 25000, 25,000, ₹25000, 25000.50."""
    cleaned = raw.replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    try:
        amount = float(cleaned)
    except ValueError:
        raise CommandError(f"AMOUNT must be a number in rupees, got {raw!r}.")
    if not MIN_AMOUNT <= amount <= MAX_AMOUNT:
        raise CommandError(
            f"AMOUNT must be between {MIN_AMOUNT:,.0f} and {MAX_AMOUNT:,.0f} rupees, got {amount:,.0f}."
        )
    return amount


def parse_command(text):
    """(scheme_code, weeks, amount) — or CommandError with something actionable.

    Tolerates the /whatif@botname form, which is what Telegram sends when the
    command is typed in a group rather than a direct chat.
    """
    parts = (text or "").split()
    if not parts:
        raise CommandError("Empty command.")

    head = parts[0].split("@")[0].lower()
    if head != COMMAND:
        raise CommandError(f"Unknown command {parts[0]!r}.")

    args = parts[1:]
    if len(args) != 3:
        raise CommandError(
            f"{COMMAND} takes exactly 3 arguments (SCHEMECODE WEEKS AMOUNT), got {len(args)}."
        )

    code, raw_weeks, raw_amount = args

    if not code.isdigit():
        raise CommandError(f"SCHEMECODE must be a numeric AMFI code, got {code!r}.")
    if code not in fund_watchlist.SCHEME_CODES:
        raise CommandError(
            f"Scheme {code} is not on the watchlist, so no NAV history is stored for it. "
            "Add it to src/fund_watchlist.py and run --stage funds --history first."
        )

    try:
        weeks = int(raw_weeks)
    except ValueError:
        raise CommandError(f"WEEKS must be a whole number, got {raw_weeks!r}.")
    if not MIN_WEEKS <= weeks <= MAX_WEEKS:
        raise CommandError(f"WEEKS must be between {MIN_WEEKS} and {MAX_WEEKS}, got {weeks}.")

    return code, weeks, _parse_amount(raw_amount)


def distribution(navs, weeks, amount, as_of=None):
    """Every `weeks`-long window inside the past three years, valued in rupees.

    Returns None when there is not enough history to say anything — never a
    distribution of four numbers dressed up as one.
    """
    days = weeks * 7
    # The window itself has to fit inside the three years, so the space of window
    # END dates is what is left over after subtracting its length.
    lookback = LOOKBACK_DAYS - days
    if lookback < 0:
        return None

    returns = funds.rolling_period_returns(navs, as_of, days, lookback=lookback)
    if len(returns) < MIN_WINDOWS:
        return None

    rupees = sorted(amount * r for r in returns)
    return {
        "windows": len(returns),
        "weeks": weeks,
        "amount": amount,
        "median": statistics.median(rupees),
        "median_pct": statistics.median(returns),
        "best": rupees[-1],
        "best_pct": max(returns),
        "worst": rupees[0],
        "worst_pct": min(returns),
        "negative_share": sum(1 for r in returns if r < 0) / len(returns),
    }


def _span(navs, weeks, as_of=None):
    """The observation dates the windows were drawn from, for the reply header."""
    end = funds.nav_at_or_before(navs, as_of)
    if not end:
        return None, None
    lookback = LOOKBACK_DAYS - weeks * 7
    start = (date.fromisoformat(end[0]) - timedelta(days=lookback + weeks * 7)).isoformat()
    return max(start, navs[0][0]), end[0]


def answer(conn, text, as_of=None):
    """The reply to one command. Never raises for user input — malformed is a reply."""
    try:
        code, weeks, amount = parse_command(text)
    except CommandError as exc:
        return f"{exc}\n\n{usage()}"

    navs = funds.load_navs(conn, code, as_of=as_of)
    label = fund_watchlist.label_for(code)

    if not navs:
        return (
            f"No NAV history stored for {label} ({code}).\n"
            "Run: python main.py --stage funds --history"
        )

    stats = distribution(navs, weeks, amount, as_of=as_of)
    if not stats:
        first, last = navs[0][0], navs[-1][0]
        return (
            f"{label}\nNot enough history for a {weeks}-week window: fewer than "
            f"{MIN_WINDOWS} complete windows fit inside the past three years. "
            f"Stored NAVs run {first} to {last} ({len(navs)} observations).\n"
            "A handful of windows has a median that moves by a third when one of "
            "them changes, so nothing is reported rather than a number that looks firm."
        )

    start, end = _span(navs, weeks, as_of)
    # Only the ones inside the reported span. HDFC Liquid restated in 2015 and a
    # three-year window cannot reach it, so naming it there is a caveat about
    # nothing — and a caveat about nothing teaches the reader to skip the caveats.
    restatements = [d for d in funds.find_restatements(navs) if start <= d <= end]

    lines = [
        f"{label}",
        f"{weeks} week{'s' if weeks != 1 else ''} holding {amount:,.0f} rupees",
        f"{stats['windows']} windows, {start} to {end}",
        "",
        f"  median   {stats['median']:>+12,.0f}   ({stats['median_pct']:+.2%})",
        f"  best     {stats['best']:>+12,.0f}   ({stats['best_pct']:+.2%})",
        f"  worst    {stats['worst']:>+12,.0f}   ({stats['worst_pct']:+.2%})",
        f"  negative {stats['negative_share']:>12.1%}   of windows ended below where they started",
        "",
        "This counts what already happened. It is not a forecast, and a window "
        "that has never lost money is not a window that cannot.",
        f"Windows overlap: two consecutive ones share all but a day of their "
        f"history, so {stats['windows']} of them is far fewer than "
        f"{stats['windows']} independent observations.",
        "Net of nothing. NAV is already after the expense ratio, but exit load, "
        "tax on the gain and the cost of the money being tied up are all excluded.",
    ]

    if restatements:
        lines.append(
            f"Windows crossing {', '.join(restatements)} are omitted: the NAV series "
            "changes unit face value there, and their ratio would measure the "
            "restatement rather than a return."
        )

    return "\n".join(lines)


# --- polling ------------------------------------------------------------------


def _load_offset(conn):
    raw = get_state(conn, OFFSET_KEY)
    return int(raw) if raw and raw.lstrip("-").isdigit() else None


def _authorised(chat_id):
    """Only the configured chat is answered.

    A bot token is a URL anyone can talk to once they know it, and every reply this
    stage sends costs an AMFI-derived computation and a Telegram call. Silently
    ignoring the rest is right: replying "not authorised" confirms the bot exists
    to whoever is probing it.
    """
    return str(chat_id) == str(config.TELEGRAM_CHAT_ID)


def handle_message(conn, message, as_of=None, dry_run=False):
    """One update. Returns a log line, or None if it was not for us."""
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()

    if not text.startswith("/"):
        return None
    if not _authorised(chat_id):
        return f"ignored a command from unauthorised chat {chat_id}"

    head = text.split()[0].split("@")[0].lower()
    if head in ("/start", "/help"):
        reply = usage()
    elif head == COMMAND:
        reply = answer(conn, text, as_of=as_of)
    else:
        # Unknown commands get the usage text rather than silence: from the phone
        # end, a typo and a broken bot look identical.
        reply = f"Unknown command {head}.\n\n{usage()}"

    if dry_run:
        print(f"[poll] would reply to {chat_id}:\n{reply}\n")
    else:
        deliver.send_reply(chat_id, reply, reply_to_message_id=message.get("message_id"))
    return f"{head} from {chat_id} -> {len(reply)} chars"


def run(dry_run=False, **kwargs):
    conn = get_connection()
    try:
        init_db(conn)
        offset = _load_offset(conn)
        updates = deliver.get_updates(offset)

        if not updates:
            print("[poll] no pending updates")
            return 0

        handled = 0
        for update in updates:
            update_id = update.get("update_id")
            message = update.get("message")
            if message:
                try:
                    outcome = handle_message(conn, message, dry_run=dry_run)
                    if outcome:
                        handled += 1
                        print(f"[poll] {outcome}")
                except Exception as exc:
                    # Advance past it regardless. A command that crashes the handler
                    # would otherwise be retried every thirty minutes forever.
                    print(f"[poll] update {update_id} failed: {exc}")

            if not dry_run:
                # Persisted per update, not per batch: a crash halfway through must
                # not replay the commands already answered.
                set_state(conn, OFFSET_KEY, update_id + 1)
                conn.commit()

        if dry_run:
            print(f"[poll] dry run — {len(updates)} update(s) seen, offset not advanced")
        else:
            print(f"[poll] {handled} command(s) handled, offset now {_load_offset(conn)}")
        return handled
    finally:
        conn.close()
