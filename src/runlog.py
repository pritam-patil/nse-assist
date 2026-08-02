"""Stage-level audit trail, stored in the `runs` table.

The DB is committed back by the scheduled workflows, so this history survives the
ephemeral runner and is what you read when a run behaved oddly three days ago. It
also backs the one-line health summary attached to the daily Telegram delivery.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone

from src.db import get_connection, init_db

# The DB is committed, so it must stay small. Trimmed to the newest MAX_ROWS
# whenever it grows past that (~3 months of normal runs fits comfortably).
MAX_ROWS = 5000


def log(stage, status, detail=None, conn=None):
    """Records one stage event. Never raises — logging must not break a run."""
    owns_conn = conn is None
    try:
        conn = conn or get_connection()
    except sqlite3.Error:
        return
    try:
        init_db(conn)
        conn.execute(
            "INSERT INTO runs (date, stage, status, detail) VALUES (?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                stage,
                status,
                str(detail)[:300] if detail else None,
            ),
        )
        conn.commit()
        _trim_if_needed(conn)
    except sqlite3.Error:
        pass
    finally:
        if owns_conn:
            conn.close()


def _trim_if_needed(conn):
    count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    if count <= MAX_ROWS:
        return
    conn.execute(
        "DELETE FROM runs WHERE rowid NOT IN (SELECT rowid FROM runs ORDER BY rowid DESC LIMIT ?)",
        (MAX_ROWS,),
    )
    conn.commit()


def events_since(hours=24, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        init_db(conn)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = conn.execute(
            "SELECT date, stage, status, detail FROM runs WHERE date >= ? ORDER BY rowid", (cutoff,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def health_line(hours=24, conn=None):
    """One line: stages completed and stages that failed. None if nothing happened."""
    events = events_since(hours, conn=conn)
    if not events:
        return None

    # Latest outcome per stage, not every outcome in the window. A stage that failed
    # at 09:00 and succeeded at 18:00 is working; reporting it as FAILED all day is
    # how a health line becomes something people learn to ignore, so that the one
    # time it matters they skip past it too.
    #
    # `events` is oldest-first, so the last write per stage wins. A trailing 'start'
    # with no outcome after it means the process died mid-stage — worth its own word,
    # because that is invisible in a tally of ok-versus-failed.
    latest = {}
    for event in events:
        latest[event["stage"]] = event["status"]

    ok = sorted(s for s, status in latest.items() if status == "ok")
    failed = sorted(s for s, status in latest.items() if status == "fail")
    incomplete = sorted(s for s, status in latest.items() if status == "start")

    parts = [f"{len(ok)} stage(s) ok" if ok else "no stages completed"]
    if failed:
        parts.append(f"FAILED: {', '.join(failed)}")
    if incomplete:
        parts.append(f"did not finish: {', '.join(incomplete)}")
    if not failed and not incomplete:
        parts.append("no failures")
    return f"Health ({hours}h): " + " | ".join(parts)


def today():
    """Trading-day stamp used across stages. Local date — the market is IST."""
    return date.today().isoformat()
