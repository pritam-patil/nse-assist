"""Stage — the morning brief: what fired last evening, and what taking it would cost.

    python main.py --stage brief

Sent before the open, so it describes decisions you are about to make rather than
reporting on ones already made. Transport is deliver.py's — the requests session
with retry and backoff, and the separator-aware message splitting — because
Telegram is still the only channel and there is no reason for two implementations
of sending.

TONE IS A DESIGN CONSTRAINT, NOT A PREFERENCE

No emojis, no superlatives, no urgency. This message exists to inform a decision,
not to sell one. A brief that makes three ordinary signals feel like an opportunity
is working against the person reading it at 8am, and the failure mode is expensive:
you act on the framing rather than the numbers. Every line here is a fact with a
unit attached.

An empty day is normal output. "No rules fired." is the whole message, stated
plainly, because a system that only speaks when it has something exciting to say
teaches you to expect excitement.

ENTRY IS AN ESTIMATE AND THE MESSAGE SAYS SO

Signals are generated after the close; the fill happens at the open that has not
happened yet when you read this. The number shown is the close-based estimate the
levels were derived from. Presenting it as the entry price would be a small lie
that compounds — the gap between it and the fill is real slippage, measured in the
backtest, and it belongs in the reader's head too.
"""

from datetime import datetime, time, timedelta, timezone

from src import deliver, health, message, risk_config, rules_config, sentiment, signals
from src.db import get_connection, init_db
from src.runlog import today


# NSE's cash market opens at 09:15 IST. The brief is written to be read before
# that; whether it actually is depends on GitHub's scheduler, which guarantees
# nothing.
MARKET_OPEN_IST = time(9, 15)


def _ist_now():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def timing_note(now=None):
    """States when the brief was generated, and whether that is still pre-open.

    NO CRON CAN BE RELIED ON. Measured 2026-08-03: the morning workflow was
    scheduled for 08:45 IST and ran at 12:07, three hours and twenty-two minutes
    late, which turned a pre-open brief into a mid-session one describing fills
    that had already happened. Moving the schedule earlier buys margin; it does
    not buy a guarantee.

    So the message says what time it is. A late brief that admits it is late is
    still usable — you know the opens have gone and the estimates are history. A
    late brief that reads exactly like an on-time one is worse than none, because
    it invites you to act on levels the market has already passed.
    """
    now = now or _ist_now()
    stamp = now.strftime("%H:%M IST")
    if now.time() < MARKET_OPEN_IST:
        minutes = ((MARKET_OPEN_IST.hour * 60 + MARKET_OPEN_IST.minute)
                   - (now.hour * 60 + now.minute))
        return f"Generated {stamp}, {minutes} minutes before the open."
    return (f"LATE — generated {stamp}, after the 09:15 open. The entries below "
            f"are estimates for opens that have already happened; treat them as a "
            f"record of what was proposed, not as prices you can still get.")


def enabled_signals(conn, date=None):
    """Signals from the most recent scan whose rule is still enabled.

    Rules can be disabled after their signals were written — a walk-forward verdict
    lands between the evening scan and the morning brief — and a disabled rule's
    signal must not be presented as actionable.
    """
    if date is None:
        row = conn.execute("SELECT MAX(date) FROM signals").fetchone()
        date = row[0] if row else None
    if not date:
        return date, []

    rows = conn.execute(
        "SELECT id, symbol, rule, direction, entry, stop, target, size, status, confirming_rules "
        "FROM signals WHERE date = ? ORDER BY symbol",
        (date,),
    ).fetchall()
    allowed = set(signals.ENABLED_RULES)
    return date, [dict(r) for r in rows if r["rule"] in allowed]


def trim_to_daily_loss(candidates, limit=None):
    """Drop from the end until the combined worst case fits the daily loss limit.

    signals.assemble_portfolio() already enforces this when the signals are written.
    This repeats it because the brief reads from the table, and the table can hold
    signals from a scan that ran under different limits — or under a bug. A cap
    worth having is worth checking at the point it is presented to a person, not
    only at the point it was computed.
    """
    limit = risk_config.MAX_DAILY_LOSS if limit is None else limit
    kept, dropped, used = [], [], 0.0
    for candidate in candidates:
        risk = (candidate["entry"] - candidate["stop"]) * candidate["size"]
        if kept and used + risk > limit:
            dropped.append(candidate)
            continue
        kept.append(candidate)
        used += risk
    return kept, dropped, round(used, 2)


def last_session_results(conn, before):
    """One line on the most recent day that closed any paper trades."""
    row = conn.execute(
        "SELECT MAX(exit_date) FROM paper_trades WHERE exit_date IS NOT NULL AND exit_date < ?",
        (before,),
    ).fetchone()
    day = row[0] if row else None
    if not day:
        return "Previous session: no paper trades closed."

    stats = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(pnl), 0) net, "
        "COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) wins "
        "FROM paper_trades WHERE exit_date = ?",
        (day,),
    ).fetchone()
    reasons = conn.execute(
        "SELECT exit_reason, COUNT(*) n FROM paper_trades WHERE exit_date = ? "
        "GROUP BY exit_reason ORDER BY n DESC",
        (day,),
    ).fetchall()
    detail = ", ".join(f"{r['n']} {r['exit_reason']}" for r in reasons)
    plural = "winner" if stats["wins"] == 1 else "winners"
    return (f"Previous session ({day}): {stats['n']} closed, {stats['wins']} {plural}, "
            f"net {stats['net']:,.0f}" + (f" ({detail})." if detail else "."))


def build_brief(conn, date=None):
    signal_date, candidates = enabled_signals(conn, date)
    kept, dropped, risk_used = trim_to_daily_loss(candidates)

    lines = [message.title(message.MORNING)]
    notes = {}
    lines.append(timing_note())

    # STALENESS GOES ABOVE THE SIGNALS, NOT IN THE FOOTER.
    #
    # The brief still sends when ingest failed — a silent morning is worse, because
    # you cannot tell it apart from a morning with nothing to report. But the whole
    # value of sending anyway depends on the reader knowing which day these levels
    # describe. An entry computed from Tuesday's close, read on Thursday, is a
    # number that looks exactly as actionable as a correct one. Under the header is
    # the only place it cannot be skimmed past.
    stale = health.staleness_note(conn)
    if stale:
        lines.append(f"\nDATA IS BEHIND\n{stale}")
        if signal_date:
            lines.append(
                f"The signals below were computed on {signal_date} and have not been "
                "recomputed since. Treat the levels as history, not as this morning's."
            )

    # Directly under the header, above the candidates. This is the message that
    # presents entries, stops and share counts as things to act on; the reader has
    # to meet the caveat before the numbers, not after them.
    banner = rules_config.pipeline_test_banner()
    if banner:
        lines.append(f"\n{banner}")

    if not signals.ENABLED_RULES:
        # Distinct from "nothing fired": nothing can fire. A reader who cannot tell
        # these apart waits indefinitely for a signal that will never come.
        lines.append(
            f"\nNo rules enabled. All {len(signals.RULES)} were disabled by walk-forward "
            "validation; none was profitable out-of-sample in a majority of windows.\n"
            "Nothing will fire until a rule is re-enabled."
        )
    elif not signal_date:
        lines.append("\nNo signals recorded yet.")
    elif not kept:
        lines.append(f"\nNo rules fired on {signal_date}.")
    else:
        lines.append(f"\nSignals from {signal_date} ({len(kept)})")
        # Fetched once for the whole brief rather than per candidate.
        notes = sentiment.for_signals(conn, [c["id"] for c in kept if c.get("id")])
        for candidate in kept:
            risk = (candidate["entry"] - candidate["stop"]) * candidate["size"]
            deployed = candidate["entry"] * candidate["size"]
            confirming = candidate.get("confirming_rules")
            lines.append(
                f"\n{candidate['symbol']}  {candidate['rule']}"
                f"\n  entry {candidate['entry']:,.2f} (estimate; fills at today's open)"
                f"\n  stop {candidate['stop']:,.2f}   target {candidate['target']:,.2f}"
                f"\n  {candidate['size']} shares   capital {deployed:,.0f}   at risk {risk:,.0f}"
                + (f"\n  also flagged by: {confirming}" if confirming else "")
            )
            # Printed under the candidate it describes, and labelled on the same
            # line as the number. A score shown without its status reads as an
            # input to the decision, which is exactly what it is not.
            note = notes.get(candidate.get("id"))
            if note and note.get("score") is not None:
                lines.append(
                    f"  news {note['score']:+.2f} (unvalidated — informational only)"
                    + (f"\n    {note['rationale']}" if note.get("rationale") else "")
                )

    if kept:
        deployed = sum(c["entry"] * c["size"] for c in kept)
        lines.append(
            f"\nIf all fire: {deployed:,.0f} deployed of {risk_config.MAX_TOTAL_CAPITAL:,} "
            f"({deployed / risk_config.MAX_TOTAL_CAPITAL:.0%})."
            f"\nWorst case combined loss: {risk_used:,.0f} of "
            f"{risk_config.MAX_DAILY_LOSS:,} limit."
        )
        if dropped:
            names = ", ".join(c["symbol"] for c in dropped)
            lines.append(
                f"Trimmed to fit the daily loss limit: {names} not listed."
            )

    lines.append(last_session_results(conn, today()))

    # Staleness is already under the header above, where it cannot be skimmed past.
    footer = health.footer(conn, include_staleness=False)
    if footer:
        lines.append(f"\n{footer}")

    if any(n.get("score") is not None for n in (notes or {}).values()):
        lines.append(
            "\nNews scores are observational. They did not filter, size, order or "
            "veto anything above, and no evidence yet says they should."
        )

    lines.append("\nPaper trades. Not investment advice.")
    return "\n".join(lines)


def run(dry_run=False, date=None, **kwargs):
    conn = get_connection()
    try:
        init_db(conn)
        message = build_brief(conn, date)
        # Plain text: the brief carries no markup, and HTML parse mode would make a
        # stray ampersand in a symbol name fail the send.
        deliver.send_message(message, dry_run=dry_run, parse_mode=None)
        print(f"[brief] sent ({len(message)} chars)" if not dry_run else "[brief] dry run")
        return message
    finally:
        conn.close()
