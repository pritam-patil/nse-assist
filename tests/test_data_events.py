"""The dividend event table: extraction, the split basis, and validation.

    python -m unittest discover -s tests -v

The basis regression is the test that earns its keep. Yahoo serves EVERYTHING
split-adjusted to the current share basis at the source — closes, volumes and
dividend amounts alike (verified against TATASTEEL's 2022 1:10 split). The
first version of events.py adjusted for splits a second time and understated
every pre-split yield by the split factor; SplitBasisTests builds exactly that
shape and asserts the numbers come out untouched.
"""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from data import events, fetch


def bars(rows):
    """A cache-schema frame: [(date, close, volume, dividend, split)]."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime([r[0] for r in rows]),
            "open": [r[1] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[1] for r in rows],
            "close": [r[1] for r in rows],
            "adj_close": [r[1] for r in rows],
            "volume": [r[2] for r in rows],
            "dividend": [r[3] for r in rows],
            "split": [r[4] for r in rows],
        }
    )


class CacheDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._original = fetch.CACHE_DIR
        fetch.CACHE_DIR = self.tmp

    def tearDown(self):
        fetch.CACHE_DIR = self._original
        shutil.rmtree(self.tmp, ignore_errors=True)


class ExtractionTests(unittest.TestCase):
    def test_one_row_per_dividend_with_the_prior_sessions_close(self):
        frame = bars([
            ("2026-08-01", 100.0, 1000, 0.0, 0.0),
            ("2026-08-04", 102.0, 1100, 0.0, 0.0),
            ("2026-08-05", 100.0, 1200, 2.55, 0.0),
        ])
        rows = events.events_for("X", frame)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["ex_date"], pd.Timestamp("2026-08-05"))
        self.assertEqual(row["amount"], 2.55)
        # The session before ex-date, not the ex-date bar itself.
        self.assertEqual(row["prev_close"], 102.0)
        self.assertAlmostEqual(row["yield_pct"], 2.55 / 102.0 * 100)
        self.assertEqual(row["prior_sessions"], 2)

    def test_averages_exclude_the_ex_date_bar(self):
        frame = bars([
            ("2026-08-01", 100.0, 1000, 0.0, 0.0),
            ("2026-08-04", 200.0, 3000, 0.0, 0.0),
            ("2026-08-05", 999.0, 999999, 5.0, 0.0),
        ])
        row = events.events_for("X", frame)[0]
        self.assertAlmostEqual(row["avg_price_60d"], 150.0)
        self.assertAlmostEqual(row["avg_volume_60d"], 2000.0)

    def test_an_event_on_the_first_bar_keeps_its_row_without_price_context(self):
        frame = bars([("2026-08-05", 100.0, 1000, 3.0, 0.0)])
        row = events.events_for("X", frame)[0]
        self.assertEqual(row["prior_sessions"], 0)
        self.assertNotEqual(row["prev_close"], row["prev_close"])   # NaN
        self.assertNotEqual(row["yield_pct"], row["yield_pct"])     # NaN

    def test_the_prior_window_is_capped_at_sixty_sessions(self):
        history = [(f"2025-{month:02d}-{day:02d}", 100.0, 1000, 0.0, 0.0)
                   for month in range(1, 9) for day in range(1, 29)]
        history.append(("2025-09-01", 100.0, 1000, 1.0, 0.0))
        row = events.events_for("X", bars(history))[0]
        self.assertEqual(row["prior_sessions"], 60)


class SplitBasisTests(unittest.TestCase):
    def test_a_pre_split_event_is_not_adjusted_a_second_time(self):
        # The TATASTEEL shape, scaled: Yahoo already serves the pre-split close as
        # 99.6 (the exchange printed 996) and the 51-rupee payout as 5.1. A later
        # split event in the split column must NOT trigger another division —
        # doing so is how this module once reported a 51% yield on a 9.96 close.
        frame = bars([
            ("2022-06-14", 99.6, 80_000_000, 0.0, 0.0),
            ("2022-06-15", 95.9, 92_000_000, 5.1, 0.0),
            ("2022-07-27", 95.9, 52_000_000, 0.0, 0.0),
            ("2022-07-28", 100.3, 137_000_000, 0.0, 10.0),   # the 1:10 split
        ])
        row = events.events_for("X", frame)[0]
        self.assertAlmostEqual(row["prev_close"], 99.6)
        self.assertAlmostEqual(row["yield_pct"], 5.1 / 99.6 * 100)

    def test_volumes_are_used_as_cached_across_a_split(self):
        frame = bars([
            ("2022-07-27", 95.9, 52_000_000, 0.0, 0.0),
            ("2022-07-28", 100.3, 138_000_000, 1.0, 10.0),
        ])
        row = events.events_for("X", frame)[0]
        # Yahoo already restated history onto the post-split share count; the
        # cached number is the right one.
        self.assertAlmostEqual(row["avg_volume_60d"], 52_000_000)
        self.assertAlmostEqual(row["avg_price_60d"], 95.9)


class SpecialFlagTests(unittest.TestCase):
    """The flag is point-in-time: only strictly-prior payouts and the ex-day
    yield can fire it. Both cuts are strict inequalities."""

    # Closes of 1,000 keep every yield here under 5%, so only the amount rule
    # can fire — a 6-rupee payout on a 100-rupee close would test the yield
    # rule by accident.

    def test_a_payout_over_three_times_the_trailing_median_is_special(self):
        frame = bars([("2024-01-05", 1000.0, 1000, 2.0, 0.0),
                      ("2024-06-05", 1000.0, 1000, 2.0, 0.0),
                      ("2025-01-05", 1000.0, 1000, 6.1, 0.0)])
        flags = [row["special"] for row in events.events_for("X", frame)]
        self.assertEqual(flags, [False, False, True])

    def test_exactly_three_times_is_not_special(self):
        frame = bars([("2024-01-05", 1000.0, 1000, 2.0, 0.0),
                      ("2025-01-05", 1000.0, 1000, 6.0, 0.0)])
        self.assertEqual([r["special"] for r in events.events_for("X", frame)],
                         [False, False])

    def test_a_later_windfall_does_not_relabel_an_earlier_payout(self):
        frame = bars([("2024-01-05", 1000.0, 1000, 2.0, 0.0),
                      ("2025-01-05", 1000.0, 1000, 50.0, 0.0)])
        rows = events.events_for("X", frame)
        self.assertFalse(rows[0]["special"])   # judged on ITS history: none
        self.assertTrue(rows[1]["special"])

    def test_a_first_event_can_only_be_flagged_by_yield(self):
        frame = bars([("2024-01-04", 100.0, 1000, 0.0, 0.0),
                      ("2024-01-05", 100.0, 1000, 5.5, 0.0)])
        rows = events.events_for("X", frame)
        self.assertTrue(rows[0]["special"])    # 5.5% yield, no history needed

    def test_exactly_five_percent_yield_is_not_special(self):
        frame = bars([("2024-01-04", 100.0, 1000, 0.0, 0.0),
                      ("2024-01-05", 100.0, 1000, 5.0, 0.0)])
        self.assertFalse(events.events_for("X", frame)[0]["special"])


class BuildTests(CacheDirTestCase):
    def test_the_table_spans_symbols_sorted_by_date(self):
        fetch.write_cache("BBB", bars([("2026-08-01", 100.0, 1000, 0.0, 0.0),
                                       ("2026-08-04", 100.0, 1000, 1.0, 0.0)]))
        fetch.write_cache("AAA", bars([("2026-08-01", 50.0, 500, 0.0, 0.0),
                                       ("2026-08-05", 50.0, 500, 2.0, 0.0)]))
        table = events.build_events()
        self.assertEqual(list(table["symbol"]), ["BBB", "AAA"])
        self.assertEqual(tuple(table.columns), events.COLUMNS)

    def test_underscore_metadata_files_are_not_symbols(self):
        fetch.write_cache("_nifty500_backup", bars([("2026-08-04", 1.0, 1, 1.0, 0.0)]))
        self.assertEqual(events.cached_symbols(), [])

    def test_an_empty_cache_yields_an_empty_table_with_the_schema(self):
        table = events.build_events()
        self.assertTrue(table.empty)
        self.assertEqual(tuple(table.columns), events.COLUMNS)


class ValidationTests(unittest.TestCase):
    def _table(self):
        return pd.DataFrame({
            "symbol": ["COALINDIA", "COALINDIA"],
            "ex_date": pd.to_datetime(["2025-08-06", "2026-02-10"]),
            "amount": [5.5, 4.0],
            "prev_close": [380.0, 400.0],
            "yield_pct": [1.45, 1.0],
            "avg_volume_60d": [1e6, 1e6],
            "avg_price_60d": [380.0, 400.0],
            "prior_sessions": [60, 60],
        })

    def test_a_matching_event_passes(self):
        results = events.validate(self._table(), [
            {"symbol": "COALINDIA", "ex_date": date(2025, 8, 6), "amount": 5.50}])
        self.assertTrue(results[0]["ok"])

    def test_a_wrong_amount_on_the_right_day_fails_and_says_so(self):
        results = events.validate(self._table(), [
            {"symbol": "COALINDIA", "ex_date": date(2025, 8, 6), "amount": 6.00}])
        self.assertFalse(results[0]["ok"])
        self.assertIn("amount is 5.50", results[0]["detail"])

    def test_a_right_amount_on_a_neighbouring_day_names_the_nearest_event(self):
        results = events.validate(self._table(), [
            {"symbol": "COALINDIA", "ex_date": date(2025, 8, 7), "amount": 5.50}])
        self.assertFalse(results[0]["ok"])
        self.assertIn("2025-08-06", results[0]["detail"])

    def test_an_uncached_symbol_is_its_own_distinct_failure(self):
        results = events.validate(self._table(), [
            {"symbol": "GHOST", "ex_date": date(2025, 8, 6), "amount": 1.0}])
        self.assertFalse(results[0]["ok"])
        self.assertIn("not in the event table", results[0]["detail"])

    def test_amounts_within_half_a_paisa_are_the_same_amount(self):
        table = self._table()
        table.loc[0, "amount"] = 5.5004
        results = events.validate(table, [
            {"symbol": "COALINDIA", "ex_date": date(2025, 8, 6), "amount": 5.50}])
        self.assertTrue(results[0]["ok"])


class RunTests(CacheDirTestCase):
    """End to end against a temp cache and temp params.yaml."""

    def setUp(self):
        super().setUp()
        self._params = events.PARAMS_PATH
        self._events_path = events.EVENTS_PATH
        events.PARAMS_PATH = self.tmp / "params.yaml"
        events.EVENTS_PATH = self.tmp / "events.parquet"
        self.addCleanup(setattr, events, "PARAMS_PATH", self._params)
        self.addCleanup(setattr, events, "EVENTS_PATH", self._events_path)

        fetch.write_cache("COALINDIA", bars([
            ("2025-08-04", 380.0, 1000, 0.0, 0.0),
            ("2025-08-06", 375.0, 1200, 5.5, 0.0),
        ]))

    def _write_params(self, amount):
        events.PARAMS_PATH.write_text(
            "validation_events:\n"
            f"  - {{symbol: COALINDIA, ex_date: 2025-08-06, amount: {amount}}}\n")

    def test_a_run_whose_pinned_events_match_exits_zero_and_writes_the_table(self):
        self._write_params(5.50)
        self.assertEqual(events.run(), 0)
        table = pd.read_parquet(events.EVENTS_PATH)
        self.assertEqual(len(table), 1)

    def test_a_pinned_mismatch_fails_the_run_but_still_writes_the_table(self):
        self._write_params(9.99)
        self.assertEqual(events.run(), 1)
        # Written anyway: the table is the evidence you debug WITH.
        self.assertTrue(events.EVENTS_PATH.exists())

    def test_an_empty_cache_is_a_loud_failure_not_an_empty_table(self):
        for path in fetch.CACHE_DIR.glob("*.parquet"):
            path.unlink()
        self.assertEqual(events.run(), 1)


if __name__ == "__main__":
    unittest.main()
