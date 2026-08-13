"""The committed liquidity snapshot: what it captures, and reading it back.

    python -m unittest discover -s tests -v

The consistency test matters more than it looks: this module hard-codes its
own copy of LIQUIDITY_SESSIONS (importing upcoming.py's would circular-import,
since upcoming.py imports this module for the cache_context() fallback). If
the two ever drift, a runner's tercile split stops matching a local run's.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from data import fetch, liquidity_snapshot, upcoming


def bars(closes_and_volumes):
    rows = closes_and_volumes
    return pd.DataFrame({
        # date_range, not manual "2026-05-{day:02d}" strings — a fixture with
        # more than 31 rows would silently overflow past day 31 otherwise.
        "date": pd.date_range("2026-01-01", periods=len(rows), freq="D"),
        "open": [c for c, _ in rows], "high": [c for c, _ in rows],
        "low": [c for c, _ in rows], "close": [c for c, _ in rows],
        "adj_close": [c for c, _ in rows], "volume": [v for _, v in rows],
        "dividend": [0.0] * len(rows), "split": [0.0] * len(rows),
    })


class CacheDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._cache = fetch.CACHE_DIR
        fetch.CACHE_DIR = self.tmp / "cache"

    def tearDown(self):
        fetch.CACHE_DIR = self._cache
        shutil.rmtree(self.tmp, ignore_errors=True)


class ConsistencyTests(unittest.TestCase):
    def test_the_session_window_matches_upcomings_own(self):
        self.assertEqual(liquidity_snapshot.LIQUIDITY_SESSIONS,
                         upcoming.LIQUIDITY_SESSIONS)


class BuildTests(CacheDirTestCase):
    def test_the_snapshot_carries_latest_close_and_average_turnover(self):
        fetch.write_cache("AAA", bars([(100.0, 1000), (102.0, 2000),
                                       (104.0, 3000)]))
        frame = liquidity_snapshot.build_snapshot(["AAA"])
        self.assertEqual(len(frame), 1)
        row = frame.iloc[0]
        self.assertEqual(row["symbol"], "AAA")
        self.assertEqual(row["close"], 104.0)   # the LATEST close, not a mean
        expected_turnover = (100 * 1000 + 102 * 2000 + 104 * 3000) / 3
        self.assertAlmostEqual(row["avg_turnover_60d"], expected_turnover)
        self.assertEqual(row["asof_date"], "2026-01-03")

    def test_only_the_trailing_window_counts(self):
        long_history = [(100.0 + i, 1000) for i in range(90)]
        fetch.write_cache("BBB", bars(long_history))
        frame = liquidity_snapshot.build_snapshot(["BBB"])
        # 90 sessions of history, only the last 60 should feed the average —
        # a naive mean over all 90 would differ from one over the last 60.
        full_mean = sum(c for c, _ in long_history) * 1000 / 90
        self.assertNotAlmostEqual(frame.iloc[0]["avg_turnover_60d"], full_mean,
                                  places=0)

    def test_an_uncached_symbol_is_silently_skipped(self):
        frame = liquidity_snapshot.build_snapshot(["GHOST"])
        self.assertTrue(frame.empty)
        self.assertEqual(tuple(frame.columns), liquidity_snapshot.COLUMNS)

    def test_defaults_to_every_cached_symbol_when_none_given(self):
        fetch.write_cache("AAA", bars([(10.0, 100)]))
        fetch.write_cache("BBB", bars([(20.0, 200)]))
        frame = liquidity_snapshot.build_snapshot()
        self.assertEqual(sorted(frame["symbol"]), ["AAA", "BBB"])


class RoundTripTests(CacheDirTestCase):
    def test_write_then_read_reproduces_the_context(self):
        fetch.write_cache("AAA", bars([(100.0, 1000), (104.0, 3000)]))
        built = liquidity_snapshot.build_snapshot(["AAA"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.csv"
            liquidity_snapshot.write_snapshot(built, path)
            context, asof = liquidity_snapshot.snapshot_context(path)
        self.assertIn("AAA", context)
        close, turnover = context["AAA"]
        self.assertEqual(close, 104.0)
        self.assertEqual(asof.date().isoformat(), "2026-01-02")

    def test_a_missing_file_is_an_empty_context_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, asof = liquidity_snapshot.snapshot_context(Path(tmp) / "none.csv")
        self.assertEqual(context, {})
        self.assertIsNone(asof)


class RunTests(CacheDirTestCase):
    def test_an_empty_cache_refuses_rather_than_writing_nothing(self):
        self.assertEqual(liquidity_snapshot.run(), 1)

    def test_a_populated_cache_writes_and_reports_the_asof_date(self):
        fetch.write_cache("AAA", bars([(100.0, 1000)]))
        self._snapshot = liquidity_snapshot.SNAPSHOT_PATH
        liquidity_snapshot.SNAPSHOT_PATH = self.tmp / "snap.csv"
        try:
            self.assertEqual(liquidity_snapshot.run(), 0)
            self.assertTrue(liquidity_snapshot.SNAPSHOT_PATH.exists())
        finally:
            liquidity_snapshot.SNAPSHOT_PATH = self._snapshot


if __name__ == "__main__":
    unittest.main()
