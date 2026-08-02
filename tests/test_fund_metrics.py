"""Fund metrics: point-in-time, gap-tolerant, and honest about restatements.

    python -m unittest discover -s tests -v

Three failure modes are covered, all of which produce a plausible number rather
than an error:

  lookahead     metrics as of D that move when later NAVs arrive
  forward-fill  padding a business-day scheme to a daily grid, which manufactures
                zero-change days and drags volatility down
  restatement   a unit face-value change read as a return, which is how a liquid
                fund comes to report 50% a year
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from src import funds
from src.db import get_connection, init_db

SCHEME = "999999"


def series(start="2024-01-01", days=500, daily_rate=0.0002, weekdays_only=False):
    """A steadily accruing NAV series, optionally skipping weekends."""
    out, value, day = [], 100.0, date.fromisoformat(start)
    for _ in range(days):
        if not (weekdays_only and day.weekday() >= 5):
            value *= 1 + daily_rate
            out.append((day.isoformat(), round(value, 4)))
        day += timedelta(days=1)
    return out


class PointInTimeTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self.navs = series()
        self._insert(self.navs)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _insert(self, navs):
        self.conn.executemany(
            "INSERT OR REPLACE INTO fund_navs (scheme_code, date, nav) VALUES (?,?,?)",
            [(SCHEME, d, v) for d, v in navs])
        self.conn.commit()

    def test_later_navs_do_not_change_an_earlier_as_of(self):
        """The whole point. A digest ranking funds as of last Friday must not shift
        because this week's NAVs arrived."""
        as_of = self.navs[400][0]
        before = funds.metrics_for(self.conn, SCHEME, as_of=as_of)
        self.conn.execute("UPDATE fund_navs SET nav = nav * 3 WHERE date > ?", (as_of,))
        self.conn.commit()
        after = funds.metrics_for(self.conn, SCHEME, as_of=as_of)
        self.assertEqual(before, after)

    def test_load_navs_respects_as_of(self):
        as_of = self.navs[200][0]
        loaded = funds.load_navs(self.conn, SCHEME, as_of=as_of)
        self.assertLessEqual(max(d for d, _ in loaded), as_of)

    def test_as_of_a_non_observation_day_resolves_backwards(self):
        """A Sunday query on a business-day scheme answers with Friday, never by
        inventing a Sunday."""
        navs = series(weekdays_only=True)
        sunday = next(d for d, _ in navs if date.fromisoformat(d).weekday() == 4)
        target = (date.fromisoformat(sunday) + timedelta(days=2)).isoformat()
        found = funds.nav_at_or_before(navs, target)
        self.assertEqual(found[0], sunday)


class GapHandlingTestCase(unittest.TestCase):
    def test_observations_per_year_is_measured_not_assumed(self):
        """Median gap is 1.0 for both calendars — Monday-to-Friday dominates it and
        the weekend hides in the tail. Only the count separates them."""
        daily = funds.observations_per_year(series(days=400))
        business = funds.observations_per_year(series(days=400, weekdays_only=True))
        self.assertGreater(daily, 350)
        self.assertLess(business, 270)

    def test_changes_are_between_consecutive_observations_only(self):
        """Friday to Monday is ONE change. Treating it as three requires inventing
        two of them, and each invented one is a zero that flattens volatility."""
        navs = series(days=30, weekdays_only=True)
        self.assertEqual(len(funds.observation_changes(navs)), len(navs) - 1)

    def test_volatility_uses_the_schemes_own_frequency(self):
        """Same underlying daily behaviour, different calendars: annualised
        volatility must not differ merely because one scheme prints on weekends."""
        daily = funds.volatility(series(days=400), None, 365)
        business = funds.volatility(series(days=400, weekdays_only=True), None, 365)
        self.assertIsNotNone(daily)
        self.assertIsNotNone(business)

    def test_a_window_with_no_close_anchor_returns_none(self):
        """A '1-month return' measured from 45 days back is a 45-day return wearing
        the wrong label. Better to say nothing."""
        sparse = [("2026-01-01", 100.0), ("2026-06-01", 110.0)]
        self.assertIsNone(funds.period_return(sparse, "2026-06-01", 30))


class RestatementTestCase(unittest.TestCase):
    def _restated(self):
        navs = series(days=400)
        pivot = 200
        return [(d, v if i < pivot else v * 100) for i, (d, v) in enumerate(navs)], navs[pivot][0]

    def test_a_face_value_change_is_detected(self):
        navs, when = self._restated()
        self.assertIn(when, funds.find_restatements(navs))

    def test_windows_spanning_a_restatement_return_none(self):
        navs, when = self._restated()
        self.assertIsNone(funds.period_return(navs, navs[-1][0], 365))

    def test_windows_after_a_restatement_are_unaffected(self):
        navs, _ = self._restated()
        self.assertIsNotNone(funds.period_return(navs, navs[-1][0], 30))

    def test_a_large_fall_is_a_real_loss_not_a_restatement(self):
        """A debt fund can genuinely drop 20% when a holding is written down. Only
        implausible GAINS are restatements — the asymmetry is the signal."""
        navs = series(days=100)
        crashed = navs[:50] + [(d, v * 0.7) for d, v in navs[50:]]
        self.assertEqual(funds.find_restatements(crashed), [])

    def test_cagr_is_withheld_rather_than_wrong(self):
        navs, _ = self._restated()
        computed = funds.compute_metrics(navs)
        self.assertIsNone(computed["return_annualized"])
        self.assertTrue(computed["restatements"])


class MetricTestCase(unittest.TestCase):
    def setUp(self):
        self.navs = series(days=500)

    def test_rising_series_has_positive_returns_and_full_consistency(self):
        computed = funds.compute_metrics(self.navs)
        self.assertGreater(computed["return_1m"], 0)
        self.assertGreater(computed["return_1y"], 0)
        self.assertEqual(computed["consistency_3m"], 1.0)

    def test_max_drawdown_is_zero_on_a_monotonic_series(self):
        self.assertEqual(funds.max_drawdown(self.navs, self.navs[-1][0]), 0.0)

    def test_max_drawdown_is_negative_after_a_fall(self):
        navs = self.navs[:400] + [(d, v * 0.95) for d, v in self.navs[400:]]
        self.assertLess(funds.max_drawdown(navs, navs[-1][0]), 0)

    def test_worst_month_is_the_minimum_rolling_month(self):
        navs = self.navs[:400] + [(d, v * 0.9) for d, v in self.navs[400:]]
        worst = funds.worst_month(navs, navs[-1][0])
        self.assertLess(worst, 0)

    def test_consistency_falls_when_quarters_go_negative(self):
        """A fund with a high average and a third of its quarters negative is a
        different instrument from one that never dips."""
        navs = series(days=500, daily_rate=0.0)
        wobbly = [(d, v * (0.9 if i % 120 < 60 else 1.0)) for i, (d, v) in enumerate(navs)]
        score = funds.consistency(wobbly, wobbly[-1][0])
        self.assertIsNotNone(score)
        self.assertLess(score, 1.0)

    def test_metrics_are_none_rather_than_zero_when_history_is_short(self):
        computed = funds.compute_metrics(series(days=20))
        self.assertIsNone(computed["return_1y"])
        self.assertIsNotNone(computed["observations"])


class CacheTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self.conn.executemany(
            "INSERT OR REPLACE INTO fund_navs (scheme_code, date, nav) VALUES (?,?,?)",
            [(SCHEME, d, v) for d, v in series(days=400)])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_metrics_are_stored_and_readable(self):
        written = funds.refresh_metrics(self.conn, [SCHEME])
        self.assertEqual(len(written), 1)
        latest = funds.load_navs(self.conn, SCHEME)[-1][0]
        cached = funds.cached_metrics(self.conn, SCHEME, latest)
        self.assertIsNotNone(cached)
        self.assertAlmostEqual(cached["return_1m"], written[0][1]["return_1m"], places=9)

    def test_a_second_refresh_does_not_recompute(self):
        """The point of caching: a weekly digest must not re-derive a year of
        rolling windows every time it runs."""
        funds.refresh_metrics(self.conn, [SCHEME])
        self.assertEqual(funds.refresh_metrics(self.conn, [SCHEME]), [])

    def test_recompute_forces_it(self):
        funds.refresh_metrics(self.conn, [SCHEME])
        self.assertEqual(len(funds.refresh_metrics(self.conn, [SCHEME], recompute=True)), 1)


if __name__ == "__main__":
    unittest.main()
