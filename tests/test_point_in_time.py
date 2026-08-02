"""The point-in-time guarantee: features as of D must not depend on data after D.

    python -m unittest discover -s tests -v

This is the most important test in the project. Lookahead bias is the failure mode
that does not announce itself — a backtest that peeks one day forward still runs,
still produces plausible numbers, and still reports an edge. It just reports one
that will not survive contact with a live market. Every other bug here costs you a
correct answer; this one costs you a wrong answer you believe.

The shape of every test below is the same:

    1. compute features as of D with future rows PRESENT
    2. change the future — delete it, or corrupt it beyond recognition
    3. compute again as of D
    4. assert the two are identical

THE CACHE IS THE TRAP. features.py memoises frames by as-of date. If step 3 is
served from the cache populated in step 1, the assertion compares a value with
itself and passes no matter how badly the point-in-time filter is broken — a test
that cannot fail. Every case here calls features.clear_cache() between the two
computations, and test_cache_cannot_mask_a_leak proves the clearing works by
showing the cache does return stale data when it is not cleared.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from src import features
from src.db import get_connection, init_db

SYMBOL = "TESTCO"
BARS = 420          # comfortably over MIN_BARS so features are fully warmed
SPLIT_INDEX = 300   # the as-of point; everything after it is "the future"


def synthetic_bars(count=BARS, seed=7.0):
    """A deterministic price series with enough shape to move every indicator.

    Not random: a fixed series means a failure is reproducible, and these tests
    compare runs against each other rather than against expected constants.
    """
    bars, price, day = [], 100.0, date(2023, 1, 2)
    for i in range(count):
        # Two interfering cycles plus drift, so SMAs, RSI and volatility all vary
        # instead of settling into a constant.
        wobble = (i % 11 - 5) * 0.6 + (i % 29 - 14) * 0.25
        price = max(5.0, price + wobble * 0.4 + 0.05)
        high = price * 1.012
        low = price * 0.988
        opening = low + (high - low) * ((i * seed) % 1.0)
        bars.append({
            "date": day.isoformat(),
            "open": round(opening, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(price, 2),
            "volume": 100_000 + (i % 17) * 7_000,
        })
        day += timedelta(days=1)
        while day.weekday() >= 5:      # keep it to weekdays; the module never
            day += timedelta(days=1)   # assumes a calendar, but realism is free
    return bars


class PointInTimeTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self.bars = synthetic_bars()
        self.as_of = self.bars[SPLIT_INDEX]["date"]
        self._insert(self.bars)
        features.clear_cache()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)
        features.clear_cache()

    # --- helpers ---

    def _insert(self, bars, symbol=SYMBOL):
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, volume, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'test')",
            [(symbol, b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"])
             for b in bars],
        )
        self.conn.commit()

    def _delete_after(self, day):
        self.conn.execute("DELETE FROM prices WHERE date > ?", (day,))
        self.conn.commit()

    def _corrupt_after(self, day, factor=10.0):
        """Make the future unmistakably different without removing it. If any
        forward-looking read exists, these values will drag the result somewhere
        obvious rather than subtly."""
        self.conn.execute(
            "UPDATE prices SET open = open * ?, high = high * ?, low = low * ?, "
            "close = close * ?, volume = volume * 100 WHERE date > ?",
            (factor, factor, factor, factor, day),
        )
        self.conn.commit()

    def _features_now(self):
        """Compute as of the fixed date, with the cache cleared first.

        The clear is the whole reason this helper exists — see the module docstring.
        """
        features.clear_cache()
        return features.compute_for(self.conn, SYMBOL, as_of=self.as_of)

    # --- the core property ---

    def test_deleting_the_future_changes_nothing(self):
        before = self._features_now()
        self.assertIsNotNone(before, "fixture must produce features")
        self._delete_after(self.as_of)
        after = self._features_now()
        self.assertEqual(before, after)

    def test_corrupting_the_future_changes_nothing(self):
        """The stronger version: the future still exists, but is 10x wrong.

        Deletion alone is a weaker test — a buggy implementation that reads the last
        row of the table would pass it, because after deletion the last row IS the
        as-of row. Corruption catches that; the values stay in place and stay wrong.
        """
        before = self._features_now()
        self._corrupt_after(self.as_of)
        after = self._features_now()
        self.assertEqual(before, after)

    def test_every_feature_individually(self):
        """Reported per key, so a failure names the broken indicator rather than
        just saying two dicts differ."""
        before = self._features_now()
        self._corrupt_after(self.as_of)
        after = self._features_now()
        for key in sorted(before):
            with self.subTest(feature=key):
                self.assertEqual(before[key], after[key], f"{key} leaked future data")

    def test_frame_is_point_in_time(self):
        import pandas as pd

        features.clear_cache()
        before = features.feature_frame(self.conn, as_of=self.as_of, symbols=[SYMBOL])
        self._corrupt_after(self.as_of)
        features.clear_cache()
        after = features.feature_frame(self.conn, as_of=self.as_of, symbols=[SYMBOL])
        pd.testing.assert_frame_equal(before, after)

    def test_as_of_row_is_the_last_row_used(self):
        computed = self._features_now()
        self.assertEqual(computed["date"], self.as_of)
        self.assertEqual(computed["close"], self.bars[SPLIT_INDEX]["close"])
        self.assertEqual(computed["bars"], SPLIT_INDEX + 1)

    def test_load_bars_respects_as_of(self):
        bars = features.load_bars(self.conn, SYMBOL, as_of=self.as_of)
        self.assertEqual(len(bars), SPLIT_INDEX + 1)
        self.assertLessEqual(max(b["date"] for b in bars), self.as_of)

    def test_as_of_none_sees_everything(self):
        """The escape hatch works as documented — and is therefore the thing a
        backtest must never use."""
        bars = features.load_bars(self.conn, SYMBOL, as_of=None)
        self.assertEqual(len(bars), BARS)

    # --- the trap itself ---

    def test_cache_cannot_mask_a_leak(self):
        """Proves the other tests are not passing vacuously.

        Without clear_cache(), the second call is served from memory and returns the
        old value even though the underlying data changed. If that were happening in
        the tests above, they would pass against a completely broken implementation.
        This asserts the cache really does go stale, which is what makes the explicit
        clearing meaningful rather than ceremonial.
        """
        features.clear_cache()
        first = features.feature_frame(self.conn, as_of=self.as_of, symbols=[SYMBOL])

        # Change data that IS within the as-of window, so a correct recomputation
        # must differ.
        self.conn.execute(
            "UPDATE prices SET close = close * 2 WHERE symbol = ? AND date <= ?",
            (SYMBOL, self.as_of),
        )
        self.conn.commit()

        cached = features.feature_frame(self.conn, as_of=self.as_of, symbols=[SYMBOL])
        self.assertEqual(
            float(first["close"].iloc[0]), float(cached["close"].iloc[0]),
            "cache should have returned the stale frame — if this fails the cache is "
            "not caching and the point-in-time tests prove less than they appear to",
        )

        features.clear_cache()
        fresh = features.feature_frame(self.conn, as_of=self.as_of, symbols=[SYMBOL])
        self.assertNotEqual(
            float(first["close"].iloc[0]), float(fresh["close"].iloc[0]),
            "after clear_cache() the recomputed frame must reflect the new data",
        )

    def test_cache_is_keyed_by_as_of(self):
        """Two different as-of dates must not share an entry."""
        earlier = self.bars[SPLIT_INDEX - 30]["date"]
        features.clear_cache()
        a = features.feature_frame(self.conn, as_of=earlier, symbols=[SYMBOL])
        b = features.feature_frame(self.conn, as_of=self.as_of, symbols=[SYMBOL])
        self.assertEqual(a["as_of"].iloc[0], earlier)
        self.assertEqual(b["as_of"].iloc[0], self.as_of)
        self.assertNotEqual(float(a["close"].iloc[0]), float(b["close"].iloc[0]))

    def test_cached_frame_is_not_the_shared_object(self):
        """A caller mutating its frame must not corrupt the next caller's."""
        features.clear_cache()
        first = features.feature_frame(self.conn, as_of=self.as_of, symbols=[SYMBOL])
        first.loc[0, "close"] = -999.0
        second = features.feature_frame(self.conn, as_of=self.as_of, symbols=[SYMBOL])
        self.assertNotEqual(float(second["close"].iloc[0]), -999.0)

    # --- the two filters must stay one rule ---

    def test_in_memory_filter_matches_the_sql_filter(self):
        """bars_as_of() and load_bars(as_of=) must select identically.

        There are two implementations of "up to this date": a WHERE clause for the
        database and a list comprehension for the backtest, which walks thousands of
        dates per symbol and cannot afford a query each. Two implementations of one
        rule is how a guarantee rots — someone fixes the SQL and not the other. This
        pins them together across the whole range, including dates with no bar.
        """
        every = features.load_bars(self.conn, SYMBOL, as_of=None)
        probes = [self.bars[i]["date"] for i in (0, 1, 50, SPLIT_INDEX, BARS - 1)]
        probes.append((date.fromisoformat(self.as_of) + timedelta(days=1)).isoformat())
        probes.append("2019-01-01")

        for probe in probes:
            with self.subTest(as_of=probe):
                self.assertEqual(features.bars_as_of(every, probe),
                                 features.load_bars(self.conn, SYMBOL, as_of=probe))

    def test_compute_as_of_matches_compute_for(self):
        """The two entry points must produce the same features for the same date."""
        every = features.load_bars(self.conn, SYMBOL, as_of=None)
        for offset in (0, 7, 40):
            probe = self.bars[SPLIT_INDEX - offset]["date"]
            with self.subTest(as_of=probe):
                features.clear_cache()
                self.assertEqual(features.compute_as_of(every, probe),
                                 features.compute_for(self.conn, SYMBOL, as_of=probe))

    def test_backtest_scan_ignores_bars_after_as_of(self):
        """The stage-level property: the signals a backtest finds up to D must not
        be moved by anything dated later, however extreme.

        scan_history() is where the backtest reads features, so this is the point
        the guarantee has to hold — the exit simulation downstream only ever reads
        bars it is handed.
        """
        from src import backtest

        every = features.load_bars(self.conn, SYMBOL, as_of=None)
        features.clear_cache()
        before = backtest.scan_history({SYMBOL: features.bars_as_of(every, self.as_of)})

        self._corrupt_after(self.as_of)
        corrupted = features.load_bars(self.conn, SYMBOL, as_of=None)
        features.clear_cache()
        after = backtest.scan_history({SYMBOL: features.bars_as_of(corrupted, self.as_of)})

        self.assertEqual(before, after)

    # --- boundaries ---

    def test_as_of_before_any_history_returns_none(self):
        self.assertIsNone(features.compute_for(self.conn, SYMBOL, as_of="2020-01-01"))

    def test_insufficient_history_returns_none(self):
        """One bar short of MIN_BARS is None, not a half-warmed answer."""
        short_day = self.bars[features.MIN_BARS - 2]["date"]
        self.assertIsNone(features.compute_for(self.conn, SYMBOL, as_of=short_day))
        exact_day = self.bars[features.MIN_BARS - 1]["date"]
        self.assertIsNotNone(features.compute_for(self.conn, SYMBOL, as_of=exact_day))

    def test_as_of_on_a_non_trading_day_uses_the_prior_bar(self):
        """An as-of date with no bar of its own resolves backwards, never forwards."""
        gap_day = (date.fromisoformat(self.as_of) + timedelta(days=1)).isoformat()
        self.conn.execute("DELETE FROM prices WHERE date = ?", (gap_day,))
        self.conn.commit()
        computed = features.compute_for(self.conn, SYMBOL, as_of=gap_day)
        self.assertEqual(computed["date"], self.as_of)

    def test_accepts_date_objects_and_strings(self):
        as_date = features.compute_for(self.conn, SYMBOL, as_of=date.fromisoformat(self.as_of))
        as_text = features.compute_for(self.conn, SYMBOL, as_of=self.as_of)
        self.assertEqual(as_date, as_text)


class FeatureCorrectnessTestCase(unittest.TestCase):
    """The point-in-time tests compare runs against each other, so they would pass
    even if every indicator returned a constant. These pin the arithmetic down."""

    def setUp(self):
        self.bars = synthetic_bars()
        self.closes = [b["close"] for b in self.bars]

    def test_sma_is_the_mean_of_the_window(self):
        self.assertAlmostEqual(
            features.sma(self.closes, 20), sum(self.closes[-20:]) / 20, places=9
        )

    def test_sma_needs_a_full_window(self):
        self.assertIsNone(features.sma([1.0, 2.0], 20))

    def test_ema_reacts_faster_than_sma(self):
        """EMA weights recent bars more, so it moves further on a step change.

        Tested with a step, not a ramp: against a *constant* trend EMA(n) and SMA(n)
        lag by exactly the same (n-1)/2 periods and come out identical, so a rising
        line proves nothing. Responsiveness only shows up when the trend changes.
        """
        step_up = [100.0] * 30 + [110.0] * 3
        self.assertGreater(features.ema(step_up, 9), features.sma(step_up, 9))

        step_down = [100.0] * 30 + [90.0] * 3
        self.assertLess(features.ema(step_down, 9), features.sma(step_down, 9))

        ramp = [float(i) for i in range(1, 60)]
        self.assertAlmostEqual(features.ema(ramp, 9), features.sma(ramp, 9), places=6)

    def test_rsi_bounds(self):
        self.assertEqual(features.rsi([float(i) for i in range(1, 40)]), 100.0)
        falling = [float(i) for i in range(40, 1, -1)]
        self.assertLess(features.rsi(falling), 1.0)

    def test_atr_spans_the_overnight_gap(self):
        """True range must include the gap from the prior close, not just high-low."""
        bars = [
            {"date": "2026-01-01", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
            {"date": "2026-01-02", "open": 120, "high": 121, "low": 119, "close": 120, "volume": 1},
        ] * 10
        value = features.atr(bars, period=2)
        self.assertGreater(value, 2.0, "ATR ignored the gap and only saw high-low")

    def test_realized_vol_is_annualised(self):
        daily = features.realized_vol(self.closes, annualize=False)
        annual = features.realized_vol(self.closes, annualize=True)
        self.assertAlmostEqual(annual / daily, features.SESSIONS_PER_YEAR**0.5, places=9)

    def test_52w_window_is_252_sessions(self):
        """Guards the reason MIN_BARS is what it is: a shorter window would quietly
        answer a different question than 'the 52-week high'."""
        self.assertEqual(features.LOOKBACK_52W, 252)
        self.assertGreaterEqual(features.MIN_BARS, features.LOOKBACK_52W)

    def test_distances_are_signed_fractions(self):
        computed = features.compute(self.bars)
        self.assertLessEqual(computed["dist_52w_high"], 0.0)
        self.assertGreaterEqual(computed["dist_52w_low"], 0.0)
        self.assertAlmostEqual(
            computed["dist_sma_20"], computed["close"] / computed["sma_20"] - 1, places=9
        )

    def test_gap_uses_the_prior_close(self):
        computed = features.compute(self.bars)
        expected = self.bars[-1]["open"] / self.bars[-2]["close"] - 1
        self.assertAlmostEqual(computed["gap_pct"], expected, places=9)


if __name__ == "__main__":
    unittest.main()
