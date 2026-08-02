"""Stage 5 — the paper ledger: fills proposed signals, walks open positions, records P&L.

    python main.py --stage journal          # each evening, after ingest
    python main.py --stage journal-report   # live results beside the backtest's

FILL LOGIC IS IMPORTED, NEVER REIMPLEMENTED

Every exit decision comes from backtest.resolve_exit(). Not a copy, not "the same
rules written out again" — the same function object. The point of running a paper
ledger next to a backtest is to learn whether the backtest predicts anything, and
that comparison is void the moment the two fill differently. A divergence would not
raise; it would produce two plausible P&L curves that disagree for reasons nobody
could locate.

Not hypothetical: before the shared primitive existed this module had its own exit
loop and a 20-session hold against the backtest's 10, so the ledger was holding
every position twice as long as the strategy being measured.

WHAT IS DELIBERATELY DIFFERENT

The backtest force-closes a position when history runs out, marking it at the last
close so an unfinished trade is not dropped from the sample. A live position with
sessions still to run is simply open, and stays open. That is the one place the two
must not match, and it is why the shared function is the per-bar decision rather
than the whole trade.

IDEMPOTENT

The evening chain gets re-run — a retried workflow, a manual invocation after a fix
— and must not open a position twice or book a P&L twice. A UNIQUE index on
signal_id enforces one trade per signal in the database rather than in a guard that
can be raced, and only rows with status='open' are ever walked.
"""

from datetime import date

from src import backtest, costs, risk_config
from src.db import get_connection, init_db
from src.runlog import today

STATUS_PROPOSED = "proposed"
STATUS_TAKEN = "taken"
STATUS_SKIPPED = "skipped"
STATUS_EXPIRED = "expired"

TRADE_OPEN = "open"
TRADE_CLOSED = "closed"

# Imported, not restated. See the module docstring.
MAX_HOLD_BARS = backtest.MAX_HOLD_BARS


# --- reading ------------------------------------------------------------------


def open_trades(conn):
    rows = conn.execute(
        """SELECT t.id, t.signal_id, t.entry_date, t.entry_price,
                  s.symbol, s.rule, s.direction, s.stop, s.target, s.size
           FROM paper_trades t JOIN signals s ON s.id = t.signal_id
           WHERE t.status = ? ORDER BY t.entry_date, s.symbol""",
        (TRADE_OPEN,),
    ).fetchall()
    return [dict(row) for row in rows]


# deliver.py reports the book; the shape it wants is the same one.
open_positions = open_trades


def _bar_on(conn, symbol, day):
    row = conn.execute(
        "SELECT date, open, high, low, close FROM prices WHERE symbol = ? AND date = ?",
        (symbol, day),
    ).fetchone()
    return dict(row) if row else None


def _sessions_between(conn, symbol, start, through):
    """Sessions strictly after `start` up to `through`, from the symbol's own bars.

    Derived rather than stored: a held-bars counter would need updating on every
    walk and would drift the first time a run died between the update and the
    commit.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM prices WHERE symbol = ? AND date > ? AND date <= ?",
        (symbol, start, through),
    ).fetchone()[0]


def realised_pnl(conn, day):
    return conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE exit_date = ?", (day,)
    ).fetchone()[0]


def day_is_done(conn, day):
    """True once the day's realised P&L has crossed either limit."""
    pnl = realised_pnl(conn, day)
    if pnl <= -abs(risk_config.MAX_DAILY_LOSS):
        return True, f"daily loss limit hit ({pnl:,.0f})"
    if pnl >= risk_config.DAILY_PROFIT_TARGET:
        return True, f"daily profit target hit ({pnl:,.0f})"
    return False, f"P&L {pnl:,.0f}"


# --- (1) proposals become positions at today's open ---------------------------


def fill_proposed(conn, day=None, dry_run=False):
    """Enter yesterday's proposals at today's actual open.

    The open is the first obtainable price after a signal written the previous
    evening, which is exactly what the backtest assumes. Levels stay anchored to the
    signal-time estimate — recomputing them from the fill would make the ledger
    trade something the backtest never simulated.
    """
    day = day or today()
    filled, skipped = [], []

    proposals = conn.execute(
        "SELECT id, date, symbol, rule, direction, entry, stop, target, size "
        "FROM signals WHERE status = ? ORDER BY date, id",
        (STATUS_PROPOSED,),
    ).fetchall()

    live = open_trades(conn)
    slots = risk_config.MAX_OPEN_POSITIONS - len(live)
    held_symbols = {t["symbol"] for t in live}

    def mark(signal_id, status):
        if not dry_run:
            conn.execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))

    for signal in (dict(row) for row in proposals):
        if signal["date"] >= day:
            continue  # written this evening; it fills tomorrow, not tonight

        bar = _bar_on(conn, signal["symbol"], day)
        if not bar or not bar["open"]:
            continue  # no session for this symbol today, so the proposal waits

        waited = _sessions_between(conn, signal["symbol"], signal["date"], day)
        if waited > risk_config.SIGNAL_VALID_SESSIONS:
            skipped.append((signal["symbol"], "expired"))
            mark(signal["id"], STATUS_EXPIRED)
            continue
        if slots <= 0 or signal["symbol"] in held_symbols:
            skipped.append((signal["symbol"], "no slot or already held"))
            mark(signal["id"], STATUS_SKIPPED)
            continue

        fill = bar["open"]
        # The same two refusals the backtest makes: an open past either level is not
        # an entry, it is an instant outcome you chose to accept.
        if fill <= signal["stop"] or fill >= signal["target"]:
            side = "stop" if fill <= signal["stop"] else "target"
            skipped.append((signal["symbol"], f"gapped past the {side} before entry"))
            mark(signal["id"], STATUS_SKIPPED)
            continue

        if not dry_run:
            # INSERT OR IGNORE against the UNIQUE index on signal_id: a re-run finds
            # the row already there and does nothing, rather than opening a second
            # position on one signal.
            conn.execute(
                "INSERT OR IGNORE INTO paper_trades "
                "(signal_id, entry_date, entry_price, status) VALUES (?, ?, ?, ?)",
                (signal["id"], day, round(fill, 2), TRADE_OPEN),
            )
            mark(signal["id"], STATUS_TAKEN)
        filled.append((signal["symbol"], signal["rule"], round(fill, 2), signal["size"]))
        held_symbols.add(signal["symbol"])
        slots -= 1

    if not dry_run:
        conn.commit()
    return filled, skipped


# --- (2) walk open positions against today's bar ------------------------------


def walk_open(conn, day=None, dry_run=False):
    """Test every open position against today's OHLC, using the backtest's own rule."""
    day = day or today()
    closed, still_open = [], []

    for trade in open_trades(conn):
        bar = _bar_on(conn, trade["symbol"], day)
        if not bar:
            still_open.append(trade)
            continue

        # Sessions held, counting the entry day as the first. The backtest's `held`
        # starts at 1 on the fill bar, so the time stop triggers on the same session
        # for both.
        held = _sessions_between(conn, trade["symbol"], trade["entry_date"], day) + 1
        exit_price, reason = backtest.resolve_exit(
            bar, trade["stop"], trade["target"], held, MAX_HOLD_BARS
        )
        if exit_price is None:
            still_open.append(trade)
            continue

        size = trade["size"] or 0
        gross = (exit_price - trade["entry_price"]) * size
        charges = costs.round_trip(
            trade["entry_price"], exit_price, size, costs.DELIVERY
        )["total"]

        if not dry_run:
            # `AND status = 'open'` makes the write itself idempotent: a second pass
            # on the same evening matches no rows.
            conn.execute(
                """UPDATE paper_trades
                   SET exit_date = ?, exit_price = ?, exit_reason = ?, pnl = ?,
                       gross_pnl = ?, costs = ?, held_bars = ?, status = ?
                   WHERE id = ? AND status = ?""",
                (day, round(exit_price, 2), reason, round(gross - charges, 2),
                 round(gross, 2), round(charges, 2), held, TRADE_CLOSED,
                 trade["id"], TRADE_OPEN),
            )
        closed.append((trade["symbol"], trade["rule"], reason,
                       round(gross - charges, 2), held))

    if not dry_run:
        conn.commit()
    return closed, still_open


# --- summaries ----------------------------------------------------------------


def summary(conn):
    row = conn.execute(
        """SELECT COUNT(*) closed, COALESCE(SUM(pnl), 0) total,
                  COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) wins,
                  COALESCE(SUM(costs), 0) charges
           FROM paper_trades WHERE status = ?""",
        (TRADE_CLOSED,),
    ).fetchone()
    return {
        "closed": row["closed"],
        "open": len(open_trades(conn)),
        "wins": row["wins"],
        "total_pnl": round(row["total"], 2),
        "costs": round(row["charges"], 2),
    }


def per_rule_live(conn):
    rows = conn.execute(
        """SELECT s.rule,
                  COUNT(*) trades,
                  COALESCE(SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END), 0) wins,
                  COALESCE(SUM(t.pnl), 0) net,
                  COALESCE(AVG(t.pnl), 0) expectancy
           FROM paper_trades t JOIN signals s ON s.id = t.signal_id
           WHERE t.status = ? GROUP BY s.rule ORDER BY s.rule""",
        (TRADE_CLOSED,),
    ).fetchall()
    return {
        r["rule"]: {
            "trades": r["trades"],
            "wins": r["wins"],
            "hit_rate": (r["wins"] / r["trades"]) if r["trades"] else None,
            "net": round(r["net"], 2),
            "expectancy": round(r["expectancy"], 2),
        }
        for r in rows
    }


def evaluation_days(conn):
    first = conn.execute("SELECT MIN(entry_date) FROM paper_trades").fetchone()[0]
    if not first:
        return 0, None
    return (date.fromisoformat(today()) - date.fromisoformat(first)).days, first


# --- stage --------------------------------------------------------------------


def run(dry_run=False, date=None, **kwargs):
    day = date or today()
    conn = get_connection()
    try:
        init_db(conn)

        closed, _ = walk_open(conn, day, dry_run=dry_run)
        for symbol, rule, reason, net, held in closed:
            print(f"[journal] closed {symbol:<12} {rule:<22} {reason:<7} "
                  f"{net:>10,.0f} after {held} session(s)")

        done, why = day_is_done(conn, day)
        if done:
            print(f"[journal] no new fills — {why}")
            filled = []
        else:
            filled, skipped = fill_proposed(conn, day, dry_run=dry_run)
            for symbol, rule, fill, size in filled:
                print(f"[journal] opened {symbol:<12} {rule:<22} @ {fill:>9.2f} x{size}")
            for symbol, reason in skipped:
                print(f"[journal] skipped {symbol:<12} {reason}")

        # Positions opened just now are tested against today's bar too, so a signal
        # that gaps to its stop on entry day resolves tonight — which is what the
        # backtest does on its own fill bar.
        if filled:
            same_day, _ = walk_open(conn, day, dry_run=dry_run)
            for symbol, rule, reason, net, held in same_day:
                print(f"[journal] closed {symbol:<12} {rule:<22} {reason:<7} "
                      f"{net:>10,.0f} on entry day")
            closed += same_day

        stats = summary(conn)
        suffix = " (dry run, nothing written)" if dry_run else ""
        print(f"[journal] {len(filled)} opened, {len(closed)} closed, {stats['open']} open | "
              f"lifetime {stats['closed']} trades, {stats['wins']} wins, "
              f"net {stats['total_pnl']:,.0f}{suffix}")
        return stats
    finally:
        conn.close()


def report(dry_run=False, **kwargs):
    """--stage journal-report: is the live ledger behaving like the backtest?"""
    from src import rules_config, signals

    conn = get_connection()
    try:
        init_db(conn)
        stats = summary(conn)
        days, first = evaluation_days(conn)
        live = per_rule_live(conn)

        print(f"\n{'=' * 78}\nPAPER LEDGER — live results beside the backtest\n{'=' * 78}")
        if first:
            print(f"evaluation period: {days} day(s) since {first}")
        else:
            print("evaluation period: not started — no paper trade has been entered yet.")
        print(f"cumulative net P&L: {stats['total_pnl']:,.0f} over {stats['closed']} closed "
              f"trade(s), {stats['wins']} winner(s), {stats['open']} still open")
        print(f"costs paid: {stats['costs']:,.0f}")

        header = (f"{'rule':<24} {'live n':>7} {'live hit':>9} {'backtest hit':>13} "
                  f"{'gap':>8} {'live exp':>9}")
        print(f"\n{header}\n{'-' * len(header)}")
        for rule in signals.RULES:
            measured = live.get(rule)
            expected = rules_config.RULE_BACKTEST_HIT_RATE.get(rule)
            live_hit = measured["hit_rate"] if measured else None
            gap = None
            if live_hit is not None and expected is not None:
                gap = live_hit - expected

            count = measured["trades"] if measured else 0
            live_text = f"{live_hit:.1%}" if live_hit is not None else "-"
            expected_text = f"{expected:.1%}" if expected is not None else "-"
            gap_text = f"{gap:+.1%}" if gap is not None else "-"
            exp_text = f"{measured['expectancy']:,.0f}" if measured else "-"
            print(f"{rule:<24} {count:>7} {live_text:>9} {expected_text:>13} "
                  f"{gap_text:>8} {exp_text:>9}")
        print("-" * len(header))
        print(f"backtest hit rates: {rules_config.RULE_BACKTEST_HIT_RATE_BASIS}.")

        # A live hit rate on a handful of trades says nothing, and the temptation to
        # read it anyway is exactly why the count sits beside it.
        thin = [r for r, m in live.items() if m["trades"] < 30]
        if thin:
            print(f"under 30 trades — not yet evidence: {', '.join(sorted(thin))}")
        print()
        return {"summary": stats, "per_rule": live, "days": days}
    finally:
        conn.close()
