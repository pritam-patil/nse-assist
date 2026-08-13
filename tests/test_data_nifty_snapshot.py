"""The committed NIFTY snapshot: what it captures, and reading it back.

    python -m unittest discover -s tests -v

The consistency test matters more than it looks, same reasoning as the
liquidity snapshot's: this module hard-codes its own copy of NIFTY_SYMBOL
(importing study_exdate's would create the exact circular import this module
exists to avoid, since study_exdate falls back to reading it). If the two
ever drift, the fallback silently stops matching the live source.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from data import fetch, nifty_snapshot, study_exdate


def bars(closes):
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(closes), freq="D"),
        "open": closes, "high": closes, "low": closes, "close": closes,
        "adj_close": closes, "volume": [1000] * len(closes),
        "dividend": [0.0] * len(closes), "split": [0.0] * len(closes),
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
    def test_the_symbol_matches_study_exdates_own(self):
        self.assertEqual(nifty_snapshot.NIFTY_SYMBOL, study_exdate.NIFTY_SYMBOL)


class BuildTests(CacheDirTestCase):
    def test_the_snapshot_is_just_date_and_close(self):
        fetch.write_cache(nifty_snapshot.NIFTY_SYMBOL, bars([100.0, 101.0, 102.0]))
        frame = nifty_snapshot.build_snapshot()
        self.assertEqual(tuple(frame.columns), nifty_snapshot.COLUMNS)
        self.assertEqual(len(frame), 3)
        self.assertEqual(list(frame["close"]), [100.0, 101.0, 102.0])

    def test_no_cached_nifty_history_yields_an_empty_frame_with_the_schema(self):
        frame = nifty_snapshot.build_snapshot()
        self.assertTrue(frame.empty)
        self.assertEqual(tuple(frame.columns), nifty_snapshot.COLUMNS)


class RoundTripTests(CacheDirTestCase):
    def test_write_then_read_reproduces_the_closes(self):
        fetch.write_cache(nifty_snapshot.NIFTY_SYMBOL, bars([100.0, 101.0]))
        built = nifty_snapshot.build_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.csv"
            nifty_snapshot.write_snapshot(built, path)
            closes = nifty_snapshot.snapshot_closes(path)
        self.assertEqual(len(closes), 2)
        self.assertEqual(closes[pd.Timestamp("2026-01-02")], 101.0)

    def test_a_missing_file_is_an_empty_dict_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            closes = nifty_snapshot.snapshot_closes(Path(tmp) / "none.csv")
        self.assertEqual(closes, {})


class RunTests(CacheDirTestCase):
    def test_no_cache_refuses_rather_than_writing_an_empty_file(self):
        self.assertEqual(nifty_snapshot.run(), 1)

    def test_a_populated_cache_writes_the_snapshot(self):
        fetch.write_cache(nifty_snapshot.NIFTY_SYMBOL, bars([100.0]))
        self._snapshot = nifty_snapshot.SNAPSHOT_PATH
        nifty_snapshot.SNAPSHOT_PATH = self.tmp / "snap.csv"
        try:
            self.assertEqual(nifty_snapshot.run(), 0)
            self.assertTrue(nifty_snapshot.SNAPSHOT_PATH.exists())
        finally:
            nifty_snapshot.SNAPSHOT_PATH = self._snapshot


if __name__ == "__main__":
    unittest.main()
