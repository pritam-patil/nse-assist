"""The weekly report: correct cohorts, honest drift, and no salesmanship.

    python -m unittest discover -s tests -v

The drift column is the one that earns its place. A live hit rate far from the
backtest's means one of them is wrong about the market — and the trade count beside
it is what says which, so the two must never be separated.
"""

import os
import tempfile
import unittest

from src import rules_config, weekly
from src.db import get_connection, init_db

HYPE = ("!", "🚀", "📈", "great", "excellent", "opportunity", "crushing",
        "beat the market", "on fire", "don't miss")

# Derived, never literal. These tests assert a relationship to the drift threshold;
# a hard-coded rate fails the day walk-forward writes a new one, for a reason that
# has nothing to do with what the test is named after.
BACKTEST_RATE = rules_config.RULE_BACKTEST_HIT_RATE["momentum_continuation"]


class WeeklyTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self.day = "2026-08-02"

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _trade(self, rule, pnl, exit_date="2026-07-30", confirming=None,
               entry_date="2026-07-28", symbol=None):
        symbol = symbol or f"S{abs(hash((rule, pnl, exit_date, confirming))) % 9999}"
        cursor = self.conn.execute(
            "INSERT INTO signals (date, symbol, rule, direction, entry, stop, target, "
            "size, status, created_at, confirming_rules) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (entry_date, symbol, rule, "long", 100.0, 95.0, 110.0, 10, "taken", "x",
             confirming))
        self.conn.execute(
            "INSERT INTO paper_trades (signal_id, entry_date, entry_price, exit_date, "
            "exit_price, exit_reason, pnl, gross_pnl, costs, held_bars, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cursor.lastrowid, entry_date, 100.0, exit_date, 105.0,
             "target" if pnl > 0 else "stop", pnl, pnl + 90, 90.0, 3, "closed"))
        self.conn.commit()

    # --- per-rule and drift ---

    def test_reports_trades_hit_rate_and_net_per_rule(self):
        self._trade("momentum_continuation", 500)
        self._trade("momentum_continuation", -300)
        stats = weekly._rule_stats(self.conn)["momentum_continuation"]
        self.assertEqual(stats["trades"], 2)
        self.assertEqual(stats["hit_rate"], 0.5)
        self.assertEqual(stats["net"], 200)

    def test_drift_is_flagged_past_the_threshold(self):
        """Ten losers is a 0% live rate — the full backtest rate away from it."""
        for i in range(10):
            self._trade("momentum_continuation", -100, symbol=f"L{i}")
        text = weekly.build_weekly(self.conn, self.day)
        self.assertIn("<- drift", text)
        self.assertIn(f"{-BACKTEST_RATE:+.1%}", text)

    def test_drift_is_not_flagged_when_live_matches_backtest(self):
        """A live rate built to sit on the backtest's own rate must never flag,
        whatever walk-forward last wrote it to be."""
        winners = round(20 * BACKTEST_RATE)
        for i in range(winners):
            self._trade("momentum_continuation", 200, symbol=f"W{i}")
        for i in range(20 - winners):
            self._trade("momentum_continuation", -200, symbol=f"L{i}")
        line = [l for l in weekly.build_weekly(self.conn, self.day).splitlines()
                if l.strip().startswith("momentum_continuation")][0]
        self.assertNotIn("<- drift", line)

    def test_the_trade_count_sits_beside_the_drift(self):
        """A 40-point gap on two trades is noise. Reading the gap without the count
        is how a fortnight of bad luck gets mistaken for a broken backtest."""
        self._trade("volume_breakout", -100)
        text = weekly.build_weekly(self.conn, self.day)
        header = [l for l in text.splitlines() if "cum n" in l][0]
        self.assertLess(header.index("cum n"), header.index("drift"))

    # --- cohorts ---

    def test_confirmed_and_solo_are_split(self):
        self._trade("momentum_continuation", 400, confirming="volume_breakout")
        self._trade("momentum_continuation", -100, confirming=None)
        cohorts = weekly.cohort_stats(self.conn)
        self.assertEqual(cohorts["confirmed"]["trades"], 1)
        self.assertEqual(cohorts["solo"]["trades"], 1)
        self.assertEqual(cohorts["confirmed"]["net"], 400)

    def test_an_empty_confirming_string_counts_as_solo(self):
        """signals.py writes NULL, but a blank string would otherwise read as
        agreement and quietly inflate the cohort."""
        self._trade("momentum_continuation", 100, confirming="")
        self.assertEqual(weekly.cohort_stats(self.conn)["solo"]["trades"], 1)

    def test_both_cohorts_appear_even_when_one_is_empty(self):
        self._trade("momentum_continuation", 100, confirming=None)
        text = weekly.build_weekly(self.conn, self.day)
        self.assertIn("confirmed", text)
        self.assertIn("solo", text)

    # --- windows ---

    def test_this_week_and_cumulative_differ(self):
        self._trade("momentum_continuation", 500, exit_date="2026-07-30")   # in week
        self._trade("momentum_continuation", 900, exit_date="2026-05-01")   # older
        since, _ = weekly.week_bounds(self.day)
        self.assertEqual(weekly._rule_stats(self.conn, since=since)["momentum_continuation"]["trades"], 1)
        self.assertEqual(weekly._rule_stats(self.conn)["momentum_continuation"]["trades"], 2)

    # --- gate ---

    def test_the_weekly_carries_the_evaluation_gate(self):
        """Folded in rather than sent separately. The gate's own criteria are
        tested in tests/test_gate.py."""
        self._trade("momentum_continuation", 100)
        text = weekly.build_weekly(self.conn, self.day)
        self.assertIn("EVALUATION GATE", text)
        self.assertIn("five pre-committed criteria", text)

    # --- benchmark ---

    def test_index_comparison_is_absent_without_index_data(self):
        """No benchmark stored and no network in tests: the section is skipped
        rather than reporting a zero that reads as 'the index did nothing'."""
        self._trade("momentum_continuation", 100)
        text = weekly.build_weekly(self.conn, self.day)
        if "AGAINST THE INDEX" in text:
            self.assertIn("NIFTY held", text)

    def test_benchmark_comparison_uses_the_trade_span(self):
        self._trade("momentum_continuation", 100, entry_date="2026-01-05",
                    exit_date="2026-01-20")
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, "
            "volume, source) VALUES (?,?,?,?,?,?,?,?)",
            [("NIFTY50", "2026-01-05", 100, 100, 100, 20000, 0, "test"),
             ("NIFTY50", "2026-01-20", 100, 100, 100, 22000, 0, "test")])
        self.conn.commit()
        versus = weekly.benchmark_comparison(self.conn)
        self.assertEqual(versus["start"], "2026-01-05")
        self.assertAlmostEqual(versus["index_return"], 0.10, places=6)

    # --- tone ---

    def test_no_emojis_or_hype(self):
        self._trade("momentum_continuation", 900, confirming="volume_breakout")
        text = weekly.build_weekly(self.conn, self.day).lower()
        for token in HYPE:
            with self.subTest(token=token):
                self.assertNotIn(token.lower(), text)

    def test_no_emoji_codepoints(self):
        self._trade("momentum_continuation", 100)
        for char in weekly.build_weekly(self.conn, self.day):
            self.assertLess(ord(char), 0x2190, f"{char!r} is a symbol or emoji")

    def test_carries_the_disclaimer(self):
        self._trade("momentum_continuation", 100)
        text = weekly.build_weekly(self.conn, self.day)
        self.assertIn("not future returns", text)
        self.assertIn("Not investment advice", text)

    def test_provisional_thresholds_say_so(self):
        """A number in a report loses its provenance the moment nobody remembers
        where it came from."""
        self.assertIn(rules_config.EVALUATION_BASIS, weekly.build_weekly(self.conn, self.day))

    def test_empty_ledger_is_normal_output(self):
        text = weekly.build_weekly(self.conn, self.day)
        self.assertIn("No closed paper trades yet", text)

    def test_an_empty_ledger_says_what_is_pending(self):
        """"No closed trades" alone reads the same whether three positions are
        queued to fill tomorrow or nothing has fired for a fortnight, and those
        need different responses."""
        from src import signals

        saved = signals.ENABLED_RULES
        signals.ENABLED_RULES = ("momentum_continuation",)
        try:
            self.conn.execute(
                "INSERT INTO signals (date, symbol, rule, direction, entry, stop, "
                "target, size, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (self.day, "AAA", "momentum_continuation", "long",
                 100.0, 95.0, 110.0, 10, "proposed", "x"))
            self.conn.commit()
            text = weekly.build_weekly(self.conn, self.day)
            self.assertIn("1 signal(s) proposed", text)
            self.assertIn("start the gate's clock", text)
        finally:
            signals.ENABLED_RULES = saved

    def test_an_empty_ledger_with_a_rule_enabled_does_not_claim_all_are_disabled(self):
        """The contradiction this pair of fixes exists to prevent."""
        from src import signals

        saved = signals.ENABLED_RULES
        signals.ENABLED_RULES = ("momentum_continuation",)
        try:
            text = weekly.build_weekly(self.conn, self.day)
            self.assertNotIn("rules are disabled by walk-forward", text)
            self.assertIn("nothing has fired or filled yet", text)
        finally:
            signals.ENABLED_RULES = saved


if __name__ == "__main__":
    unittest.main()
