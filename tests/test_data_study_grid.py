"""The split study: selection discipline, bucket honesty, and the verdict.

    python -m unittest discover -s tests -v

The selection tests are the load-bearing ones: the best cell must be chosen on
the tuning period alone, with a validation-period star deliberately planted to
verify it does not win, and the liquidity cutoffs must come from tuning-period
turnover so the buckets cannot leak validation information backwards.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from data import backtest, results, study_grid


def trades_frame(rows):
    """[(e, x, net_return, in_sample)] with the rest scaffolded consistently."""
    return pd.DataFrame({
        "entry_days_before": [r[0] for r in rows],
        "exit_days_after": [r[1] for r in rows],
        "net_return": [r[2] for r in rows],
        "in_sample": [r[3] for r in rows],
        "net": [r[2] * 10_000 for r in rows],
        "symbol": ["X"] * len(rows),
        "ex_date": pd.to_datetime(["2022-06-02" if r[3] else "2024-06-04"
                                   for r in rows]),
        "yield_pct": [1.5] * len(rows),
        "turnover_60d": [1e7] * len(rows),
    })


class SelectionTests(unittest.TestCase):
    def test_the_best_cell_is_chosen_on_the_tuning_period_alone(self):
        rows = [
            (1, 0, 0.004, True), (1, 0, 0.006, True),     # tune winner e=1
            (5, 0, 0.001, True), (5, 0, 0.002, True),
            (5, 0, 0.090, False), (5, 0, 0.080, False),   # validation star e=5
            (1, 0, -0.010, False),
        ]
        self.assertEqual(study_grid.best_cell(trades_frame(rows)), (1, 0))

    def test_nothing_surviving_frictions_in_tune_means_no_selection(self):
        rows = [(1, 0, -0.002, True), (5, 0, -0.001, True), (5, 0, 0.09, False)]
        self.assertIsNone(study_grid.best_cell(trades_frame(rows)))

    def test_survivorship_counts_tune_positive_cells_that_hold_up(self):
        rows = [
            (1, 0, 0.004, True), (1, 0, 0.003, False),    # positive, holds
            (3, 0, 0.002, True), (3, 0, -0.005, False),   # positive, fails
            (5, 0, -0.001, True), (5, 0, 0.09, False),    # never counted
        ]
        positive, held = study_grid.survivorship(trades_frame(rows))
        self.assertEqual(sorted(positive), [(1, 0), (3, 0)])
        self.assertEqual(held, [(1, 0)])


class ContextTests(unittest.TestCase):
    def test_the_naive_return_deletes_every_friction(self):
        import data.events as events_module
        trades = pd.DataFrame({
            "symbol": ["X"], "ex_date": pd.to_datetime(["2026-08-06"]),
            "entry_close": [100.0], "exit_close": [99.0], "quantity": [100],
            "dividend_gross": [200.0],
        })
        table = pd.DataFrame({
            "symbol": ["X"], "ex_date": pd.to_datetime(["2026-08-06"]),
            "yield_pct": [2.0], "avg_volume_60d": [50_000.0],
            "avg_price_60d": [100.0],
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.parquet"
            table.to_parquet(path, index=False)
            with mock.patch.object(events_module, "EVENTS_PATH", path):
                joined = study_grid.with_context(trades)
        row = joined.iloc[0]
        # (99 - 100) x 100 + 200 over 10,000: +1% with no costs anywhere.
        self.assertAlmostEqual(row["naive_return"], 0.01)
        self.assertAlmostEqual(row["turnover_60d"], 5_000_000.0)


class BucketTests(unittest.TestCase):
    def test_yield_bucket_edges(self):
        self.assertEqual(study_grid.yield_bucket(0.4), "0–0.5%")
        self.assertEqual(study_grid.yield_bucket(0.5), "0.5–1%")
        self.assertEqual(study_grid.yield_bucket(9.0), "≥2%")

    def test_liquidity_cutoffs_come_from_the_tuning_period_only(self):
        frame = trades_frame([(1, 0, 0.01, True)] * 6 + [(1, 0, 0.01, False)] * 3)
        frame.loc[frame["in_sample"], "turnover_60d"] = [1., 2., 3., 4., 5., 6.]
        # A validation whale must not drag the cutoffs.
        frame.loc[~frame["in_sample"], "turnover_60d"] = [1e12, 2e12, 3e12]
        low, high = study_grid.liquidity_cutoffs(frame)
        self.assertLess(high, 7)
        self.assertEqual(study_grid.liquidity_bucket(0.5, (low, high)), "low")
        self.assertEqual(study_grid.liquidity_bucket(1e12, (low, high)), "high")

    def test_win_rates_split_by_period_within_the_cell(self):
        frame = trades_frame([
            (1, 0, 0.01, True), (1, 0, -0.01, True),    # tune: 50%
            (1, 0, 0.02, False),                        # validate: 100%
            (9, 9, 0.5, True),                          # another cell: excluded
        ])
        rows = study_grid.bucket_win_rates(
            frame, (1, 0), lambda row: study_grid.yield_bucket(row["yield_pct"]),
            "yield")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tune_n"], 2)
        self.assertAlmostEqual(rows[0]["tune_win"], 0.5)
        self.assertEqual(rows[0]["validate_n"], 1)
        self.assertAlmostEqual(rows[0]["validate_win"], 1.0)


class VerdictTests(unittest.TestCase):
    TUNE = {"n": 100, "median_return": 0.005, "mean_return": 0.004,
            "hit_rate": 0.55, "total_net": 1.0}

    def test_a_positive_validation_verdict_carries_the_drift_caveat(self):
        validate = {"n": 50, "median_return": 0.003, "hit_rate": 0.53}
        text = study_grid.verdict_sentence((20, 0), self.TUNE, validate)
        self.assertIn("survives out-of-sample", text)
        self.assertIn("beta", text)
        self.assertIn("not yet over the index", text)

    def test_a_negative_validation_verdict_says_so_without_hedging(self):
        validate = {"n": 50, "median_return": -0.002, "hit_rate": 0.44}
        text = study_grid.verdict_sentence((1, 0), self.TUNE, validate)
        self.assertIn("no friction-adjusted edge survives", text.lower())
        self.assertIn("system working", text)


class LoadGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dir = backtest.BACKTEST_DIR
        backtest.BACKTEST_DIR = self.tmp

    def tearDown(self):
        backtest.BACKTEST_DIR = self._dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_missing_grid_is_a_clear_instruction(self):
        with self.assertRaises(RuntimeError) as caught:
            study_grid.load_trades()
        self.assertIn("data.backtest", str(caught.exception))

    def test_a_stale_fingerprint_refuses_to_analyze(self):
        (self.tmp / backtest.META_PATH_NAME).write_text(
            json.dumps({"fingerprint": "stale"}))
        with mock.patch.object(backtest, "fingerprint", return_value="fresh"):
            with self.assertRaises(RuntimeError) as caught:
                study_grid.load_trades()
        self.assertIn("different params", str(caught.exception))


class OutputTests(unittest.TestCase):
    def test_the_heatmap_figure_lands_on_disk(self):
        frame = trades_frame([(1, 0, 0.01, True), (1, 1, -0.01, True),
                              (1, 0, 0.005, False), (1, 1, -0.02, False)])
        frame["naive_return"] = frame["net_return"] + 0.004
        pivots = {(period, value): study_grid.pivot(
            frame, column, in_sample, [1], [0, 1])
            for period, in_sample in (("tune", True), ("validate", False))
            for value, column in (("naive", "naive_return"), ("net", "net_return"))}
        with tempfile.TemporaryDirectory() as tmp:
            path = study_grid.render_heatmaps(pivots, Path(tmp) / "grid.png")
            self.assertGreater(path.stat().st_size, 20_000)

    def test_the_results_section_contains_the_verdict_and_both_tables(self):
        frame = trades_frame([
            (1, 0, 0.01, True), (1, 0, 0.02, True),
            (1, 0, -0.01, False), (1, 0, 0.03, False),
        ])
        frame["naive_return"] = frame["net_return"] + 0.004
        section = study_grid.results_markdown(frame, (1, 0), "2022-12-31")
        self.assertIn(study_grid.MARK_START, section)
        self.assertIn("| yield |", section)
        self.assertIn("| liquidity |", section)
        self.assertIn("Verdict", section)


if __name__ == "__main__":
    unittest.main()
