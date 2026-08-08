"""The stress battery: pairing, the pre-committed rule, and the verdict text.

    python -m unittest discover -s tests -v

The rule tests matter most: the verdict function must apply the docstring's
decision rule mechanically — a diagnostic row (special dividends only) going
negative must NOT kill the edge, and a required row going negative must, by
name. If judgment ever creeps into verdict(), these tests are the tripwire.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from data import backtest, events, fetch, frictions, study_stress
from tests.test_data_backtest import MINI_PARAMS, SIX_BARS


def stress_rows(**overrides):
    """A full battery result with every row healthy; override per test."""
    names = ["baseline", "slippage 2x", "slippage 3x",
             "ex bottom-liquidity tercile", "regular dividends only",
             "special dividends only", "top 5 winners removed"]
    rows = [{"stress": name, "n": 100, "median_net": 0.009,
             "median_nifty": 0.004, "median_excess": 0.005, "beat_nifty": 0.55}
            for name in names]
    for name, excess in overrides.items():
        for row in rows:
            if row["stress"] == name:
                row["median_excess"] = excess
    return rows


def validation_frame(rows):
    """[(symbol, entry_date, exit_date, net_return, yield_pct, turnover)]"""
    return pd.DataFrame({
        "entry_days_before": [20] * len(rows), "exit_days_after": [0] * len(rows),
        "in_sample": [False] * len(rows),
        "symbol": [r[0] for r in rows],
        "ex_date": pd.to_datetime([f"2024-0{i % 8 + 1}-15" for i in range(len(rows))]),
        "entry_date": pd.to_datetime([r[1] for r in rows]),
        "exit_date": pd.to_datetime([r[2] for r in rows]),
        "net_return": [r[3] for r in rows],
        "net": [r[3] * 10_000 for r in rows],
        "yield_pct": [r[4] for r in rows],
        "turnover_60d": [r[5] for r in rows],
    })


class PairingTests(unittest.TestCase):
    def test_each_trade_is_paired_over_its_own_dates(self):
        frame = validation_frame([("A", "2024-01-01", "2024-02-01", 0.02, 1.0, 5e6)])
        closes = {pd.Timestamp("2024-01-01"): 100.0,
                  pd.Timestamp("2024-02-01"): 101.0}
        paired, missing = study_stress.with_nifty(frame, closes)
        self.assertEqual(missing, 0)
        self.assertAlmostEqual(paired["nifty_return"].iloc[0], 0.01)
        self.assertAlmostEqual(paired["excess"].iloc[0], 0.01)

    def test_a_trade_the_index_cannot_date_is_dropped_and_counted(self):
        frame = validation_frame([
            ("A", "2024-01-01", "2024-02-01", 0.02, 1.0, 5e6),
            ("B", "2024-03-01", "2024-04-01", 0.02, 1.0, 5e6),
        ])
        closes = {pd.Timestamp("2024-01-01"): 100.0,
                  pd.Timestamp("2024-02-01"): 101.0}
        paired, missing = study_stress.with_nifty(frame, closes)
        self.assertEqual(len(paired), 1)
        self.assertEqual(missing, 1)


class StatsTests(unittest.TestCase):
    def test_the_beat_rate_is_the_share_of_paired_wins(self):
        frame = validation_frame([
            ("A", "2024-01-01", "2024-02-01", 0.02, 1.0, 5e6),
            ("B", "2024-01-01", "2024-02-01", 0.00, 1.0, 5e6),
        ])
        closes = {pd.Timestamp("2024-01-01"): 100.0,
                  pd.Timestamp("2024-02-01"): 101.0}
        paired, _ = study_stress.with_nifty(frame, closes)
        row = study_stress.stress_stats("baseline", paired)
        self.assertEqual(row["n"], 2)
        self.assertAlmostEqual(row["beat_nifty"], 0.5)

    def test_an_empty_slice_reports_dashes_not_zeros(self):
        row = study_stress.stress_stats("special dividends only",
                                        validation_frame([]).assign(
                                            nifty_return=[], excess=[]))
        self.assertEqual(row["n"], 0)
        self.assertIsNone(row["median_excess"])


class VerdictRuleTests(unittest.TestCase):
    def test_all_required_rows_positive_means_survives(self):
        survives, killer, _ = study_stress.verdict(stress_rows())
        self.assertTrue(survives)
        self.assertIsNone(killer)

    def test_a_negative_baseline_dies_at_baseline(self):
        survives, killer, _ = study_stress.verdict(stress_rows(baseline=-0.001))
        self.assertFalse(survives)
        self.assertEqual(killer, "baseline")

    def test_a_required_stress_going_negative_kills_by_name(self):
        survives, killer, _ = study_stress.verdict(
            stress_rows(**{"slippage 3x": -0.0004}))
        self.assertFalse(survives)
        self.assertEqual(killer, "slippage 3x")

    def test_the_diagnostic_special_row_cannot_kill(self):
        survives, _, _ = study_stress.verdict(
            stress_rows(**{"special dividends only": -0.02}))
        self.assertTrue(survives)

    def test_slippage_2x_is_reported_but_not_required(self):
        # The rule names 3x; a 2x failure alone (with 3x somehow fine) must not
        # kill — the rule is applied as written, not as remembered.
        survives, _, _ = study_stress.verdict(stress_rows(**{"slippage 2x": -0.001}))
        self.assertTrue(survives)


class BatteryTests(unittest.TestCase):
    def setUp(self):
        rows = [("A", "2024-01-01", "2024-02-01", 0.020, 1.0, 1e7),
                ("B", "2024-01-01", "2024-02-01", 0.015, 0.5, 2e7),
                ("C", "2024-01-01", "2024-02-01", 0.012, 6.0, 3e7),
                ("D", "2024-01-01", "2024-02-01", 0.010, 1.5, 5e5),
                ("E", "2024-01-01", "2024-02-01", 0.008, 0.3, 4e7),
                ("F", "2024-01-01", "2024-02-01", 0.006, 2.0, 5e7),
                ("G", "2024-01-01", "2024-02-01", -0.004, 0.8, 6e7)]
        self.baseline = validation_frame(rows)
        # NIFTY +0.5% over the window, safely away from every trade's return so
        # no excess sits at floating-point zero.
        self.closes = {pd.Timestamp("2024-01-01"): 100.0,
                       pd.Timestamp("2024-02-01"): 100.5}

    def test_the_battery_produces_every_row_from_the_docstring(self):
        fake = self.baseline.copy()
        fake["net_return"] = fake["net_return"] - 0.005
        with mock.patch.object(study_stress, "resimulate_cells", return_value=fake):
            rows, missing = study_stress.battery(
                self.baseline, (20, 0), self.closes, (1e6, 3e7))
        names = [row["stress"] for row in rows]
        self.assertEqual(names, ["baseline", "slippage 2x", "slippage 3x",
                                 "ex bottom-liquidity tercile",
                                 "regular dividends only",
                                 "special dividends only",
                                 "top 5 winners removed"])
        by_name = {row["stress"]: row for row in rows}
        self.assertEqual(by_name["baseline"]["n"], 7)
        # D (5e5 turnover) falls below the 1e6 cutoff.
        self.assertEqual(by_name["ex bottom-liquidity tercile"]["n"], 6)
        # C is the only yield >= 5 event.
        self.assertEqual(by_name["special dividends only"]["n"], 1)
        self.assertEqual(by_name["regular dividends only"]["n"], 6)
        # Seven rows minus the five largest winners.
        self.assertEqual(by_name["top 5 winners removed"]["n"], 2)

    def test_slippage_rows_use_the_resimulated_nets(self):
        fake = self.baseline.copy()
        fake["net_return"] = -0.03
        with mock.patch.object(study_stress, "resimulate_cells", return_value=fake):
            rows, _ = study_stress.battery(
                self.baseline, (20, 0), self.closes, (1e6, 3e7))
        by_name = {row["stress"]: row for row in rows}
        self.assertLess(by_name["slippage 3x"]["median_excess"], -0.03)
        # Baseline is untouched by the fake.
        self.assertGreater(by_name["baseline"]["median_excess"], 0)


class ResimulateTests(unittest.TestCase):
    """resimulate_cells against the sandbox from the backtest tests."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (fetch.CACHE_DIR, events.EVENTS_PATH, backtest.PARAMS_PATH,
                       frictions.PARAMS_PATH)
        fetch.CACHE_DIR = self.tmp / "cache"
        events.EVENTS_PATH = self.tmp / "events.parquet"
        backtest.PARAMS_PATH = self.tmp / "params.yaml"
        frictions.PARAMS_PATH = self.tmp / "params.yaml"
        backtest.PARAMS_PATH.write_text(MINI_PARAMS)
        fetch.write_cache("X", SIX_BARS)
        pd.DataFrame({"symbol": ["X"], "ex_date": pd.to_datetime(["2026-08-06"]),
                      "amount": [2.0]}).to_parquet(events.EVENTS_PATH, index=False)

    def tearDown(self):
        (fetch.CACHE_DIR, events.EVENTS_PATH, backtest.PARAMS_PATH,
         frictions.PARAMS_PATH) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_slippage_is_scaled_through_the_real_friction_model(self):
        frame = study_stress.resimulate_cells([(1, 0)], 3)
        # params slippage is 10 bps; at 3x the buy executes 0.3% above the
        # quoted 104 close.
        self.assertAlmostEqual(frame.iloc[0]["entry_close"], 104.0)
        expected = 104.0 * 1.003
        # buy_exec is not logged; deployed / quantity recovers it.
        self.assertAlmostEqual(frame.iloc[0]["deployed"] / frame.iloc[0]["quantity"],
                               expected, places=2)


class MarkdownTests(unittest.TestCase):
    def test_the_section_carries_the_verdict_and_the_two_numbers(self):
        rows = stress_rows()
        verdict_result = study_stress.verdict(rows)
        section = study_stress.results_markdown(
            (20, 0), rows, (10, 8, 12), 0, verdict_result)
        self.assertIn(study_stress.MARK_START, section)
        self.assertIn("FINAL VERDICT: the edge survives", section)
        self.assertIn("+0.90%", section)   # the strategy's number
        self.assertIn("+0.40%", section)   # NIFTY's number
        self.assertIn("10 keep positive", section)

    def test_a_dying_edge_names_its_killer(self):
        rows = stress_rows(**{"regular dividends only": -0.001})
        verdict_result = study_stress.verdict(rows)
        section = study_stress.results_markdown(
            (20, 0), rows, (10, 8, 12), 2, verdict_result)
        self.assertIn("FINAL VERDICT: the edge dies", section)
        self.assertIn("regular dividends only", section)
        self.assertIn("2 trade(s) had no NIFTY bar", section)


if __name__ == "__main__":
    unittest.main()
