"""SQLite helper for the nse-assist schema.

Five tables, one job each:

  prices        daily OHLCV bars, keyed (symbol, date) so re-ingesting a day is
                an upsert rather than a duplicate row
  signals       one row per rule firing, with the levels that were valid *at the
                time it fired* — never recomputed, so a backtest reads the same
                numbers the delivery message showed
  paper_trades  the ledger: what a signal actually did once taken
  fund_navs     AMFI NAVs, keyed (scheme_code, date), same upsert reasoning
  runs          stage-level audit trail written by src/runlog.py

Nothing here runs at import: init_db() is called once from main().
"""

import os
import sqlite3

from src.config import DB_PATH

# Composite primary key rather than a surrogate id: a bar is uniquely identified by
# its symbol and date, and INSERT OR REPLACE then makes re-ingest idempotent.
# `source` records which feed produced the bar so a feed swap is auditable.
_PRICES_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    source TEXT,
    PRIMARY KEY (symbol, date)
)
"""

# entry/stop/target are stored, not derived, so that a signal reviewed weeks later
# shows the levels it was actually generated with even if the rule has since changed.
# status: 'new' -> 'taken' | 'skipped' | 'expired'.
_SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rule TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry REAL,
    stop REAL,
    target REAL,
    size INTEGER,
    status TEXT DEFAULT 'proposed',
    created_at TEXT,
    -- Other rules that fired on the same symbol the same day. Multi-rule agreement
    -- is a testable conviction signal, so the losers of the dedupe are recorded
    -- rather than discarded — Burst 8 can ask whether agreement predicts anything.
    confirming_rules TEXT
)
"""

# Paper only. exit_reason is one of 'target', 'stop', 'time', 'manual' — kept as
# free text because a new exit rule should not require a migration.
_PAPER_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    entry_date TEXT,
    entry_price REAL,
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    pnl REAL,
    FOREIGN KEY (signal_id) REFERENCES signals (id)
)
"""

_FUND_NAVS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fund_navs (
    scheme_code TEXT NOT NULL,
    date TEXT NOT NULL,
    nav REAL,
    PRIMARY KEY (scheme_code, date)
)
"""

# Written by every stage start/ok/fail. Survives ephemeral CI runners because the
# DB is committed back after each scheduled run.
_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    date TEXT,
    stage TEXT,
    status TEXT,
    detail TEXT
)
"""

# Signals are almost always read "what fired today" or "everything still open",
# and paper trades are read by signal. Both are full scans without these.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_signals_date ON signals (date)",
    "CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_signal ON paper_trades (signal_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_date ON runs (date)",
)

TABLES = ("prices", "signals", "paper_trades", "fund_navs", "runs")


def get_connection(db_path=None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn, table, column, definition):
    """Adds a column to an existing table if it is missing.

    The database is committed and long-lived, so a schema addition has to migrate
    what is already there rather than only shaping new files.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(conn=None, db_path=None):
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        conn.execute(_PRICES_SCHEMA)
        conn.execute(_SIGNALS_SCHEMA)
        conn.execute(_PAPER_TRADES_SCHEMA)
        conn.execute(_FUND_NAVS_SCHEMA)
        conn.execute(_RUNS_SCHEMA)
        _ensure_column(conn, "signals", "confirming_rules", "TEXT")
        for statement in _INDEXES:
            conn.execute(statement)
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def table_counts(conn=None, db_path=None):
    """Row count per table, in TABLES order. Used by the doctor stage."""
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        init_db(conn)
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}
    finally:
        if owns_conn:
            conn.close()
