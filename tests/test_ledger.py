"""One reader for the paper ledger, and proof the reports agree about it.

    python -m unittest discover -s tests -v

Six places used to compute "closed trades grouped by something" with their own
SQL: journal.summary, journal.per_rule_live, weekly._rule_stats,
weekly.cohort_stats, gate.snapshot and the sentiment scorecard. They agreed, and
nothing checked that they did.

That is the shape of a bug this project already paid for once — journal.py and
backtest.py each owned an exit loop, silently disagreed about hold length, and
nothing failed, because two implementations producing different numbers is not an
error, it is a comparison. The cross-consistency class below is the check that was
missing.
"""

import os
import tempfile
import unittest

from src import gate, journal, ledger, weekly
from src.db import get_connection, init_db


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _trade(self, pnl, rule="momentum_continuation", exit_date="2026-07-30",
               entry_date="2026-06-01", confirming=None, symbol=None, costs=90.0,
               status="closed"):
        symbol = symbol or f"S{abs(hash((rule, pnl, exit_date, confirming))) % 99999}"
        cursor = self.conn.execute(
            "INSERT INTO signals (date, symbol, rule, direction, entry, stop, target, "
            "size, status, created_at, confirming_rules) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (entry_date, symbol, rule, "long", 100.0, 95.0, 110.0, 10, "taken", "x",
             confirming))
        self.conn.execute(
            "INSERT INTO paper_trades (signal_id, entry_date, entry_price, exit_date, "
            "exit_price, exit_reason, pnl, gross_pnl, costs, held_bars, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cursor.lastrowid, entry_date, 100.0,
             exit_date if status == "closed" else None, 105.0,
             "target" if pnl > 0 else "stop", pnl, pnl + costs, costs, 3, status))
        self.conn.commit()
        return cursor.lastrowid

    # --- selection ---

    def test_open_trades_are_excluded(self):
        self._trade(100)
        self._trade(999, status="open", symbol="OPEN1")
        self.assertEqual(len(ledger.closed_trades(self.conn)), 1)

    def test_until_is_inclusive_and_since_is_exclusive(self):
        """Matches how the weekly asks for 'this week': everything after last
        Sunday, up to and including today."""
        self._trade(100, exit_date="2026-07-25", symbol="A")
        self._trade(200, exit_date="2026-07-30", symbol="B")
        self.assertEqual(len(ledger.closed_trades(self.conn, since="2026-07-25")), 1)
        self.assertEqual(len(ledger.closed_trades(self.conn, until="2026-07-25")), 1)

    def test_null_cost_columns_do_not_poison_a_summary(self):
        """costs, gross_pnl and held_bars were added by migration, so pre-migration
        rows carry NULL. summarize() sums them — one NULL turns the whole report
        into None without raising anywhere."""
        signal_id = self._trade(100)
        self.conn.execute(
            "UPDATE paper_trades SET costs = NULL, gross_pnl = NULL, held_bars = NULL "
            "WHERE signal_id = ?", (signal_id,))
        self.conn.commit()
        stats = ledger.summarize(ledger.closed_trades(self.conn))
        self.assertEqual(stats["costs"], 0.0)
        self.assertEqual(stats["net_pnl"], 100)

    # --- grouping ---

    def test_grouping_by_rule(self):
        self._trade(500, rule="momentum_continuation", symbol="M1")
        self._trade(-300, rule="momentum_continuation", symbol="M2")
        self._trade(100, rule="volume_breakout", symbol="V1")
        by_rule = ledger.by_rule(self.conn)
        self.assertEqual(by_rule["momentum_continuation"]["trades"], 2)
        self.assertEqual(by_rule["momentum_continuation"]["net_pnl"], 200)
        self.assertEqual(by_rule["volume_breakout"]["trades"], 1)

    def test_an_empty_confirming_string_counts_as_solo(self):
        """signals.py writes NULL, but a blank string would otherwise read as
        agreement and quietly inflate the cohort."""
        self._trade(100, confirming="")
        self._trade(100, confirming="   ", symbol="B")
        self.assertEqual(ledger.by_cohort(self.conn)["solo"]["trades"], 2)
        self.assertNotIn("confirmed", ledger.by_cohort(self.conn))

    def test_a_real_confirming_rule_counts_as_confirmed(self):
        self._trade(100, confirming="volume_breakout")
        self.assertEqual(ledger.by_cohort(self.conn)["confirmed"]["trades"], 1)

    # --- the check that was missing ---

    def test_every_report_agrees_on_the_trade_count(self):
        for i in range(7):
            self._trade(100 if i % 2 else -50, symbol=f"T{i}")
        expected = 7
        self.assertEqual(journal.summary(self.conn)["closed"], expected)
        self.assertEqual(
            sum(r["trades"] for r in journal.per_rule_live(self.conn).values()), expected)
        self.assertEqual(
            sum(r["trades"] for r in weekly._rule_stats(self.conn).values()), expected)
        self.assertEqual(
            sum(r["trades"] for r in weekly.cohort_stats(self.conn).values()), expected)
        self.assertEqual(gate.snapshot(self.conn, "2026-08-02")["trades"], expected)

    def test_every_report_agrees_on_net_pnl(self):
        for i in range(7):
            self._trade(100 if i % 2 else -50, symbol=f"T{i}")
        expected = ledger.summarize(ledger.closed_trades(self.conn))["net_pnl"]
        self.assertEqual(journal.summary(self.conn)["total_pnl"], expected)
        self.assertEqual(
            round(sum(r["net"] for r in weekly._rule_stats(self.conn).values()), 2), expected)
        self.assertEqual(gate.snapshot(self.conn, "2026-08-02")["net"], expected)

    def test_the_weekly_and_the_gate_agree_on_hit_rate(self):
        """The gate is frozen and the weekly is what you read every Sunday. A
        divergence between them is a divergence in what PASS means."""
        for i in range(10):
            self._trade(100 if i < 3 else -50, symbol=f"T{i}")
        weekly_rate = weekly._rule_stats(self.conn)["momentum_continuation"]["hit_rate"]
        from src import rules_config

        expected = rules_config.RULE_BACKTEST_HIT_RATE["momentum_continuation"]
        gate_drift = gate.snapshot(self.conn, "2026-08-02")["drift"]
        self.assertAlmostEqual(weekly_rate - expected, gate_drift, places=9)

    def test_a_break_even_trade_is_not_a_win_anywhere(self):
        """Zero P&L is the boundary every one of the six implementations had to get
        right independently, and the one most likely to differ."""
        self._trade(0)
        self.assertEqual(journal.summary(self.conn)["wins"], 0)
        self.assertEqual(weekly._rule_stats(self.conn)["momentum_continuation"]["wins"], 0)
        self.assertEqual(ledger.by_cohort(self.conn)["solo"]["wins"], 0)

    def test_an_empty_ledger_reports_zero_not_none(self):
        stats = ledger.totals(self.conn)
        self.assertEqual(stats["trades"], 0)
        self.assertEqual(stats["net_pnl"], 0.0)
        self.assertEqual(journal.summary(self.conn)["closed"], 0)

    def test_the_arithmetic_is_the_backtests(self):
        """Not reimplemented here. The live ledger and the backtest have to be
        comparable field by field, which requires the same function."""
        from src import backtest

        self.assertIs(ledger.summarize, backtest.summarize)


if __name__ == "__main__":
    unittest.main()
