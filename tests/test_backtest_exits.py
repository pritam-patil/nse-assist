"""Exit resolution: the arithmetic that decides whether a backtest tells the truth.

    python -m unittest discover -s tests -v

Every rule here is one a backtest gets wrong by default, and every one of them
errs in the same direction — flattering the strategy:

  a gap through a stop that fills AT the stop invents a price nobody could get
  a day containing both levels that counts as a target picks the good outcome
  levels re-anchored to the fill run trades the live journal would not

None of these crash. They just quietly add money that was never there, which is
why they are asserted rather than assumed.

Bars are built by hand: these are properties of the exit logic, and a fixture that
needed 260 sessions of history to test a gap would be testing something else.
"""

import unittest

from src import backtest, costs


def bar(day, open_, high, low, close, volume=100_000):
    return {"date": day, "open": open_, "high": high, "low": low,
            "close": close, "volume": volume}


def plan(entry=100.0, stop=95.0, target=110.0, size=10):
    """A signal as signals.levels() produces it: levels from the signal-day close."""
    return {"symbol": "TEST", "rule": "unit", "entry": entry,
            "stop": stop, "target": target, "size": size}


SIGNAL_BAR = bar("2026-01-01", 100.0, 101.0, 99.0, 100.0)


class StopExitTestCase(unittest.TestCase):
    def test_ordinary_stop_fills_at_the_stop(self):
        bars = [SIGNAL_BAR, bar("2026-01-02", 100.0, 101.0, 94.0, 96.0)]
        trade, _ = backtest.simulate_position(bars, 0, plan())
        self.assertEqual(trade["exit_reason"], backtest.EXIT_STOP)
        self.assertEqual(trade["exit_price"], 95.0)

    def test_gap_through_the_stop_fills_at_the_open_not_the_stop(self):
        """The one that matters most. Opening at 90 against a stop at 95 means the
        fill is 90 — the stop order becomes a market order at the open. Booking 95
        would credit five rupees a share that nobody could have got."""
        bars = [SIGNAL_BAR, bar("2026-01-02", 90.0, 92.0, 88.0, 91.0)]
        trade, reason = backtest.simulate_position(bars, 0, plan())
        # Opening below the stop means the position is never entered at all.
        self.assertIsNone(trade)
        self.assertIn("gapped through the stop", reason)

    def test_gap_below_stop_on_a_later_day_fills_at_that_open(self):
        bars = [
            SIGNAL_BAR,
            bar("2026-01-02", 100.0, 102.0, 99.0, 101.0),   # entered here, no exit
            bar("2026-01-05", 90.0, 91.0, 88.0, 89.0),      # gaps under the stop
        ]
        trade, _ = backtest.simulate_position(bars, 0, plan())
        self.assertEqual(trade["exit_reason"], backtest.EXIT_STOP)
        self.assertEqual(trade["exit_price"], 90.0, "must fill at the open, not the stop")
        self.assertLess(trade["exit_price"], trade["stop"])


class TargetExitTestCase(unittest.TestCase):
    def test_ordinary_target_fills_at_the_target(self):
        bars = [SIGNAL_BAR, bar("2026-01-02", 100.0, 111.0, 99.0, 108.0)]
        trade, _ = backtest.simulate_position(bars, 0, plan())
        self.assertEqual(trade["exit_reason"], backtest.EXIT_TARGET)
        self.assertEqual(trade["exit_price"], 110.0)

    def test_gap_above_target_fills_at_the_open(self):
        """Symmetric to the stop rule, and here it happens to favour you — a limit
        sell resting at 110 fills at 115 when the market opens there."""
        bars = [
            SIGNAL_BAR,
            bar("2026-01-02", 100.0, 102.0, 99.0, 101.0),
            bar("2026-01-05", 115.0, 118.0, 114.0, 117.0),
        ]
        trade, _ = backtest.simulate_position(bars, 0, plan())
        self.assertEqual(trade["exit_reason"], backtest.EXIT_TARGET)
        self.assertEqual(trade["exit_price"], 115.0)
        self.assertGreater(trade["exit_price"], trade["target"])


class AmbiguousDayTestCase(unittest.TestCase):
    def test_a_day_containing_both_levels_is_a_stop(self):
        """Daily bars cannot say which came first. Choosing the target would be
        choosing the pleasant answer, every time, for years."""
        bars = [SIGNAL_BAR, bar("2026-01-02", 100.0, 112.0, 94.0, 105.0)]
        trade, _ = backtest.simulate_position(bars, 0, plan())
        self.assertEqual(trade["exit_reason"], backtest.EXIT_STOP)
        self.assertEqual(trade["exit_price"], 95.0)
        self.assertLess(trade["pnl"], 0)


class TimeExitTestCase(unittest.TestCase):
    def test_time_stop_exits_at_the_close_of_the_last_held_bar(self):
        bars = [SIGNAL_BAR] + [
            bar(f"2026-02-{d:02d}", 100.0, 101.0, 99.0, 100.5) for d in range(1, 16)
        ]
        trade, _ = backtest.simulate_position(bars, 0, plan(), max_hold=10)
        self.assertEqual(trade["exit_reason"], backtest.EXIT_TIME)
        self.assertEqual(trade["held_bars"], 10)
        self.assertEqual(trade["exit_date"], "2026-02-10")
        self.assertEqual(trade["exit_price"], 100.5)

    def test_a_position_is_never_held_past_the_limit(self):
        bars = [SIGNAL_BAR] + [
            bar(f"2026-02-{d:02d}", 100.0, 101.0, 99.0, 100.5) for d in range(1, 28)
        ]
        trade, _ = backtest.simulate_position(bars, 0, plan(), max_hold=10)
        self.assertLessEqual(trade["held_bars"], 10)


class EntryTestCase(unittest.TestCase):
    def test_entry_is_the_next_open_not_the_signal_close(self):
        bars = [SIGNAL_BAR, bar("2026-01-02", 103.0, 112.0, 102.0, 111.0)]
        trade, _ = backtest.simulate_position(bars, 0, plan())
        self.assertEqual(trade["entry_price"], 103.0)
        self.assertEqual(trade["entry_estimate"], 100.0)

    def test_levels_stay_anchored_to_the_estimate(self):
        """The point of the whole design: stop and target must NOT move to the fill.

        journal.py fills live signals from levels fixed at signal time, because the
        next open does not exist when the signal is written. Re-anchoring here would
        make the backtest run trades the journal never would.
        """
        bars = [SIGNAL_BAR, bar("2026-01-02", 105.0, 106.0, 104.0, 105.0)]
        trade, _ = backtest.simulate_position(bars, 0, plan(entry=100.0, stop=95.0, target=110.0))
        self.assertEqual(trade["stop"], 95.0)
        self.assertEqual(trade["target"], 110.0)

    def test_the_close_to_open_gap_is_recorded_as_slippage(self):
        bars = [SIGNAL_BAR, bar("2026-01-02", 102.0, 103.0, 101.0, 102.0)]
        trade, _ = backtest.simulate_position(bars, 0, plan(size=10))
        self.assertAlmostEqual(trade["entry_slippage"], 20.0, places=2)

    def test_no_session_after_the_signal(self):
        trade, reason = backtest.simulate_position([SIGNAL_BAR], 0, plan())
        self.assertIsNone(trade)
        self.assertIn("no session", reason)

    def test_gap_past_the_target_before_entry_is_not_a_free_win(self):
        bars = [SIGNAL_BAR, bar("2026-01-02", 115.0, 116.0, 114.0, 115.0)]
        trade, reason = backtest.simulate_position(bars, 0, plan())
        self.assertIsNone(trade)
        self.assertIn("gapped past the target", reason)


class CostTestCase(unittest.TestCase):
    def test_costs_are_charged_on_losers_too(self):
        bars = [SIGNAL_BAR, bar("2026-01-02", 100.0, 101.0, 94.0, 96.0)]
        trade, _ = backtest.simulate_position(bars, 0, plan())
        self.assertGreater(trade["costs"], 0)
        self.assertLess(trade["pnl"], trade["gross_pnl"])

    def test_slippage_is_part_of_the_charge(self):
        """A backtest without slippage assumes every order fills at the printed
        price, which is the single most flattering assumption available."""
        with_slip = costs.round_trip(100.0, 105.0, 10, include_slippage=True)["total"]
        without = costs.round_trip(100.0, 105.0, 10, include_slippage=False)["total"]
        self.assertGreater(with_slip, without)

    def test_net_is_gross_minus_costs(self):
        bars = [SIGNAL_BAR, bar("2026-01-02", 100.0, 111.0, 99.0, 108.0)]
        trade, _ = backtest.simulate_position(bars, 0, plan())
        self.assertAlmostEqual(trade["pnl"], trade["gross_pnl"] - trade["costs"], places=2)


class MetricsTestCase(unittest.TestCase):
    def _trade(self, pnl, exit_date):
        return {"pnl": pnl, "gross_pnl": pnl, "costs": 0.0, "held_bars": 3,
                "exit_date": exit_date}

    def test_max_drawdown_is_peak_to_trough(self):
        trades = [self._trade(p, f"2026-01-{i + 1:02d}")
                  for i, p in enumerate([100, -40, -30, 20])]
        self.assertEqual(backtest.max_drawdown(trades), -70.0)

    def test_drawdown_of_a_rising_curve_is_zero(self):
        trades = [self._trade(p, f"2026-01-{i + 1:02d}") for i, p in enumerate([10, 20, 30])]
        self.assertEqual(backtest.max_drawdown(trades), 0.0)

    def test_profit_factor_and_hit_rate(self):
        trades = [self._trade(p, f"2026-01-{i + 1:02d}")
                  for i, p in enumerate([100, 100, -50, -50])]
        stats = backtest.summarize(trades)
        self.assertEqual(stats["hit_rate"], 0.5)
        self.assertEqual(stats["profit_factor"], 2.0)
        self.assertEqual(stats["expectancy"], 25.0)
        self.assertEqual(stats["avg_win"], 100.0)
        self.assertEqual(stats["avg_loss"], -50.0)

    def test_empty_summary_has_every_key(self):
        self.assertEqual(set(backtest.summarize([])),
                         set(backtest.summarize([self._trade(1.0, "2026-01-01")])))


if __name__ == "__main__":
    unittest.main()
