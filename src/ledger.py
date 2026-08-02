"""One way to read the paper ledger. Every report goes through here.

    from src import ledger
    ledger.summarize(ledger.closed_trades(conn))

WHY THIS MODULE EXISTS

Before it, six places computed "closed trades grouped by something" with their own
SQL: journal.summary, journal.per_rule_live, weekly._rule_stats, weekly.cohort_stats,
gate.snapshot, and the scorecard. Each had its own idea of whether a break-even
trade counts as a win, whether an empty group returns None or 0.0, and how to round.
They agreed, and nothing checked that they did.

That is the shape of a bug this project has already paid for once. journal.py and
backtest.py each owned an exit loop; they silently disagreed about hold length —
20 sessions against 10 — and nothing failed, because two implementations producing
different numbers is not an error, it is a comparison. `resolve_exit` fixed it by
having one definition and importing it.

This is the same fix one level up, and it matters more now: the frozen evaluation
gate reads these numbers, so a divergence between what the weekly reports and what
the gate judges would be a divergence in what "PASS" means.

THE ARITHMETIC LIVES IN backtest.summarize(), NOT HERE

Deliberately. The backtest and the live ledger must be compared field by field, and
they can only be compared if the fields mean the same thing. `paper_trades` already
stores pnl, gross_pnl, costs and held_bars precisely so a live trade can be handed
to the same function a simulated one is. This module is the query layer; the
statistics are the backtest's.
"""

from src.backtest import summarize

TRADE_CLOSED = "closed"

# Re-exported so callers need only one import. `summarize` is backtest's — see the
# module docstring for why it is not reimplemented here.
__all__ = ["closed_trades", "summarize", "by_rule", "by_cohort", "totals"]


def closed_trades(conn, since=None, until=None, rules=None):
    """Closed paper trades with their originating rule, oldest exit first.

    `since` is exclusive and `until` inclusive, matching how the weekly asks for
    "this week": everything that closed after last Sunday, up to and including
    today.

    COALESCE on the three cost fields is load-bearing. They were added to
    paper_trades by migration, so rows written before that carry NULL, and
    summarize() sums them — one NULL turns a whole report into None without
    raising anywhere.
    """
    clauses = ["t.status = ?"]
    params = [TRADE_CLOSED]
    if since:
        clauses.append("t.exit_date > ?")
        params.append(since)
    if until:
        clauses.append("t.exit_date <= ?")
        params.append(until)
    if rules:
        clauses.append(f"s.rule IN ({','.join('?' * len(rules))})")
        params.extend(rules)

    rows = conn.execute(
        f"""SELECT t.id, t.signal_id, t.entry_date, t.exit_date, t.exit_reason,
                   COALESCE(t.pnl, 0) pnl,
                   COALESCE(t.gross_pnl, 0) gross_pnl,
                   COALESCE(t.costs, 0) costs,
                   COALESCE(t.held_bars, 0) held_bars,
                   s.rule, s.symbol, s.confirming_rules
            FROM paper_trades t JOIN signals s ON s.id = t.signal_id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.exit_date, t.id""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def open_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
    ).fetchone()[0]


def _group(trades, key):
    grouped = {}
    for trade in trades:
        grouped.setdefault(key(trade), []).append(trade)
    return {name: summarize(rows) for name, rows in sorted(grouped.items())}


def by_rule(conn, since=None, until=None):
    return _group(closed_trades(conn, since, until), lambda t: t["rule"])


def by_cohort(conn, since=None, until=None):
    """Confirmed against solo.

    A trade is "confirmed" when more than one rule fired on that symbol that day
    and the others were recorded rather than discarded. An EMPTY STRING counts as
    solo: signals.py writes NULL, but a blank would otherwise read as agreement and
    quietly inflate the cohort.
    """
    def cohort(trade):
        confirming = trade.get("confirming_rules")
        return "confirmed" if (confirming and confirming.strip()) else "solo"

    return _group(closed_trades(conn, since, until), cohort)


def totals(conn, since=None, until=None):
    """One summary across every closed trade, plus the open count."""
    stats = summarize(closed_trades(conn, since, until))
    stats["open"] = open_count(conn)
    return stats
