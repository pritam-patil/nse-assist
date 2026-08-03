"""The evaluation gate — and the frozen thresholds it judges against.

    python -m unittest discover -s tests -v

THE FIRST TEST CLASS IS THE POINT OF THIS FILE.

It asserts every pre-committed threshold by literal value. That looks like testing
that a constant equals itself, and it is — deliberately. The mechanism is social,
not technical: an edit to rules_config.py fails this suite until someone edits this
file too, so relaxing a criterion after seeing a near-miss costs a second commit
that says so in the diff.

The failure being defended against is not malice. It is the reasonable-sounding
week-seven conversation in which 30 trades was always a bit arbitrary and 15 points
was maybe tight for a small sample. Each step is defensible; the conclusion is
fitted to the result. A test that has to be edited is a speed bump in exactly the
right place.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from src import gate, rules_config, weekly
from src.db import get_connection, init_db

# The live backtest rate for the rule the fixtures use. Derived, never literal —
# these tests assert a RELATIONSHIP to the threshold, not a coincidence with
# whatever number walk-forward last wrote.
MATCHING_HIT_RATE = rules_config.RULE_BACKTEST_HIT_RATE["momentum_continuation"]


class FrozenThresholdsTestCase(unittest.TestCase):
    """Literal values, pre-committed 2026-08-02. See the module docstring before
    changing any of these."""

    def test_sample_window_is_six_weeks(self):
        self.assertEqual(rules_config.EVALUATION_WEEKS_REQUIRED, 6)
        self.assertEqual(rules_config.EVALUATION_DAYS_REQUIRED, 42)

    def test_minimum_closed_trades_is_thirty(self):
        self.assertEqual(rules_config.EVALUATION_MIN_TRADES, 30)

    def test_cumulative_pnl_must_be_strictly_positive(self):
        self.assertEqual(rules_config.GATE_MIN_CUMULATIVE_PNL, 0.0)

    def test_expectancy_must_be_strictly_positive(self):
        self.assertEqual(rules_config.GATE_MIN_EXPECTANCY, 0.0)

    def test_hit_rate_drift_limit_is_fifteen_points(self):
        self.assertEqual(rules_config.GATE_MAX_HIT_RATE_DRIFT, 0.15)

    def test_the_benchmark_criterion_is_active(self):
        self.assertTrue(rules_config.GATE_BEAT_BENCHMARK)

    def test_the_freeze_date_is_recorded(self):
        self.assertEqual(rules_config.GATE_FROZEN_ON, "2026-08-02")

    def test_there_are_exactly_five_criteria(self):
        """A sixth added quietly would change what PASS means."""
        conn = get_connection(":memory:")
        init_db(conn)
        try:
            rows, _, _ = gate.criteria(conn, "2026-08-02")
            self.assertEqual(len(rows), 5)
            self.assertEqual(
                [r["key"] for r in rows],
                ["sample", "cumulative_pnl", "expectancy", "drift", "benchmark"],
            )
        finally:
            conn.close()


class GateTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self.day = "2026-08-02"

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _trade(self, pnl, rule="momentum_continuation", entry="2026-06-01",
               exit_date="2026-06-05", symbol=None):
        symbol = symbol or f"S{abs(hash((rule, pnl, exit_date, symbol))) % 99999}"
        cursor = self.conn.execute(
            "INSERT INTO signals (date, symbol, rule, direction, entry, stop, target, "
            "size, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (entry, symbol, rule, "long", 100.0, 95.0, 110.0, 10, "taken", "x"))
        self.conn.execute(
            "INSERT INTO paper_trades (signal_id, entry_date, entry_price, exit_date, "
            "exit_price, exit_reason, pnl, gross_pnl, costs, held_bars, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cursor.lastrowid, entry, 100.0, exit_date, 105.0,
             "target" if pnl > 0 else "stop", pnl, pnl + 90, 90.0, 3, "closed"))
        self.conn.commit()

    def _full_sample(self, win=100, loss=None, hit_rate=None, first="2026-06-01"):
        """30 trades spanning more than 6 weeks — enough to close the window.

        `loss` defaults to `win`, which nets exactly zero at hit_rate=0.5. That is
        a fine fixture for testing break-even and a useless one for testing a pass,
        so a test wanting a profitable sample must make the winners bigger.

        hit_rate=None makes every trade a winner — 100% against whatever the
        backtest rate is, which is always far outside the drift limit. Tests
        needing the drift criterion to PASS must pass MATCHING_HIT_RATE, derived
        from rules_config rather than hard-coded: a literal here would fail the
        day walk-forward writes a new rate, for a reason unrelated to the test's
        name. That has already happened once.
        """
        loss = abs(win if loss is None else loss)
        start = date.fromisoformat(first)
        winners = round(30 * hit_rate) if hit_rate is not None else 30
        for i in range(30):
            entry = (start + timedelta(days=i)).isoformat()
            exit_date = (start + timedelta(days=i + 3)).isoformat()
            self._trade(win if i < winners else -loss,
                        entry=entry, exit_date=exit_date, symbol=f"SYM{i}")

    def _by_key(self, as_of=None):
        rows, _, _ = gate.criteria(self.conn, as_of or self.day)
        return {r["key"]: r for r in rows}

    # --- the three states ---

    def test_an_empty_ledger_is_insufficient_not_failing(self):
        """Being early is not a failure, and marking it as one makes the first five
        weeks look like five weeks of bad news."""
        rows = self._by_key()
        for key in ("sample", "cumulative_pnl", "expectancy", "drift", "benchmark"):
            with self.subTest(criterion=key):
                self.assertEqual(rows[key]["status"], gate.INSUFFICIENT)

    def test_the_verdict_is_in_progress_while_the_window_is_open(self):
        self._trade(-500)
        rows, _, _ = gate.criteria(self.conn, self.day)
        self.assertEqual(gate.verdict(rows), gate.VERDICT_IN_PROGRESS)

    def test_a_losing_criterion_inside_the_window_is_not_yet_a_fail(self):
        """There are trades still to come that could move it."""
        self._trade(-500)
        self.assertEqual(self._by_key()["cumulative_pnl"]["status"], gate.FAIL)
        rows, _, _ = gate.criteria(self.conn, self.day)
        self.assertEqual(gate.verdict(rows), gate.VERDICT_IN_PROGRESS)

    def test_the_verdict_turns_final_once_the_sample_is_complete(self):
        self._full_sample(win=-100)
        rows, _, _ = gate.criteria(self.conn, self.day)
        self.assertEqual(gate.verdict(rows), gate.VERDICT_FAIL)

    # --- criterion 1: sample ---

    def test_the_sample_needs_both_thresholds_not_either(self):
        """'Whichever comes later' — 30 trades inside a fortnight is not six weeks."""
        for i in range(30):
            self._trade(100, entry="2026-07-25", exit_date="2026-07-28", symbol=f"Q{i}")
        self.assertEqual(self._by_key()["sample"]["status"], gate.INSUFFICIENT)

    def test_six_weeks_with_too_few_trades_is_also_insufficient(self):
        self._trade(100, entry="2026-05-01", exit_date="2026-05-05")
        self.assertEqual(self._by_key()["sample"]["status"], gate.INSUFFICIENT)

    def test_both_met_passes_the_sample_criterion(self):
        self._full_sample()
        self.assertEqual(self._by_key()["sample"]["status"], gate.PASS)

    # --- criterion 2: cumulative P&L ---

    def test_break_even_fails_rather_than_passes(self):
        """Zero means the rules paid their own transaction costs and nothing else."""
        self._full_sample()
        self.conn.execute("UPDATE paper_trades SET pnl = 0")
        self.conn.commit()
        self.assertEqual(self._by_key()["cumulative_pnl"]["status"], gate.FAIL)

    def test_positive_cumulative_passes(self):
        self._full_sample(win=100)
        self.assertEqual(self._by_key()["cumulative_pnl"]["status"], gate.PASS)

    # --- criterion 3: expectancy ---

    def test_expectancy_and_cumulative_pnl_always_agree(self):
        """Pinning a redundancy in the pre-committed criteria, not a behaviour.

        Expectancy as specified is mean P&L per trade — net divided by count — so
        for any non-zero sample the two criteria carry the same sign and can never
        disagree. Criterion 3 can never be the one that fails a gate that criterion
        2 passed.

        It is implemented and reported anyway, because the criteria were frozen
        before evaluation and quietly dropping one is exactly the edit this suite
        exists to make expensive. This test records that the redundancy is known
        rather than accidental. See the note in the README.
        """
        for total in (3000, -3000, 1):
            with self.subTest(total=total):
                self.conn.execute("DELETE FROM paper_trades")
                self.conn.execute("DELETE FROM signals")
                self.conn.commit()
                self._full_sample(win=abs(total) // 30 or 1,
                                  loss=0 if total > 0 else abs(total) // 30 or 1,
                                  hit_rate=1.0 if total > 0 else 0.0)
                rows = self._by_key()
                self.assertEqual(rows["cumulative_pnl"]["status"],
                                 rows["expectancy"]["status"])

    # --- criterion 4: drift ---

    def test_drift_within_the_limit_passes(self):
        """A live rate at the backtest's own rate is zero drift, by construction."""
        self._full_sample(hit_rate=MATCHING_HIT_RATE)
        rows = self._by_key()
        self.assertEqual(rows["drift"]["status"], gate.PASS)

    def test_a_live_rate_matching_the_backtest_is_not_flagged_however_it_moves(self):
        """The guard on the whole pattern: whatever walk-forward writes, matching
        it must never read as drift."""
        self._full_sample(hit_rate=MATCHING_HIT_RATE)
        drift = gate.snapshot(self.conn, self.day)["drift"]
        self.assertLess(abs(drift), rules_config.GATE_MAX_HIT_RATE_DRIFT)

    def test_drift_beyond_the_limit_fails(self):
        """Every trade a winner is 100%, which is outside the limit against any
        plausible backtest rate."""
        self._full_sample(hit_rate=1.0)
        self.assertEqual(self._by_key()["drift"]["status"], gate.FAIL)

    def test_the_worst_rule_binds_rather_than_the_average(self):
        """Averaging would let a rule that fired twice and matched perfectly cancel
        one that is badly off."""
        for i in range(15):
            self._trade(100, rule="momentum_continuation", entry="2026-06-01",
                        exit_date="2026-06-05", symbol=f"M{i}")
        for i in range(15):
            self._trade(100 if i < 7 else -100, rule="oversold_reversion",
                        entry="2026-06-01", exit_date="2026-06-05", symbol=f"R{i}")
        drift = gate.snapshot(self.conn, self.day)["drift"]
        self.assertGreater(abs(drift), rules_config.GATE_MAX_HIT_RATE_DRIFT)

    # --- criterion 5: benchmark ---

    def test_the_benchmark_criterion_is_insufficient_without_index_data(self):
        """No baseline stored means no comparison — not a silent pass."""
        self._full_sample()
        self.assertEqual(self._by_key()["benchmark"]["status"], gate.INSUFFICIENT)

    def test_beating_the_index_passes(self):
        self._full_sample(win=500)
        self._store_index(20000, 20100)   # +0.5% on 125,000 = 625
        self.assertEqual(self._by_key()["benchmark"]["status"], gate.PASS)

    def test_losing_to_the_index_fails(self):
        self._full_sample(win=1)
        self._store_index(20000, 24000)   # +20% on 125,000 = 25,000
        self.assertEqual(self._by_key()["benchmark"]["status"], gate.FAIL)

    def test_matching_the_index_passes(self):
        """Ties pass — matching the index while holding cash most of the time is a
        real result."""
        self._full_sample(win=100)
        net = gate.snapshot(self.conn, self.day)["net"]
        from src import risk_config

        ratio = net / risk_config.MAX_TOTAL_CAPITAL
        self._store_index(20000, 20000 * (1 + ratio))
        self.assertEqual(self._by_key()["benchmark"]["status"], gate.PASS)

    def _store_index(self, first, last):
        trades = self.conn.execute(
            "SELECT MIN(entry_date), MAX(exit_date) FROM paper_trades WHERE status='closed'"
        ).fetchone()
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, "
            "volume, source) VALUES (?,?,?,?,?,?,?,?)",
            [("NIFTY50", trades[0], first, first, first, first, 0, "test"),
             ("NIFTY50", trades[1], last, last, last, last, 0, "test")])
        self.conn.commit()

    # --- all five together ---

    def test_pass_requires_every_criterion(self):
        self._full_sample(win=500, loss=100, hit_rate=MATCHING_HIT_RATE)
        self._store_index(20000, 20100)
        rows, _, _ = gate.criteria(self.conn, self.day)
        self.assertEqual(gate.verdict(rows), gate.VERDICT_PASS)

    def test_one_failing_criterion_denies_the_pass(self):
        """The whole point of five criteria: four out of five is not a pass."""
        self._full_sample(win=500, loss=100, hit_rate=MATCHING_HIT_RATE)
        self._store_index(20000, 30000)   # index up 50%, paper cannot match it
        rows, _, _ = gate.criteria(self.conn, self.day)
        self.assertEqual(sum(1 for r in rows if r["status"] == gate.PASS), 4)
        self.assertEqual(gate.verdict(rows), gate.VERDICT_FAIL)

    # --- trends ---

    def test_a_trend_is_reported_once_there_is_a_prior_week(self):
        self._trade(100, entry="2026-06-01", exit_date="2026-06-05")
        self._trade(100, entry="2026-07-28", exit_date="2026-08-01")
        self.assertEqual(self._by_key()["sample"]["trend"], "improving")

    def test_a_worsening_pnl_trend_is_named(self):
        self._trade(500, entry="2026-06-01", exit_date="2026-06-05")
        self._trade(-900, entry="2026-07-28", exit_date="2026-08-01")
        self.assertEqual(self._by_key()["cumulative_pnl"]["trend"], "worsening")

    def test_shrinking_drift_counts_as_improving(self):
        """Smaller absolute drift is better, so the comparison is on |drift| —
        a naive greater-than would call a drift moving from -40 to -5 'improving'
        and one moving from +40 to +5 'worsening'."""
        for i in range(10):
            self._trade(100, entry="2026-06-01", exit_date="2026-06-05", symbol=f"W{i}")
        for i in range(10):
            self._trade(-100, entry="2026-07-28", exit_date="2026-08-01", symbol=f"L{i}")
        self.assertEqual(self._by_key()["drift"]["trend"], "improving")

    # --- the message ---

    def test_the_message_states_the_freeze_date(self):
        text = gate.build_gate(self.conn, self.day)
        self.assertIn(rules_config.GATE_FROZEN_ON, text)
        self.assertIn("frozen", text)

    def test_the_message_lists_all_five_criteria(self):
        text = gate.build_gate(self.conn, self.day)
        for n in range(1, 6):
            self.assertIn(f"{n}.", text)

    def test_a_fail_is_described_as_the_system_working(self):
        """The week it prints FAIL is the week nobody wants to read it.

        Forces the pipeline-test flag off rather than reading the live config: the
        two FAIL messages say different things on purpose, and a test that picks
        one by whatever happens to be enabled today asserts nothing.
        """
        saved = rules_config.RULE_ENABLED.copy()
        rules_config.RULE_ENABLED.update({r: False for r in rules_config.PIPELINE_TEST_RULES})
        try:
            self._full_sample(win=-100)
            text = gate.build_gate(self.conn, self.day)
            self.assertIn("VERDICT: FAIL", text)
            self.assertIn("gate working", text)
            self.assertIn("paper-only", text)
        finally:
            rules_config.RULE_ENABLED.clear()
            rules_config.RULE_ENABLED.update(saved)

    def test_a_fail_during_a_pipeline_test_is_marked_expected(self):
        """A losing record from a rule enabled to exercise the plumbing is not a
        strategy failing evaluation, and the verdict alone cannot tell them apart."""
        saved = rules_config.RULE_ENABLED.copy()
        rules_config.RULE_ENABLED.update({r: True for r in rules_config.PIPELINE_TEST_RULES})
        try:
            self._full_sample(win=-100)
            text = gate.build_gate(self.conn, self.day)
            self.assertIn("VERDICT: FAIL", text)
            self.assertIn("EXPECTED", text)
            self.assertIn("not a finding about the strategy", text)
        finally:
            rules_config.RULE_ENABLED.clear()
            rules_config.RULE_ENABLED.update(saved)

    def test_a_pass_does_not_read_as_permission_to_trade(self):
        self._full_sample(win=500, loss=100, hit_rate=MATCHING_HIT_RATE)
        self._store_index(20000, 20100)
        text = gate.build_gate(self.conn, self.day)
        self.assertIn("VERDICT: PASS", text)
        self.assertIn("not a recommendation", text)

    def test_the_message_says_the_thresholds_are_pinned_by_tests(self):
        self.assertIn("tests/test_gate.py", gate.build_gate(self.conn, self.day))

    def test_no_emoji_codepoints(self):
        self._full_sample()
        for char in gate.build_gate(self.conn, self.day):
            self.assertLess(ord(char), 0x2190, f"{char!r} is a symbol or emoji")

    # --- point-in-time ---

    def test_the_snapshot_ignores_trades_that_close_after_the_as_of_date(self):
        """Same discipline features.py applies to prices. A trend computed against
        a baseline that can see the future is not a trend."""
        self._trade(100, entry="2026-06-01", exit_date="2026-06-05")
        self._trade(9999, entry="2026-07-28", exit_date="2026-08-01")
        self.assertEqual(gate.snapshot(self.conn, "2026-06-10")["net"], 100)


class WeeklyIntegrationTestCase(unittest.TestCase):
    def test_the_weekly_carries_the_gate(self):
        conn = get_connection(":memory:")
        init_db(conn)
        try:
            text = weekly.build_weekly(conn, "2026-08-02")
            self.assertIn("EVALUATION GATE", text)
            self.assertIn("all must hold", text)
        finally:
            conn.close()


class PipelineTestBannerTestCase(unittest.TestCase):
    """A rule enabled to exercise the plumbing must never read as a strategy view.

    The risk is entirely one of impression: an enabled flag and a losing paper
    record look identical whether the rule was believed in or deliberately known to
    lose. Three weeks after anyone remembers setting it, only the banner
    distinguishes them.
    """

    def test_the_banner_names_the_rule_and_the_date(self):
        banner = rules_config.pipeline_test_banner()
        self.assertIsNotNone(banner, "no pipeline test active — adjust this suite")
        self.assertIn("momentum_continuation", banner)
        self.assertIn(rules_config.PIPELINE_TEST_SINCE, banner)

    def test_the_banner_says_it_is_not_a_strategy_view(self):
        banner = rules_config.pipeline_test_banner()
        self.assertIn("not a strategy view", banner)
        self.assertIn("expected outcome", banner)

    def test_the_banner_carries_the_measured_loss(self):
        """Without the number, 'this is a test' is a reassurance rather than a
        fact the reader can check."""
        self.assertIn("-257", rules_config.pipeline_test_banner())

    def test_the_banner_is_silent_when_no_test_is_running(self):
        saved = rules_config.RULE_ENABLED.copy()
        rules_config.RULE_ENABLED.update(
            {r: False for r in rules_config.PIPELINE_TEST_RULES})
        try:
            self.assertIsNone(rules_config.pipeline_test_banner())
        finally:
            rules_config.RULE_ENABLED.clear()
            rules_config.RULE_ENABLED.update(saved)

    def test_a_listed_but_disabled_rule_does_not_count_as_active(self):
        """PIPELINE_TEST_RULES is the roster; RULE_ENABLED decides. A rule left on
        the roster after the test ended must not keep announcing itself."""
        saved = rules_config.RULE_ENABLED.copy()
        rules_config.RULE_ENABLED["momentum_continuation"] = False
        try:
            self.assertNotIn("momentum_continuation", rules_config.active_pipeline_tests())
        finally:
            rules_config.RULE_ENABLED.clear()
            rules_config.RULE_ENABLED.update(saved)

    def test_every_scheduled_message_carries_it(self):
        """Four messages, one helper, so they cannot describe the same flag
        differently."""
        from src import brief, deliver, weekly

        conn = get_connection(":memory:")
        init_db(conn)
        try:
            for text in (brief.build_brief(conn),
                         deliver.build_report(conn, "2026-08-02"),
                         weekly.build_weekly(conn, "2026-08-02")):
                with self.subTest(message=text.splitlines()[0][:30]):
                    self.assertIn("PIPELINE TEST ACTIVE", text)
        finally:
            conn.close()

    def test_the_enabled_rule_is_the_one_that_fires_often_enough(self):
        """274 out-of-sample trades against 35 and 88. A test that produces four
        fills in six weeks exercises nothing."""
        self.assertEqual(rules_config.PIPELINE_TEST_RULES, ("momentum_continuation",))


if __name__ == "__main__":
    unittest.main()
