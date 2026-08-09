"""The specials retrofit: join integrity, the change rule, the sanity prints.

    python -m unittest discover -s tests -v

The join tests are the guard that replaced the fingerprint check — a trade the
events table cannot match, or an amount that disagrees across the join, must
stop the analysis rather than dilute it.
"""

import unittest
from unittest import mock

import pandas as pd

from data import study_specials
from data.frictions import Config


def trades(rows):
    """[(symbol, ex_date, amount, net_return, in_sample, e, x)]"""
    return pd.DataFrame({
        "symbol": [r[0] for r in rows],
        "ex_date": pd.to_datetime([r[1] for r in rows]),
        "amount": [r[2] for r in rows],
        "net_return": [r[3] for r in rows],
        "net": [r[3] * 10_000 for r in rows],
        "in_sample": [r[4] for r in rows],
        "entry_days_before": [r[5] for r in rows],
        "exit_days_after": [r[6] for r in rows],
        "entry_date": pd.to_datetime(["2024-01-01"] * len(rows)),
        "exit_date": pd.to_datetime(["2024-02-01"] * len(rows)),
    })


def event_table(rows):
    """[(symbol, ex_date, amount, special, yield_pct)]"""
    return pd.DataFrame({
        "symbol": [r[0] for r in rows],
        "ex_date": pd.to_datetime([r[1] for r in rows]),
        "amount": [r[2] for r in rows],
        "special": [r[3] for r in rows],
        "yield_pct": [r[4] for r in rows],
    })


CLOSES = {pd.Timestamp("2024-01-01"): 100.0, pd.Timestamp("2024-02-01"): 100.5}


class JoinTests(unittest.TestCase):
    def test_flags_attach_by_symbol_and_ex_date(self):
        log = trades([("A", "2024-03-01", 5.0, 0.01, False, 20, 0)])
        table = event_table([("A", "2024-03-01", 5.0, True, 6.0)])
        joined = study_specials.join_flags(log, table)
        self.assertTrue(bool(joined["special"].iloc[0]))

    def test_an_unmatched_trade_stops_the_analysis(self):
        log = trades([("A", "2024-03-01", 5.0, 0.01, False, 20, 0),
                      ("B", "2024-03-01", 2.0, 0.01, False, 20, 0)])
        table = event_table([("A", "2024-03-01", 5.0, True, 6.0)])
        with self.assertRaises(RuntimeError) as caught:
            study_specials.join_flags(log, table)
        self.assertIn("diverged", str(caught.exception))

    def test_an_amount_disagreement_stops_the_analysis(self):
        log = trades([("A", "2024-03-01", 5.0, 0.01, False, 20, 0)])
        table = event_table([("A", "2024-03-01", 5.5, True, 6.0)])
        with self.assertRaises(RuntimeError) as caught:
            study_specials.join_flags(log, table)
        self.assertIn("amounts disagree", str(caught.exception))


class SliceTests(unittest.TestCase):
    def test_slices_are_out_of_sample_and_flag_pure(self):
        log = trades([
            ("A", "2024-03-01", 5.0, 0.020, False, 20, 0),   # special, OOS
            ("B", "2024-03-02", 2.0, 0.001, False, 20, 0),   # regular, OOS
            ("C", "2024-03-03", 9.0, 0.500, True, 20, 0),    # special, tune: out
            ("D", "2024-03-04", 9.0, 0.500, False, 5, 1),    # other cell: out
        ])
        table = event_table([("A", "2024-03-01", 5.0, True, 6.0),
                             ("B", "2024-03-02", 2.0, False, 1.0),
                             ("C", "2024-03-03", 9.0, True, 7.0),
                             ("D", "2024-03-04", 9.0, True, 7.0)])
        joined = study_specials.join_flags(log, table)
        special = study_specials.slice_stats(joined, (20, 0), CLOSES, special=True)
        regular = study_specials.slice_stats(joined, (20, 0), CLOSES, special=False)
        self.assertEqual(special["n"], 1)
        self.assertEqual(regular["n"], 1)
        # NIFTY made +0.5% over the window; A's excess is 2% - 0.5%.
        self.assertAlmostEqual(special["median_excess"], 0.015)


class ChangeRuleTests(unittest.TestCase):
    BOUND = 0.004   # 3x slippage bound at 10 bps/side

    def test_below_the_trade_floor_never_changes_the_verdict(self):
        stats = {"n": 29, "median_net": 0.09, "hit_rate": 0.9,
                 "median_excess": 0.08}
        changes, reason = study_specials.verdict_change(stats, self.BOUND)
        self.assertFalse(changes)
        self.assertIn("floor", reason)

    def test_an_excess_inside_the_slippage_bound_does_not_change_it(self):
        stats = {"n": 60, "median_net": 0.006, "hit_rate": 0.6,
                 "median_excess": 0.003}
        changes, reason = study_specials.verdict_change(stats, self.BOUND)
        self.assertFalse(changes)
        self.assertIn("slippage", reason)

    def test_enough_trades_clearing_the_bound_qualifies_the_verdict(self):
        stats = {"n": 60, "median_net": 0.02, "hit_rate": 0.7,
                 "median_excess": 0.015}
        changes, reason = study_specials.verdict_change(stats, self.BOUND)
        self.assertTrue(changes)
        self.assertIn("pre-committed bar", reason)

    def test_a_failed_sanity_check_voids_the_question_however_good_the_numbers(self):
        stats = {"n": 300, "median_net": 0.05, "hit_rate": 0.9,
                 "median_excess": 0.04}
        changes, reason = study_specials.verdict_change(stats, self.BOUND,
                                                        sanity_ok=False)
        self.assertFalse(changes)
        self.assertIn("mislabeled", reason)


class SanityTests(unittest.TestCase):
    def test_the_slippage_bound_is_direct_impact_only(self):
        cfg = mock.Mock(slippage_bps=10.0)
        # (3-1) x 2 x 10 bps = 40 bps.
        self.assertAlmostEqual(study_specials.slippage_bound(cfg, 3), 0.004)

    def test_top_flagged_names_which_rule_fired(self):
        table = event_table([("A", "2024-03-01", 50.0, True, 9.0),
                             ("B", "2024-03-02", 6.1, True, 1.2),
                             ("C", "2024-03-03", 2.0, False, 1.0)])
        top = study_specials.top_flagged(table)
        self.assertEqual(len(top), 2)
        self.assertEqual(list(top["rule"]), ["yield", "amount"])

    def test_flagged_share_is_the_minority_check(self):
        table = event_table([("A", "2024-03-01", 50.0, True, 9.0)] +
                            [("B", f"2024-04-{d:02d}", 2.0, False, 1.0)
                             for d in range(1, 10)])
        self.assertAlmostEqual(study_specials.flagged_share(table), 0.1)


class MarkdownTests(unittest.TestCase):
    def _fixtures(self, padding):
        """One special and one regular trade, plus `padding` unflagged events
        to steer the flagged share across the sanity threshold."""
        log = trades([("A", "2024-03-01", 5.0, 0.02, False, 20, 0),
                      ("B", "2024-03-02", 2.0, 0.001, False, 20, 0)])
        rows = [("A", "2024-03-01", 5.0, True, 6.0),
                ("B", "2024-03-02", 2.0, False, 1.0)]
        rows += [(f"P{i}", f"2024-05-{i + 1:02d}", 1.0, False, 1.0)
                 for i in range(padding)]
        return log, event_table(rows)

    def test_the_section_states_the_verdict_outcome_explicitly(self):
        log, table = self._fixtures(padding=18)   # 1 of 20 flagged: sane
        joined = study_specials.join_flags(log, table)
        section = study_specials.results_markdown(
            table, joined, [(20, 0)], (20, 0), CLOSES, bound=0.004)
        self.assertIn(study_specials.MARK_START, section)
        self.assertIn("verdict is unchanged", section)
        self.assertIn("floor", section)
        self.assertIn("| special | 1 |", section)
        self.assertNotIn("Sanity check FAILED", section)

    def test_a_failed_sanity_check_is_stated_and_gates_the_verdict(self):
        log, table = self._fixtures(padding=0)    # 1 of 2 flagged: not sane
        joined = study_specials.join_flags(log, table)
        section = study_specials.results_markdown(
            table, joined, [(20, 0)], (20, 0), CLOSES, bound=0.004)
        self.assertIn("Sanity check FAILED", section)
        self.assertIn("verdict is unchanged", section)
        self.assertIn("mislabeled", section)


if __name__ == "__main__":
    unittest.main()
