"""Stage 5 — the paper ledger: fills new signals, exits open ones, enforces the
daily limits from risk_config.

This is the only module that writes to paper_trades and the only one that moves a
signal off status 'new'. Running it twice in a day is safe — every step is
idempotent against what is already in the table.

Fills use the *next* bar's open, not the signal bar's close. A signal is generated
after the close, so filling at that close would be buying at a price that had
already passed.
"""

from src import features, risk_config
from src.db import get_connection, init_db
from src.runlog import today

STATUS_NEW = "new"
STATUS_TAKEN = "taken"
STATUS_SKIPPED = "skipped"
STATUS_EXPIRED = "expired"

EXIT_TARGET = "target"
EXIT_STOP = "stop"
EXIT_TIME = "time"

MAX_HOLD_BARS = 20


def open_positions(conn):
    rows = conn.execute(
        """SELECT t.id, t.signal_id, t.entry_date, t.entry_price,
                  s.symbol, s.direction, s.stop, s.target, s.size
           FROM paper_trades t JOIN signals s ON s.id = t.signal_id
           WHERE t.exit_date IS NULL"""
    ).fetchall()
    return [dict(row) for row in rows]


def realised_pnl(conn, date):
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE exit_date = ?", (date,)
    ).fetchone()
    return row[0]


def day_is_done(conn, date):
    """True once the day's realised P&L has crossed either limit. Both are hard
    stops: the profit target exists because giving back a good morning is the
    more expensive mistake of the two."""
    pnl = realised_pnl(conn, date)
    if pnl <= -abs(risk_config.MAX_DAILY_LOSS):
        return True, f"daily loss limit hit ({pnl:,.0f})"
    if pnl >= risk_config.DAILY_PROFIT_TARGET:
        return True, f"daily profit target hit ({pnl:,.0f})"
    return False, f"P&L {pnl:,.0f}"


def _bars_after(conn, symbol, date):
    return [bar for bar in features.load_bars(conn, symbol) if bar["date"] > date]


def fill_new_signals(conn, dry_run=False):
    """Fills 'new' signals at the next available open, subject to the position cap.
    Signals older than risk_config.SIGNAL_VALID_SESSIONS bars are expired instead —
    the setup that justified them has moved on."""
    filled, expired = [], []
    slots = risk_config.MAX_OPEN_POSITIONS - len(open_positions(conn))

    rows = conn.execute(
        "SELECT id, date, symbol, direction, entry, stop, target, size FROM signals "
        "WHERE status = ? ORDER BY date, id",
        (STATUS_NEW,),
    ).fetchall()

    for signal in (dict(r) for r in rows):
        later = _bars_after(conn, signal["symbol"], signal["date"])
        if not later:
            continue  # not enough forward data yet — leave it 'new' and retry tomorrow

        if len(later) > risk_config.SIGNAL_VALID_SESSIONS or slots <= 0:
            reason = STATUS_EXPIRED if len(later) > risk_config.SIGNAL_VALID_SESSIONS else STATUS_SKIPPED
            if not dry_run:
                conn.execute("UPDATE signals SET status = ? WHERE id = ?", (reason, signal["id"]))
            expired.append((signal["symbol"], reason))
            continue

        fill_bar = later[0]
        entry_price = fill_bar["open"] or fill_bar["close"]
        if not dry_run:
            conn.execute(
                "INSERT INTO paper_trades (signal_id, entry_date, entry_price) VALUES (?, ?, ?)",
                (signal["id"], fill_bar["date"], round(entry_price, 2)),
            )
            conn.execute("UPDATE signals SET status = ? WHERE id = ?", (STATUS_TAKEN, signal["id"]))
        filled.append((signal["symbol"], fill_bar["date"], round(entry_price, 2)))
        slots -= 1

    if not dry_run:
        conn.commit()
    return filled, expired


def close_open_positions(conn, dry_run=False):
    """Walks each open position's bars since entry and closes it on the first stop,
    target, or the hold limit. Stop wins a bar that contains both — daily bars
    cannot tell us which came first, and assuming the good one flatters the ledger."""
    closed = []

    for position in open_positions(conn):
        long = position["direction"] == "long"
        sign = 1 if long else -1

        for held, bar in enumerate(_bars_after(conn, position["symbol"], position["entry_date"]), start=1):
            hit_stop = bar["low"] <= position["stop"] if long else bar["high"] >= position["stop"]
            hit_target = bar["high"] >= position["target"] if long else bar["low"] <= position["target"]

            if hit_stop:
                exit_price, reason = position["stop"], EXIT_STOP
            elif hit_target:
                exit_price, reason = position["target"], EXIT_TARGET
            elif held >= MAX_HOLD_BARS:
                exit_price, reason = bar["close"], EXIT_TIME
            else:
                continue

            pnl = round((exit_price - position["entry_price"]) * sign * (position["size"] or 0), 2)
            if not dry_run:
                conn.execute(
                    "UPDATE paper_trades SET exit_date = ?, exit_price = ?, exit_reason = ?, pnl = ? WHERE id = ?",
                    (bar["date"], round(exit_price, 2), reason, pnl, position["id"]),
                )
            closed.append((position["symbol"], reason, pnl))
            break

    if not dry_run:
        conn.commit()
    return closed


def summary(conn):
    row = conn.execute(
        """SELECT COUNT(*) AS closed,
                  COALESCE(SUM(pnl), 0) AS total,
                  COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins
           FROM paper_trades WHERE exit_date IS NOT NULL"""
    ).fetchone()
    return {
        "closed": row["closed"],
        "open": len(open_positions(conn)),
        "wins": row["wins"],
        "total_pnl": round(row["total"], 2),
    }


def run(dry_run=False, **kwargs):
    conn = get_connection()
    try:
        init_db(conn)
        date = today()

        closed = close_open_positions(conn, dry_run=dry_run)

        done, why = day_is_done(conn, date)
        if done:
            print(f"[journal] no new fills — {why}")
            filled, expired = [], []
        else:
            filled, expired = fill_new_signals(conn, dry_run=dry_run)
            for symbol, fill_date, price in filled:
                print(f"[journal] filled {symbol:<12} @ {price:>9.2f} on {fill_date}")
            for symbol, reason in expired:
                print(f"[journal] {reason} {symbol}")
            # Again, because a signal old enough to fill this run may also have hit
            # its stop or target since. Without this the exit waits for tomorrow's
            # run, which matters when catching up on a gap in the schedule.
            closed += close_open_positions(conn, dry_run=dry_run)

        for symbol, reason, pnl in closed:
            print(f"[journal] closed {symbol:<12} {reason:<7} {pnl:>10,.0f}")

        stats = summary(conn)
        suffix = " (dry run, nothing written)" if dry_run else ""
        print(
            f"[journal] {len(filled)} filled, {len(closed)} closed, {stats['open']} open | "
            f"lifetime {stats['closed']} trades, {stats['wins']} wins, P&L {stats['total_pnl']:,.0f}{suffix}"
        )
        return stats
    finally:
        conn.close()
