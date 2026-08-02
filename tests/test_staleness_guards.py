"""Committed constants that go stale silently, and the clocks on them.

    python -m unittest discover -s tests -v

Three constants in this repo describe the world at a moment: the holiday calendar,
the index membership, and the broker's fee schedule. All three drift, none of them
errors when they do, and they fail in decreasing order of loudness:

  calendar   raises on the first date it cannot answer — loud, immediate
  universe   scans last season's index — silent, wrong names
  cost rates shifts every expectancy by a percentage nobody chose — silent, and
             the numbers still look entirely reasonable

The quieter the failure, the more it needs a clock. These are the clocks.
"""

import unittest
from datetime import date

from src import costs, holidays_2026 as calendar, universe, verify_data


class CalendarExpiryTestCase(unittest.TestCase):
    def test_silent_while_there_is_time(self):
        self.assertIsNone(calendar.expiry_warning(date(2026, 8, 2)))

    def test_warns_inside_the_notice_window(self):
        warning = calendar.expiry_warning(date(2026, 12, 1))
        self.assertIn("expires in", warning)
        self.assertIn("2027", warning)

    def test_says_expired_once_past(self):
        warning = calendar.expiry_warning(date(2027, 1, 15))
        self.assertIn("EXPIRED", warning)
        self.assertIn("every run is failing", warning)

    def test_the_notice_window_reaches_past_publication(self):
        """NSE publishes the next year in December. A warning window that closed
        before then would be unactionable for its whole length."""
        first_warning = calendar.COVERAGE_END - __import__("datetime").timedelta(
            days=calendar.EXPIRY_WARNING_DAYS)
        self.assertLessEqual(first_warning.month, 12)
        self.assertGreaterEqual(calendar.EXPIRY_WARNING_DAYS, 30)


class UniverseSnapshotTestCase(unittest.TestCase):
    def test_silent_before_the_next_reconstitution(self):
        self.assertIsNone(universe.snapshot_warning(date(2026, 8, 30)))

    def test_warns_after_a_reconstitution_month_starts(self):
        warning = universe.snapshot_warning(date(2026, 9, 15))
        self.assertIn("reconstitution", warning)
        self.assertIn("niftyindices", warning)

    def test_counts_multiple_missed_reconstitutions(self):
        """Two years of silence should not read the same as one missed season."""
        self.assertIn("3 NIFTY 100", universe.snapshot_warning(date(2027, 10, 1)))

    def test_the_snapshot_date_is_not_in_the_future(self):
        self.assertLessEqual(universe.SNAPSHOT_DATE, date.today())


class CostRatesTestCase(unittest.TestCase):
    def test_silent_inside_one_budget_cycle(self):
        self.assertIsNone(costs.snapshot_warning(date(2027, 3, 1)))

    def test_warns_once_a_budget_has_certainly_passed(self):
        warning = costs.snapshot_warning(date(2028, 1, 1))
        self.assertIn("cost rates are from", warning)
        self.assertIn("contract note", warning)

    def test_the_window_spans_a_full_budget_cycle(self):
        """Statutory charges move in most Union Budgets — presented February,
        effective April. Anything shorter warns before a change could have landed."""
        self.assertGreaterEqual(costs.RATES_MAX_AGE_DAYS, 365)


class DiscontinuityListTestCase(unittest.TestCase):
    def test_every_entry_names_a_universe_symbol(self):
        for symbol, day, _, _ in verify_data.KNOWN_DISCONTINUITIES:
            with self.subTest(symbol=symbol, date=day):
                self.assertIn(symbol, universe.UNIVERSE)

    def test_every_entry_has_a_parseable_date(self):
        for _, day, _, _ in verify_data.KNOWN_DISCONTINUITIES:
            date.fromisoformat(day)

    def test_unverified_entries_say_so_rather_than_guessing(self):
        """The honesty rule for the list: an unverified guess recorded as a finding
        is worse than an open question, because it stops anyone looking again."""
        for symbol, day, status, note in verify_data.KNOWN_DISCONTINUITIES:
            with self.subTest(symbol=symbol):
                self.assertIn(status, ("provider defect", "real move", "structural",
                                       "unreviewed"))
                if status == "unreviewed":
                    self.assertIn("not yet", note.lower())

    def test_the_known_defects_are_recorded(self):
        """TRENT and VEDL were verified against bhavcopy during Burst 5. Losing
        those findings would mean re-deriving them from scratch."""
        keys = verify_data.KNOWN_DISCONTINUITY_KEYS
        self.assertIn(("TRENT", "2026-01-01"), keys)
        self.assertIn(("VEDL", "2026-04-30"), keys)

    def test_no_duplicate_entries(self):
        keys = [(s, d) for s, d, _, _ in verify_data.KNOWN_DISCONTINUITIES]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
