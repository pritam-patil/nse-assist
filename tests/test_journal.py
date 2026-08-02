"""The paper ledger must fill exactly as the backtest does, and be safe to re-run.

    python -m unittest discover -s tests -v

The load-bearing test is test_journal_matches_backtest_trade_for_trade. Everything
else in this project is measured against the backtest, so if the live ledger fills
differently the comparison is not merely noisy — it is meaningless, and it looks
fine while being meaningless. That failure has already happened once: journal.py
used to hold positions for 20 sessions against the backtest's 10.
"""

import os
import tempfile
import unittest

from src import backtest, journal
from src.db import get_connection, init_db

SYMBOL = "TESTCO"


def bar(day, open_, high, low, close):
    return (SYMBOL, day, open_, high, low, close, 100_000, "test")


class SharedFillLogicTestCase(unittest.TestCase):
    """The journal must not own a second definition of a fill."""

    def test_journal_uses_the_backtest_function_object(self):
        self.assertIs(journal.backtest.resolve_exit, backtest.resolve_exit)

    def test_hold_limits_are_the_same_number(self):
        """They were 20 and 10 once. Nothing failed; the ledger just measured a
        different strategy than the one being validated."""
        self.assertEqual(journal.MAX_HOLD_BARS, backtest.MAX_HOLD_BARS)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _prices(self, bars):
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices "
            "(symbol, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            bars)
        self.conn.commit()

    def _signal(self, day, entry=100.0, stop=95.0, target=110.0, size=10,
                rule="momentum_continuation"):
        cursor = self.conn.execute(
            "INSERT INTO signals (date, symbol, rule, direction, entry, stop, target, "
            "size, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (day, SYMBOL, rule, "long", entry, stop, target, size,
             journal.STATUS_PROPOSED, "x"))
        self.conn.commit()
        return cursor.lastrowid

    # --- the comparison the whole project rests on ---

    def test_journal_matches_backtest_trade_for_trade(self):
        """Walk a position day by day and run it in one pass: identical outcome.

        Same entry price, same exit price, same reason, same P&L. If these diverge,
        every live-versus-backtest number in the system is comparing two different
        strategies.
        """
        bars = [
            bar("2026-01-01", 100.0, 101.0, 99.0, 100.0),   # signal evening
            bar("2026-01-02", 100.5, 102.0, 99.5, 101.0),   # fill at open
            bar("2026-01-05", 101.0, 103.0, 100.0, 102.0),
            bar("2026-01-06", 102.0, 111.0, 101.0, 109.0),  # target
        ]
        self._prices(bars)
        self._signal("2026-01-01")

        for day in ("2026-01-02", "2026-01-05", "2026-01-06"):
            journal.walk_open(self.conn, day)
            journal.fill_proposed(self.conn, day)
            journal.walk_open(self.conn, day)

        live = self.conn.execute(
            "SELECT entry_price, exit_price, exit_reason, pnl, held_bars, gross_pnl, costs "
            "FROM paper_trades").fetchone()

        as_bars = [{"date": b[1], "open": b[2], "high": b[3], "low": b[4], "close": b[5]}
                   for b in bars]
        simulated, _ = backtest.simulate_position(
            as_bars, 0,
            {"symbol": SYMBOL, "rule": "momentum_continuation",
             "entry": 100.0, "stop": 95.0, "target": 110.0, "size": 10})

        self.assertEqual(live["entry_price"], simulated["entry_price"])
        self.assertEqual(live["exit_price"], simulated["exit_price"])
        self.assertEqual(live["exit_reason"], simulated["exit_reason"])
        self.assertAlmostEqual(live["pnl"], simulated["pnl"], places=2)
        self.assertAlmostEqual(live["costs"], simulated["costs"], places=2)
        self.assertEqual(live["held_bars"], simulated["held_bars"])

    def test_gap_through_the_stop_fills_at_the_open_here_too(self):
        self._prices([
            bar("2026-01-01", 100.0, 101.0, 99.0, 100.0),
            bar("2026-01-02", 100.5, 102.0, 99.5, 101.0),
            bar("2026-01-05", 90.0, 91.0, 88.0, 89.0),      # gaps under the stop
        ])
        self._signal("2026-01-01")
        journal.fill_proposed(self.conn, "2026-01-02")
        journal.walk_open(self.conn, "2026-01-05")
        row = self.conn.execute("SELECT exit_price, exit_reason FROM paper_trades").fetchone()
        self.assertEqual(row["exit_reason"], "stop")
        self.assertEqual(row["exit_price"], 90.0, "must fill at the open, not the stop")

    def test_a_day_containing_both_levels_is_a_stop(self):
        self._prices([
            bar("2026-01-01", 100.0, 101.0, 99.0, 100.0),
            bar("2026-01-02", 100.5, 112.0, 94.0, 105.0),
        ])
        self._signal("2026-01-01")
        journal.fill_proposed(self.conn, "2026-01-02")
        journal.walk_open(self.conn, "2026-01-02")
        row = self.conn.execute("SELECT exit_reason, pnl FROM paper_trades").fetchone()
        self.assertEqual(row["exit_reason"], "stop")
        self.assertLess(row["pnl"], 0)

    # --- entry ---

    def test_entry_is_todays_actual_open(self):
        self._prices([
            bar("2026-01-01", 100.0, 101.0, 99.0, 100.0),
            bar("2026-01-02", 103.0, 104.0, 102.0, 103.5),
        ])
        self._signal("2026-01-01", entry=100.0)
        journal.fill_proposed(self.conn, "2026-01-02")
        row = self.conn.execute("SELECT entry_price, status FROM paper_trades").fetchone()
        self.assertEqual(row["entry_price"], 103.0)
        self.assertEqual(row["status"], journal.TRADE_OPEN)

    def test_a_signal_written_tonight_does_not_fill_tonight(self):
        self._prices([bar("2026-01-02", 100.0, 101.0, 99.0, 100.0)])
        self._signal("2026-01-02")
        filled, _ = journal.fill_proposed(self.conn, "2026-01-02")
        self.assertEqual(filled, [])

    def test_an_open_past_the_stop_is_not_entered(self):
        self._prices([
            bar("2026-01-01", 100.0, 101.0, 99.0, 100.0),
            bar("2026-01-02", 90.0, 92.0, 89.0, 91.0),
        ])
        self._signal("2026-01-01")
        filled, skipped = journal.fill_proposed(self.conn, "2026-01-02")
        self.assertEqual(filled, [])
        self.assertIn("gapped past the stop", skipped[0][1])

    def test_a_stale_proposal_expires(self):
        days = ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
        self._prices([bar(d, 100.0, 101.0, 99.0, 100.0) for d in days])
        self._signal("2026-01-01")
        journal.fill_proposed(self.conn, days[-1])
        status = self.conn.execute("SELECT status FROM signals").fetchone()["status"]
        self.assertEqual(status, journal.STATUS_EXPIRED)

    # --- idempotency ---

    def test_refilling_does_not_open_a_second_position(self):
        self._prices([
            bar("2026-01-01", 100.0, 101.0, 99.0, 100.0),
            bar("2026-01-02", 100.5, 102.0, 99.5, 101.0),
        ])
        self._signal("2026-01-01")
        for _ in range(3):
            journal.fill_proposed(self.conn, "2026-01-02")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 1)

    def test_rewalking_does_not_book_pnl_twice(self):
        self._prices([
            bar("2026-01-01", 100.0, 101.0, 99.0, 100.0),
            bar("2026-01-02", 100.5, 102.0, 99.5, 101.0),
            bar("2026-01-05", 101.0, 112.0, 100.0, 111.0),
        ])
        self._signal("2026-01-01")
        journal.fill_proposed(self.conn, "2026-01-02")
        first, _ = journal.walk_open(self.conn, "2026-01-05")
        again, _ = journal.walk_open(self.conn, "2026-01-05")
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [], "a closed trade must not be walked again")
        total = self.conn.execute("SELECT SUM(pnl) FROM paper_trades").fetchone()[0]
        self.assertAlmostEqual(total, first[0][3], places=2)

    def test_the_unique_index_is_the_guard_not_the_python(self):
        """Enforced by the database, so a raced or skipped guard cannot duplicate."""
        self._prices([bar("2026-01-02", 100.0, 101.0, 99.0, 100.0)])
        signal_id = self._signal("2026-01-01")
        self.conn.execute(
            "INSERT INTO paper_trades (signal_id, entry_date, entry_price, status) "
            "VALUES (?,?,?,?)", (signal_id, "2026-01-02", 100.0, journal.TRADE_OPEN))
        self.conn.commit()
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO paper_trades (signal_id, entry_date, entry_price, status) "
                "VALUES (?,?,?,?)", (signal_id, "2026-01-02", 100.0, journal.TRADE_OPEN))

    # --- costs and reporting ---

    def test_pnl_is_net_of_the_same_costs_module(self):
        from src import costs
        self._prices([
            bar("2026-01-01", 100.0, 101.0, 99.0, 100.0),
            bar("2026-01-02", 100.0, 102.0, 99.5, 101.0),
            bar("2026-01-05", 101.0, 112.0, 100.0, 111.0),
        ])
        self._signal("2026-01-01")
        journal.fill_proposed(self.conn, "2026-01-02")
        journal.walk_open(self.conn, "2026-01-05")
        row = self.conn.execute(
            "SELECT entry_price, exit_price, gross_pnl, costs, pnl FROM paper_trades").fetchone()
        expected = costs.round_trip(row["entry_price"], row["exit_price"], 10)["total"]
        self.assertAlmostEqual(row["costs"], expected, places=2)
        self.assertAlmostEqual(row["pnl"], row["gross_pnl"] - row["costs"], places=2)

    def test_report_runs_on_an_empty_ledger(self):
        """The current state: rules disabled, nothing pending. Must not divide by
        zero on the way to saying so."""
        result = journal.report()
        self.assertEqual(result["summary"]["closed"], 0)

    def test_per_rule_live_groups_by_the_originating_rule(self):
        self._prices([
            bar("2026-01-01", 100.0, 101.0, 99.0, 100.0),
            bar("2026-01-02", 100.0, 102.0, 99.5, 101.0),
            bar("2026-01-05", 101.0, 112.0, 100.0, 111.0),
        ])
        self._signal("2026-01-01", rule="volume_breakout")
        journal.fill_proposed(self.conn, "2026-01-02")
        journal.walk_open(self.conn, "2026-01-05")
        per_rule = journal.per_rule_live(self.conn)
        self.assertIn("volume_breakout", per_rule)
        self.assertEqual(per_rule["volume_breakout"]["trades"], 1)
        self.assertEqual(per_rule["volume_breakout"]["hit_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
