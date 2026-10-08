"""The index baseline must follow the stocks forward, and never run ahead of them.

    python -m unittest discover -s tests -v

ensure_benchmark() used to return as soon as any index row existed, so its first
fetch was also its last. The stored NIFTY stopped at 2026-07-30 and the gate's
fifth criterion — paper P&L against the index over the same days — read "no index
data" for the entire paper window, which began 2026-08-04. Nothing failed; the
criterion just never had a number.
"""

import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

from src import backtest
from src.db import get_connection, init_db

INDEX = backtest.BENCHMARK_SYMBOL


def _bar(symbol, day, close=100.0):
    return (symbol, day, close, close, close, close, 1000, "test")


def _index_frame(days, close=110.0):
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 0},
        index=pd.to_datetime(days),
    )


class BenchmarkRefreshTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _store(self, rows):
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, "
            "volume, source) VALUES (?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def _index_through(self):
        return self.conn.execute(
            "SELECT MAX(date) FROM prices WHERE symbol = ?", (INDEX,)).fetchone()[0]

    def test_a_stale_series_is_fetched_forward_to_the_newest_stock_session(self):
        self._store([_bar(INDEX, "2026-07-29"), _bar(INDEX, "2026-07-30"),
                     _bar("RELIANCE", "2026-07-30"), _bar("RELIANCE", "2026-07-31"),
                     _bar("RELIANCE", "2026-08-03")])
        fetched = _index_frame(["2026-07-31", "2026-08-03"])
        with mock.patch("yfinance.download", return_value=fetched) as download:
            backtest.ensure_benchmark(self.conn)

        kwargs = download.call_args.kwargs
        self.assertEqual(kwargs["start"], "2026-07-31", "from the day after the last stored bar")
        self.assertEqual(kwargs["end"], "2026-08-04", "yfinance's end is exclusive")
        self.assertEqual(self._index_through(), "2026-08-03")
        self.assertIsNotNone(backtest.benchmark_return(self.conn, "2026-07-30", "2026-08-03"))

    def test_a_current_series_does_not_touch_the_network(self):
        self._store([_bar(INDEX, "2026-08-03"), _bar("RELIANCE", "2026-08-03")])
        with mock.patch("yfinance.download") as download:
            backtest.ensure_benchmark(self.conn)
        download.assert_not_called()

    def test_the_index_never_runs_ahead_of_the_stocks(self):
        """yfinance hands out a partial bar for a session still trading. An index
        bar with no stocks beside it describes a day nothing else has seen."""
        self._store([_bar(INDEX, "2026-07-30"), _bar("RELIANCE", "2026-08-03")])
        fetched = _index_frame(["2026-07-31", "2026-08-03", "2026-08-04"])
        with mock.patch("yfinance.download", return_value=fetched):
            backtest.ensure_benchmark(self.conn)
        self.assertEqual(self._index_through(), "2026-08-03")

    def test_an_empty_table_still_gets_its_first_fetch(self):
        self._store([_bar("RELIANCE", "2026-07-30"), _bar("RELIANCE", "2026-07-31")])
        fetched = _index_frame(["2026-07-30", "2026-07-31"])
        with mock.patch("yfinance.download", return_value=fetched) as download:
            backtest.ensure_benchmark(self.conn)
        self.assertEqual(download.call_args.kwargs["start"], "2026-07-30")
        self.assertEqual(self._index_through(), "2026-07-31")

    def test_a_failed_fetch_degrades_rather_than_raises(self):
        self._store([_bar(INDEX, "2026-07-30"), _bar("RELIANCE", "2026-08-03")])
        with mock.patch("yfinance.download", side_effect=RuntimeError("rate limited")):
            self.assertEqual(backtest.ensure_benchmark(self.conn), 0)
        self.assertEqual(self._index_through(), "2026-07-30")


if __name__ == "__main__":
    unittest.main()
