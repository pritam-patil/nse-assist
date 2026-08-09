"""The signal report: the derived surviving set, eligibility, and the labels.

    python -m unittest discover -s tests -v

The load-bearing tests are the derivation ones: no cell may reach the report
without clearing the burst-7 bar from the actual stored evidence, and an empty
surviving set must produce the nothing-to-signal text, exit code 0 — the
correct answer, not an error.
"""

import unittest
from unittest import mock

import pandas as pd

from data import signal, upcoming


def grid_frame(rows):
    """[(symbol, ex_date, e, x, net_return, in_sample)] with pairing dates."""
    return pd.DataFrame({
        "symbol": [r[0] for r in rows],
        "ex_date": pd.to_datetime([r[1] for r in rows]),
        "entry_days_before": [r[2] for r in rows],
        "exit_days_after": [r[3] for r in rows],
        "net_return": [r[4] for r in rows],
        "net": [r[4] * 10_000 for r in rows],
        "in_sample": [r[5] for r in rows],
        "entry_date": pd.to_datetime(["2024-01-01"] * len(rows)),
        "exit_date": pd.to_datetime(["2024-02-01"] * len(rows)),
    })


# NIFTY +0.5% over the shared window.
CLOSES = {pd.Timestamp("2024-01-01"): 100.0, pd.Timestamp("2024-02-01"): 100.5}


def healthy_cell():
    """One tune-positive cell whose OOS excess beats +0.5% comfortably."""
    return grid_frame([
        ("A", "2022-03-01", 20, 0, 0.010, True),
        ("B", "2022-03-02", 20, 0, 0.012, True),
        ("C", "2024-03-01", 20, 0, 0.020, False),
        ("D", "2024-03-02", 20, 0, 0.018, False),
        ("E", "2024-03-03", 20, 0, 0.016, False),
    ])


class SurvivorTests(unittest.TestCase):
    def test_a_cell_clearing_both_bars_survives_with_its_stats(self):
        trades = healthy_cell()
        stressed = trades.copy()
        stressed["net_return"] = trades["net_return"] - 0.004   # 3x bound, still up
        with mock.patch.object(signal.study_stress, "resimulate_cells",
                               return_value=stressed):
            survivors = signal.surviving_cells(trades, CLOSES)
        self.assertEqual(len(survivors), 1)
        survivor = survivors[0]
        self.assertEqual(survivor["cell"], (20, 0))
        self.assertEqual(survivor["n"], 3)
        self.assertAlmostEqual(survivor["median_return"], 0.018)
        self.assertGreater(survivor["stressed_excess"], 0)

    def test_failing_the_stressed_bar_excludes_the_cell(self):
        trades = healthy_cell()
        stressed = trades.copy()
        stressed["net_return"] = 0.001   # below NIFTY's 0.5% after stress
        with mock.patch.object(signal.study_stress, "resimulate_cells",
                               return_value=stressed):
            self.assertEqual(signal.surviving_cells(trades, CLOSES), [])

    def test_failing_the_baseline_bar_excludes_the_cell(self):
        trades = healthy_cell()
        trades.loc[~trades["in_sample"], "net_return"] = 0.004   # under NIFTY
        with mock.patch.object(signal.study_stress, "resimulate_cells",
                               return_value=trades):
            self.assertEqual(signal.surviving_cells(trades, CLOSES), [])

    def test_no_tune_positive_cells_means_no_survivors(self):
        trades = healthy_cell()
        trades.loc[trades["in_sample"], "net_return"] = -0.01
        self.assertEqual(signal.surviving_cells(trades, CLOSES), [])


class EligibilityTests(unittest.TestCase):
    TABLE = pd.DataFrame({
        "symbol": ["FAR", "NEAR", "TBA"],
        "ex_date": [pd.Timestamp("2026-09-20"), pd.Timestamp("2026-08-12"), pd.NaT],
        "record_date": [pd.NaT] * 3,
        "amount": [5.0, 2.0, None],
        "est_yield_pct": [1.2, 3.4, None],
        "liquidity": ["high", "mid", "low"],
        "source": ["corporate-actions"] * 3,
    })

    def test_only_ex_dates_far_enough_out_qualify(self):
        # e=20 sessions needs >= 28 calendar days from 2026-08-09.
        kept = signal.eligible(self.TABLE, 20, today="2026-08-09")
        self.assertEqual(list(kept["symbol"]), ["FAR"])

    def test_a_short_entry_admits_nearer_events_but_never_tba(self):
        kept = signal.eligible(self.TABLE, 1, today="2026-08-09")
        self.assertEqual(sorted(kept["symbol"]), ["FAR", "NEAR"])

    def test_ranking_is_by_estimated_yield(self):
        ranked = signal.rank(self.TABLE.dropna(subset=["ex_date"]))
        self.assertEqual(list(ranked["symbol"]), ["NEAR", "FAR"])


class ReportTests(unittest.TestCase):
    SURVIVOR = {"cell": (20, 0), "n": 2158, "median_return": 0.0095,
                "p25": 0.0032, "p75": 0.0131, "hit_rate": 0.56,
                "median_excess": 0.002, "stressed_excess": 0.0005}

    def test_the_report_carries_its_labels_and_the_dispersion_band(self):
        lines = signal.report([self.SURVIVOR], healthy_cell(),
                              EligibilityTests.TABLE, notional=100_000,
                              today="2026-08-09")
        text = "\n".join(lines)
        self.assertIn("PERSONAL USE", text)
        self.assertIn("not advice", text)
        self.assertIn("tuned on 2022-03-01 to 2022-03-02", text)
        self.assertIn("validation on 2024-03-01 to 2024-03-03", text)
        self.assertIn("+0.32% and +1.31%", text)             # the IQR band
        self.assertIn("quarter did worse", text)
        self.assertIn("FAR", text)
        self.assertIn("~+950", text)                          # 0.95% of 100k
        self.assertNotIn("NEAR", text)                        # too close for e=20

    def test_an_empty_calendar_says_so_instead_of_ranking_nothing(self):
        empty = EligibilityTests.TABLE.iloc[0:0]
        lines = signal.report([self.SURVIVOR], healthy_cell(), empty,
                              notional=100_000, today="2026-08-09")
        self.assertIn("no eligible candidates", "\n".join(lines))


class RunTests(unittest.TestCase):
    def test_no_survivors_is_the_correct_answer_not_an_error(self):
        trades = healthy_cell()
        with mock.patch.object(signal.study_grid, "with_context",
                               return_value=trades), \
             mock.patch.object(signal.study_specials, "load_grid_trades",
                               return_value=trades), \
             mock.patch.object(signal.study_exdate, "nifty_closes",
                               return_value=CLOSES), \
             mock.patch.object(signal, "surviving_cells", return_value=[]), \
             mock.patch("builtins.print") as told:
            self.assertEqual(signal.run(), 0)
        text = "\n".join(str(call) for call in told.call_args_list)
        self.assertIn("NOTHING TO SIGNAL", text)
        self.assertIn("verdict in RESULTS.md stands", text)

    def test_survivors_without_a_calendar_snapshot_ask_for_one(self):
        trades = healthy_cell()
        with mock.patch.object(signal.study_grid, "with_context",
                               return_value=trades), \
             mock.patch.object(signal.study_specials, "load_grid_trades",
                               return_value=trades), \
             mock.patch.object(signal.study_exdate, "nifty_closes",
                               return_value=CLOSES), \
             mock.patch.object(signal, "surviving_cells",
                               return_value=[ReportTests.SURVIVOR]), \
             mock.patch.object(upcoming, "OUT_PATH") as path:
            path.exists.return_value = False
            self.assertEqual(signal.run(), 1)


if __name__ == "__main__":
    unittest.main()
