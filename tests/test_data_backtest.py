"""The grid backtester: offset semantics, frictions wiring, resume honesty.

    python -m unittest discover -s tests -v

The test that matters most is the frictions cross-check: the same trade that
tests/test_data_frictions.py hand-computed to -1,118.00 is rebuilt here as
cached bars and driven through simulate_cell, asserting the backtester reaches
the identical net. If the two modules ever disagree about what a trade costs,
one shared number breaks instead of two reports quietly diverging.
"""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

from data import backtest, events, fetch, frictions
from data.frictions import Config


NOCOST = Config(
    brokerage_buy=0.0, brokerage_sell=0.0, stt_rate=0.0, stamp_rate=0.0,
    exchange_rate=0.0, sebi_rate=0.0, gst_rate=0.0, dp_per_sell=0.0,
    slippage_bps=0.0, dividend_slab_rate=0.0, stcg_rate=0.0,
    apply_section_94_7=True,
)

# The hand-computed config from tests/test_data_frictions.py, verbatim.
HAND = Config(
    brokerage_buy=20.0, brokerage_sell=20.0, stt_rate=0.001, stamp_rate=0.00015,
    exchange_rate=0.0000297, sebi_rate=0.000001, gst_rate=0.18, dp_per_sell=15.0,
    slippage_bps=0.0, dividend_slab_rate=0.312, stcg_rate=0.20,
    apply_section_94_7=True,
)


def bars(pairs):
    """A cache frame from [(date, close)]; the other columns are scaffolding."""
    return pd.DataFrame({
        "date": pd.to_datetime([p[0] for p in pairs]),
        "open": [p[1] for p in pairs], "high": [p[1] for p in pairs],
        "low": [p[1] for p in pairs], "close": [p[1] for p in pairs],
        "adj_close": [p[1] for p in pairs], "volume": [1000] * len(pairs),
        "dividend": [0.0] * len(pairs), "split": [0.0] * len(pairs),
    })


def series_for(frames):
    """The sessions_by_symbol structure, built directly from frames."""
    out = {}
    for symbol, frame in frames.items():
        dates = list(frame["date"])
        out[symbol] = (dates, list(frame["close"]),
                       {stamp: index for index, stamp in enumerate(dates)})
    return out


def located(symbol, ex_date, amount, series):
    return [(symbol, pd.Timestamp(ex_date), amount,
             series[symbol][2][pd.Timestamp(ex_date)])]


SIX_BARS = bars([("2026-08-03", 100.0), ("2026-08-04", 102.0),
                 ("2026-08-05", 104.0), ("2026-08-06", 101.0),
                 ("2026-08-07", 103.0), ("2026-08-10", 105.0)])


class OffsetTests(unittest.TestCase):
    def setUp(self):
        self.series = series_for({"X": SIX_BARS})
        self.event = located("X", "2026-08-06", 2.0, self.series)

    def _cell(self, entry, exit_after):
        frame, skips = backtest.simulate_cell(
            entry, exit_after, self.event, self.series, NOCOST,
            notional=10_000, train_until=date(2030, 1, 1))
        return frame, skips

    def test_offsets_walk_the_session_series(self):
        frame, _ = self._cell(2, 1)
        row = frame.iloc[0]
        self.assertEqual(row["entry_date"], pd.Timestamp("2026-08-04"))
        self.assertEqual(row["entry_close"], 102.0)
        self.assertEqual(row["exit_date"], pd.Timestamp("2026-08-07"))
        self.assertEqual(row["exit_close"], 103.0)

    def test_a_weekend_is_not_a_session(self):
        # Two sessions after the 08-06 ex-date is Monday the 10th, not Saturday.
        frame, _ = self._cell(1, 2)
        self.assertEqual(frame.iloc[0]["exit_date"], pd.Timestamp("2026-08-10"))

    def test_entry_before_ex_receives_the_dividend(self):
        frame, _ = self._cell(1, 0)
        row = frame.iloc[0]
        # 96 shares at 104 (10,000 // 104): dividend 2 x 96, and the nocost net
        # is the price move plus the payout: (101 - 104) x 96 + 192 = -96.
        self.assertEqual(row["quantity"], 96)
        self.assertEqual(row["dividend_gross"], 192.0)
        self.assertAlmostEqual(row["net"], (101.0 - 104.0) * 96 + 192.0)

    def test_entry_zero_buys_the_ex_bar_without_the_dividend(self):
        frame, _ = self._cell(0, 1)
        row = frame.iloc[0]
        self.assertEqual(row["entry_date"], pd.Timestamp("2026-08-06"))
        self.assertEqual(row["dividend_gross"], 0.0)
        self.assertFalse(row["section_94_7_applied"])

    def test_insufficient_history_is_skipped_and_counted(self):
        frame, skips = self._cell(20, 0)
        self.assertTrue(frame.empty)
        self.assertEqual(skips["insufficient history"], 1)

    def test_a_price_above_the_notional_is_skipped_and_counted(self):
        rich = series_for({"X": bars([("2026-08-05", 15_000.0),
                                      ("2026-08-06", 15_000.0)])})
        event = located("X", "2026-08-06", 2.0, rich)
        frame, skips = backtest.simulate_cell(
            1, 0, event, rich, NOCOST, notional=10_000,
            train_until=date(2030, 1, 1))
        self.assertTrue(frame.empty)
        self.assertEqual(skips["price above notional"], 1)


class FrictionWiringTests(unittest.TestCase):
    def test_the_hand_computed_trade_survives_the_backtester(self):
        # Trade three from tests/test_data_frictions.py: buy 100 x 300 one
        # session before the 2025-08-06 ex-date, sell at 280 after, dividend 12,
        # 94(7) bites: net -1,118.00. Bars are laid out so e=1, x=1 lands on
        # exactly those prices and dates (sell 2025-08-20, inside the window).
        frames = {"Y": bars([("2025-08-05", 300.0), ("2025-08-06", 290.0),
                             ("2025-08-20", 280.0)])}
        series = series_for(frames)
        event = located("Y", "2025-08-06", 12.0, series)
        frame, _ = backtest.simulate_cell(
            1, 1, event, series, HAND, notional=30_000,
            train_until=date(2030, 1, 1))
        row = frame.iloc[0]
        self.assertEqual(row["quantity"], 100)
        self.assertTrue(row["section_94_7_applied"])
        self.assertEqual(row["disallowed_loss"], 1200.0)
        self.assertAlmostEqual(row["net"], -1118.00)
        # And it is literally the friction model's number, not a reimplementation.
        direct = frictions.trade(
            HAND, quantity=100, buy_price=300.0, sell_price=280.0,
            buy_date=date(2025, 8, 5), sell_date=date(2025, 8, 20),
            dividend_per_share=12.0, record_date=date(2025, 8, 6))
        self.assertEqual(row["net"], direct["net"])

    def test_net_return_is_net_over_deployed(self):
        series = series_for({"X": SIX_BARS})
        event = located("X", "2026-08-06", 2.0, series)
        frame, _ = backtest.simulate_cell(
            1, 0, event, series, NOCOST, notional=10_000,
            train_until=date(2030, 1, 1))
        row = frame.iloc[0]
        self.assertAlmostEqual(row["net_return"], row["net"] / row["deployed"],
                               places=6)


class SampleSplitTests(unittest.TestCase):
    def test_train_until_labels_each_trade(self):
        frames = {"X": bars([("2022-06-01", 100.0), ("2022-06-02", 100.0),
                             ("2023-06-01", 100.0), ("2023-06-02", 100.0)])}
        series = series_for(frames)
        both = (located("X", "2022-06-02", 1.0, series)
                + located("X", "2023-06-02", 1.0, series))
        frame, _ = backtest.simulate_cell(
            1, 0, both, series, NOCOST, notional=10_000,
            train_until=date(2022, 12, 31))
        flags = dict(zip(frame["ex_date"].dt.year, frame["in_sample"]))
        self.assertTrue(flags[2022])
        self.assertFalse(flags[2023])


class DeterminismTests(unittest.TestCase):
    def test_the_same_inputs_produce_identical_frames(self):
        series = series_for({"X": SIX_BARS})
        event = located("X", "2026-08-06", 2.0, series)
        first, _ = backtest.simulate_cell(3, 1, event, series, HAND,
                                          notional=10_000, train_until=date(2030, 1, 1))
        second, _ = backtest.simulate_cell(3, 1, event, series, HAND,
                                           notional=10_000, train_until=date(2030, 1, 1))
        pd.testing.assert_frame_equal(first, second)


class AggregateTests(unittest.TestCase):
    def test_per_cell_stats_split_by_sample(self):
        trades = pd.DataFrame({
            "entry_days_before": [1, 1, 1], "exit_days_after": [0, 0, 0],
            "net": [100.0, -50.0, 999.0], "net_return": [0.01, -0.005, 0.09],
            "disallowed_loss": [0.0, 30.0, 0.0],
            "in_sample": [True, True, False],
        })
        summary = backtest.aggregate(trades)
        inside = summary[summary["in_sample"]].iloc[0]
        self.assertEqual(inside["trades"], 2)
        self.assertAlmostEqual(inside["hit_rate"], 0.5)
        self.assertAlmostEqual(inside["total_net"], 50.0)
        self.assertEqual(inside["bitten_by_94_7"], 1)
        outside = summary[~summary["in_sample"]].iloc[0]
        self.assertEqual(outside["trades"], 1)

    def test_an_empty_run_aggregates_to_an_empty_table(self):
        self.assertTrue(backtest.aggregate(pd.DataFrame()).empty)


MINI_PARAMS = (
    "frictions:\n"
    "  brokerage: {delivery_buy_inr: 0, delivery_sell_inr: 0}\n"
    "  stt_delivery_pct: 0.1\n"
    "  stamp_duty_buy_pct: 0.015\n"
    "  exchange_txn_pct: 0.00297\n"
    "  sebi_fee_pct: 0.0001\n"
    "  gst_pct: 18\n"
    "  dp_charge_per_sell_inr: 13\n"
    "  slippage_bps: 10\n"
    "tax:\n"
    "  dividend_slab_pct: 31.2\n"
    "  stcg_pct: 20\n"
    "  tds_threshold_inr: 10000\n"
    "  apply_section_94_7: true\n"
    "backtest:\n"
    "  entry_days_before: [1]\n"
    "  exit_days_after: [0]\n"
    "  notional_per_trade_inr: 10000\n"
    "  train_until: 2030-01-01\n")


class RunTests(unittest.TestCase):
    """run() end to end in a sandbox: fingerprinted resume and invalidation."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (fetch.CACHE_DIR, events.EVENTS_PATH, backtest.BACKTEST_DIR,
                       backtest.PARAMS_PATH, frictions.PARAMS_PATH)
        fetch.CACHE_DIR = self.tmp / "cache"
        events.EVENTS_PATH = self.tmp / "events.parquet"
        backtest.BACKTEST_DIR = self.tmp / "grid"
        backtest.PARAMS_PATH = self.tmp / "params.yaml"
        frictions.PARAMS_PATH = self.tmp / "params.yaml"

        backtest.PARAMS_PATH.write_text(MINI_PARAMS)
        fetch.write_cache("X", SIX_BARS)
        pd.DataFrame({"symbol": ["X"], "ex_date": pd.to_datetime(["2026-08-06"]),
                      "amount": [2.0]}).to_parquet(events.EVENTS_PATH, index=False)

    def tearDown(self):
        (fetch.CACHE_DIR, events.EVENTS_PATH, backtest.BACKTEST_DIR,
         backtest.PARAMS_PATH, frictions.PARAMS_PATH) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_run_writes_the_cell_log_and_summary(self):
        self.assertEqual(backtest.run(), 0)
        self.assertTrue(backtest.cell_path(1, 0).exists())
        summary = pd.read_parquet(backtest.BACKTEST_DIR / backtest.SUMMARY_NAME)
        self.assertEqual(int(summary.iloc[0]["trades"]), 1)

    def test_an_unchanged_rerun_reuses_cells_without_simulating(self):
        backtest.run()
        with mock.patch.object(backtest, "simulate_cell",
                               side_effect=AssertionError("resume did not resume")):
            self.assertEqual(backtest.run(), 0)

    def test_changed_inputs_invalidate_every_stored_cell(self):
        backtest.run()
        backtest.PARAMS_PATH.write_text(MINI_PARAMS + "# a comment is a change\n")
        calls = []
        original = backtest.simulate_cell

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        with mock.patch.object(backtest, "simulate_cell", side_effect=counting):
            self.assertEqual(backtest.run(), 0)
        self.assertEqual(len(calls), 1)

    def test_force_recomputes_even_when_nothing_changed(self):
        backtest.run()
        calls = []
        original = backtest.simulate_cell

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        with mock.patch.object(backtest, "simulate_cell", side_effect=counting):
            self.assertEqual(backtest.run(force=True), 0)
        self.assertEqual(len(calls), 1)

    def test_missing_events_parquet_is_a_loud_failure(self):
        events.EVENTS_PATH.unlink()
        self.assertEqual(backtest.run(), 1)


if __name__ == "__main__":
    unittest.main()
