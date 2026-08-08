"""The research cache: shaping, incremental refresh, and the retry file.

    python -m unittest discover -s tests -v

Network never happens here. yfinance is faked at fetch._download — the seam the
module exposes for exactly this — and the NIFTY 500 fetch is exercised against
canned CSV text. What these tests pin down is the behaviour that makes the cache
trustworthy: the partial-bar overlap, the full-refetch-on-event rule that keeps
adj_close on one basis, and a retry file that converges instead of accumulating.
"""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

from data import fetch


def yahoo_frame(rows):
    """A raw yfinance-shaped frame: [(date, open, high, low, close, adj, vol, div, split)]."""
    index = pd.DatetimeIndex([r[0] for r in rows], name="Date")
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Adj Close": [r[5] for r in rows],
            "Volume": [r[6] for r in rows],
            "Dividends": [r[7] for r in rows],
            "Stock Splits": [r[8] for r in rows],
        },
        index=index,
    )


def cache_frame(rows):
    """A frame already in the cache schema: [(date, close, dividend, split)] with
    the remaining columns derived — tests here care about dates and events."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime([r[0] for r in rows]),
            "open": [r[1] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[1] for r in rows],
            "close": [r[1] for r in rows],
            "adj_close": [r[1] for r in rows],
            "volume": [1000] * len(rows),
            "dividend": [r[2] for r in rows],
            "split": [r[3] for r in rows],
        }
    )


class CacheDirTestCase(unittest.TestCase):
    """Every test gets a throwaway cache directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._original = fetch.CACHE_DIR
        fetch.CACHE_DIR = self.tmp

    def tearDown(self):
        fetch.CACHE_DIR = self._original
        shutil.rmtree(self.tmp, ignore_errors=True)


class TidyTests(unittest.TestCase):
    def test_output_matches_the_cache_schema_sorted_by_date(self):
        raw = yahoo_frame([
            ("2026-08-05", 11, 12, 10, 11.5, 11.4, 500, 0.0, 0.0),
            ("2026-08-04", 10, 11, 9, 10.5, 10.4, 400, 0.0, 0.0),
        ])
        frame = fetch.tidy(raw)
        self.assertEqual(tuple(frame.columns), fetch.COLUMNS)
        self.assertEqual(
            [d.date().isoformat() for d in frame["date"]],
            ["2026-08-04", "2026-08-05"],
        )

    def test_phantom_holiday_bars_are_dropped(self):
        raw = yahoo_frame([
            ("2026-08-04", 10, 11, 9, 10.5, 10.4, 400, 0.0, 0.0),
            # Zero volume, no range: Yahoo's holiday placeholder.
            ("2026-08-05", 10.5, 10.5, 10.5, 10.5, 10.4, 0, 0.0, 0.0),
        ])
        frame = fetch.tidy(raw)
        self.assertEqual(len(frame), 1)

    def test_a_flat_bar_carrying_an_event_survives(self):
        raw = yahoo_frame([
            ("2026-08-05", 10.5, 10.5, 10.5, 10.5, 10.4, 0, 2.5, 0.0),
        ])
        frame = fetch.tidy(raw)
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame["dividend"].iloc[0], 2.5)

    def test_rows_without_a_close_are_dropped(self):
        raw = yahoo_frame([
            ("2026-08-04", 10, 11, 9, 10.5, 10.4, 400, 0.0, 0.0),
            ("2026-08-05", None, None, float("nan"), float("nan"), None, 0, 0.0, 0.0),
        ])
        self.assertEqual(len(fetch.tidy(raw)), 1)

    def test_missing_event_columns_become_zero_not_an_error(self):
        raw = yahoo_frame([("2026-08-04", 10, 11, 9, 10.5, 10.4, 400, 0.0, 0.0)])
        raw = raw.drop(columns=["Dividends", "Stock Splits"])
        frame = fetch.tidy(raw)
        self.assertEqual(frame["dividend"].iloc[0], 0.0)
        self.assertEqual(frame["split"].iloc[0], 0.0)


class MergeAndEventTests(unittest.TestCase):
    def test_the_fresh_copy_wins_on_an_overlapping_date(self):
        # The cached bar is a mid-session partial; fresh carries the real close.
        cached = cache_frame([("2026-08-04", 100.0, 0.0, 0.0)])
        fresh = cache_frame([("2026-08-04", 104.0, 0.0, 0.0),
                             ("2026-08-05", 105.0, 0.0, 0.0)])
        merged = fetch.merge(cached, fresh)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged["close"].iloc[0], 104.0)

    def test_an_event_already_cached_on_the_overlap_bar_is_not_new(self):
        cached = cache_frame([("2026-08-04", 100.0, 2.5, 0.0)])
        fresh = cache_frame([("2026-08-04", 100.0, 2.5, 0.0),
                             ("2026-08-05", 101.0, 0.0, 0.0)])
        self.assertFalse(fetch.has_new_events(cached, fresh))

    def test_a_dividend_the_cache_has_not_seen_is_new(self):
        cached = cache_frame([("2026-08-04", 100.0, 0.0, 0.0)])
        fresh = cache_frame([("2026-08-04", 100.0, 0.0, 0.0),
                             ("2026-08-05", 101.0, 2.5, 0.0)])
        self.assertTrue(fetch.has_new_events(cached, fresh))

    def test_a_split_counts_as_an_event_too(self):
        cached = cache_frame([("2026-08-04", 100.0, 0.0, 0.0)])
        fresh = cache_frame([("2026-08-05", 20.0, 0.0, 5.0)])
        self.assertTrue(fetch.has_new_events(cached, fresh))


class PlanTests(CacheDirTestCase):
    def test_an_uncached_symbol_gets_the_full_window(self):
        plans = fetch.plan(["NEW"], date(2016, 8, 9))
        self.assertEqual(plans, [("NEW", date(2016, 8, 9))])

    def test_a_cached_symbol_resumes_from_its_last_bar_not_the_day_after(self):
        fetch.write_cache("HAVE", cache_frame([("2026-08-04", 100.0, 0.0, 0.0)]))
        plans = fetch.plan(["HAVE"], date(2016, 8, 9))
        # FROM the last bar: the overlap is what lets a partial bar be replaced.
        self.assertEqual(plans, [("HAVE", date(2026, 8, 4))])

    def test_force_ignores_the_cache(self):
        fetch.write_cache("HAVE", cache_frame([("2026-08-04", 100.0, 0.0, 0.0)]))
        plans = fetch.plan(["HAVE"], date(2016, 8, 9), force=True)
        self.assertEqual(plans, [("HAVE", date(2016, 8, 9))])

    def test_full_window_fetches_sort_first_so_they_batch_together(self):
        fetch.write_cache("HAVE", cache_frame([("2026-08-04", 100.0, 0.0, 0.0)]))
        plans = fetch.plan(["HAVE", "NEW"], date(2016, 8, 9))
        self.assertEqual([symbol for symbol, _ in plans], ["NEW", "HAVE"])


class RefreshTests(CacheDirTestCase):
    """refresh() against a faked _download; time.sleep patched to keep tests fast."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(fetch.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_new_symbol_lands_in_its_own_parquet_file(self):
        fresh = cache_frame([("2026-08-04", 100.0, 0.0, 0.0)])
        with mock.patch.object(fetch, "_download", return_value={"NEW": fresh}):
            result = fetch.refresh(["NEW"])
        self.assertEqual(result["fetched"], ["NEW"])
        stored = fetch.read_cache("NEW")
        self.assertEqual(len(stored), 1)

    def test_incremental_rows_are_appended_to_the_cache(self):
        fetch.write_cache("HAVE", cache_frame([("2026-08-03", 99.0, 0.0, 0.0),
                                               ("2026-08-04", 100.0, 0.0, 0.0)]))
        fresh = cache_frame([("2026-08-04", 100.0, 0.0, 0.0),
                             ("2026-08-05", 101.0, 0.0, 0.0)])
        with mock.patch.object(fetch, "_download", return_value={"HAVE": fresh}):
            fetch.refresh(["HAVE"])
        self.assertEqual(len(fetch.read_cache("HAVE")), 3)

    def test_a_new_event_triggers_a_full_window_refetch(self):
        fetch.write_cache("DIV", cache_frame([("2026-08-04", 100.0, 0.0, 0.0)]))
        incremental = cache_frame([("2026-08-05", 101.0, 2.5, 0.0)])
        full = cache_frame([("2026-08-01", 97.0, 0.0, 0.0),
                            ("2026-08-04", 100.0, 0.0, 0.0),
                            ("2026-08-05", 101.0, 2.5, 0.0)])
        calls = []

        def fake(symbols, start, end=None):
            calls.append((tuple(symbols), start))
            return {"DIV": full if len(calls) > 1 else incremental}

        with mock.patch.object(fetch, "_download", side_effect=fake):
            result = fetch.refresh(["DIV"], years=1)

        self.assertEqual(result["fetched"], ["DIV"])
        # Second call: this one symbol, from the full window's start.
        self.assertEqual(calls[1][0], ("DIV",))
        self.assertLess(calls[1][1], calls[0][1])
        # The cache is the full refetch, not an append across the seam.
        self.assertEqual(len(fetch.read_cache("DIV")), 3)

    def test_an_empty_incremental_result_is_unchanged_not_a_failure(self):
        fetch.write_cache("SHUT", cache_frame([("2026-08-04", 100.0, 0.0, 0.0)]))
        with mock.patch.object(fetch, "_download", return_value={}):
            result = fetch.refresh(["SHUT"])
        self.assertEqual(result["unchanged"], ["SHUT"])
        self.assertEqual(result["failures"], {})
        self.assertEqual(fetch.read_retry(), {})

    def test_no_data_for_an_uncached_symbol_is_a_failure(self):
        with mock.patch.object(fetch, "_download", return_value={}):
            result = fetch.refresh(["GHOST"])
        self.assertIn("GHOST", result["failures"])
        self.assertIn("GHOST", fetch.read_retry())

    def test_a_dead_batch_does_not_stop_the_batches_after_it(self):
        good = cache_frame([("2026-08-04", 100.0, 0.0, 0.0)])
        calls = []

        def fake(symbols, start, end=None):
            calls.append(tuple(symbols))
            if "BAD" in symbols:
                raise RuntimeError("yahoo said no")
            return {s: good for s in symbols}

        with mock.patch.object(fetch, "_download", side_effect=fake):
            result = fetch.refresh(["BAD", "GOOD"], batch_size=1)

        self.assertEqual(result["fetched"], ["GOOD"])
        self.assertEqual(list(result["failures"]), ["BAD"])
        self.assertEqual(len(calls), 2)

    def test_batches_are_separated_by_the_polite_pause(self):
        good = cache_frame([("2026-08-04", 100.0, 0.0, 0.0)])
        with mock.patch.object(
            fetch, "_download",
            side_effect=lambda symbols, start, end=None: {s: good for s in symbols},
        ):
            fetch.refresh(["A", "B", "C"], batch_size=1, pause=2.0)
        # Two gaps for three batches; no trailing sleep after the last.
        self.sleep.assert_has_calls([mock.call(2.0), mock.call(2.0)])
        self.assertEqual(self.sleep.call_count, 2)


class RetryFileTests(CacheDirTestCase):
    def test_a_failure_is_recorded_with_its_reason(self):
        fetch.update_retry(["X"], {"X": "timed out"})
        self.assertEqual(fetch.read_retry(), {"X": "timed out"})

    def test_a_success_clears_the_entry(self):
        fetch.update_retry(["X"], {"X": "timed out"})
        fetch.update_retry(["X"], {})
        self.assertEqual(fetch.read_retry(), {})
        self.assertFalse(fetch.retry_path().exists())

    def test_symbols_not_attempted_keep_their_entries(self):
        fetch.update_retry(["X", "Y"], {"X": "timed out", "Y": "no data"})
        # A later run touches only X; Y's entry must survive it.
        fetch.update_retry(["X"], {})
        self.assertEqual(fetch.read_retry(), {"Y": "no data"})

    def test_blank_and_comment_lines_are_ignored(self):
        fetch.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fetch.retry_path().write_text("# failures\n\nX\ttimed out\n")
        self.assertEqual(fetch.read_retry(), {"X": "timed out"})


NIFTY500_CSV = "Company Name,Industry,Symbol,Series,ISIN Code\n" + "\n".join(
    f"Company {i},Industry,SYM{i},EQ,INE{i:06d}" for i in range(500)
)


class Nifty500Tests(CacheDirTestCase):
    def test_parse_reads_the_symbol_column(self):
        symbols = fetch._parse_nifty500(NIFTY500_CSV)
        self.assertEqual(len(symbols), 500)
        self.assertEqual(symbols[0], "SYM0")

    def test_non_eq_series_rows_are_dropped(self):
        text = NIFTY500_CSV + "\nBond Thing,Industry,BONDX,GB,INE999999"
        self.assertNotIn("BONDX", fetch._parse_nifty500(text))

    def test_a_truncated_file_raises_rather_than_half_warming(self):
        short = "Company Name,Industry,Symbol,Series,ISIN Code\nOnly One,Ind,ONE,EQ,INE1"
        with self.assertRaises(RuntimeError):
            fetch._parse_nifty500(short)

    def test_a_fresh_cached_list_short_circuits_the_network(self):
        fetch.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fetch.nifty500_path().write_text(NIFTY500_CSV)
        with mock.patch.object(fetch.requests, "Session",
                               side_effect=AssertionError("network touched")):
            symbols = fetch.nifty500_symbols()
        self.assertEqual(len(symbols), 500)

    def test_a_failed_fetch_falls_back_to_the_stale_cached_copy(self):
        fetch.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fetch.nifty500_path().write_text(NIFTY500_CSV)
        with mock.patch.object(fetch.requests, "Session",
                               side_effect=RuntimeError("nse down")):
            symbols = fetch.nifty500_symbols(max_age_days=-1)
        self.assertEqual(len(symbols), 500)

    def test_a_failed_fetch_with_no_cache_raises(self):
        with mock.patch.object(fetch.requests, "Session",
                               side_effect=RuntimeError("nse down")):
            with self.assertRaises(RuntimeError):
                fetch.nifty500_symbols()


class CliTests(CacheDirTestCase):
    def test_symbols_flag_is_parsed_and_uppercased(self):
        seen = {}

        def fake_refresh(symbols, years, force):
            seen["symbols"] = symbols
            return {"fetched": symbols, "unchanged": [], "failures": {}}

        with mock.patch.object(fetch, "refresh", side_effect=fake_refresh):
            code = fetch.main(["--symbols", "reliance, tcs"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["symbols"], ["RELIANCE", "TCS"])

    def test_retry_with_an_empty_file_does_nothing_successfully(self):
        self.assertEqual(fetch.main(["--retry"]), 0)

    def test_retry_feeds_exactly_the_recorded_symbols_back_in(self):
        fetch.update_retry(["B", "A"], {"B": "x", "A": "y"})
        seen = {}

        def fake_refresh(symbols, years, force):
            seen["symbols"] = symbols
            return {"fetched": symbols, "unchanged": [], "failures": {}}

        with mock.patch.object(fetch, "refresh", side_effect=fake_refresh):
            fetch.main(["--retry"])
        self.assertEqual(seen["symbols"], ["A", "B"])

    def test_a_run_where_nothing_succeeded_exits_nonzero(self):
        with mock.patch.object(
            fetch, "refresh",
            return_value={"fetched": [], "unchanged": [], "failures": {"X": "no"}},
        ):
            self.assertEqual(fetch.main(["--symbols", "X"]), 1)


if __name__ == "__main__":
    unittest.main()
