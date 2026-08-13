"""The ex-date study: the ratio arithmetic, exclusions, and the results file.

    python -m unittest discover -s tests -v

The arithmetic tests pin the sign conventions, which is where a study like this
quietly inverts: a drop is a POSITIVE ratio, a market fall on ex-date must not
be billed to the dividend, and a stock that fell less than the market shows a
negative ratio rather than a small positive one.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from unittest import mock

from data import events, fetch, nifty_snapshot, study_exdate as study


def event_table(rows):
    """[(symbol, ex_date, amount, prev_date, prev_close, ex_close, yield_pct)]"""
    return pd.DataFrame({
        "symbol": [r[0] for r in rows],
        "ex_date": pd.to_datetime([r[1] for r in rows]),
        "amount": [r[2] for r in rows],
        "prev_date": pd.to_datetime([r[3] for r in rows]),
        "prev_close": [r[4] for r in rows],
        "ex_close": [r[5] for r in rows],
        "yield_pct": [r[6] for r in rows],
    })


def nifty(**closes):
    return {pd.Timestamp(day.replace("_", "-")): value for day, value in closes.items()}


class MeasureTests(unittest.TestCase):
    def test_a_full_drop_on_a_flat_market_is_ratio_one(self):
        table = event_table([("X", "2026-08-05", 2.0, "2026-08-04", 100.0, 98.0, 2.0)])
        index = nifty(**{"2026_08_04": 1000.0, "2026_08_05": 1000.0})
        measured, exclusions = study.measure(table, index)
        self.assertAlmostEqual(measured["drop_ratio"].iloc[0], 1.0)
        self.assertEqual(sum(exclusions.values()), 0)

    def test_the_markets_own_fall_is_not_billed_to_the_dividend(self):
        # Stock fell 3: 2 of it is the market's 2% day, 1 is the dividend.
        table = event_table([("X", "2026-08-05", 1.0, "2026-08-04", 100.0, 97.0, 1.0)])
        index = nifty(**{"2026_08_04": 1000.0, "2026_08_05": 980.0})
        measured, _ = study.measure(table, index)
        self.assertAlmostEqual(measured["drop_ratio"].iloc[0], 1.0)

    def test_a_stock_that_outperformed_shows_a_negative_ratio(self):
        # Flat stock on a falling market gave up less than nothing.
        table = event_table([("X", "2026-08-05", 1.0, "2026-08-04", 100.0, 100.0, 1.0)])
        index = nifty(**{"2026_08_04": 1000.0, "2026_08_05": 980.0})
        measured, _ = study.measure(table, index)
        self.assertLess(measured["drop_ratio"].iloc[0], 0)

    def test_an_event_the_index_cannot_date_is_excluded_and_counted(self):
        table = event_table([("X", "2026-08-05", 2.0, "2026-08-04", 100.0, 98.0, 2.0)])
        index = nifty(**{"2026_08_04": 1000.0})   # no ex-date bar
        measured, exclusions = study.measure(table, index)
        self.assertTrue(measured.empty)
        self.assertEqual(exclusions["index missing a session"], 1)

    def test_an_event_without_a_prior_session_is_excluded_and_counted(self):
        table = event_table([("X", "2026-08-05", 2.0, None, float("nan"), 98.0, float("nan"))])
        measured, exclusions = study.measure(table, {})
        self.assertTrue(measured.empty)
        self.assertEqual(exclusions["no prior session"], 1)


class BucketTests(unittest.TestCase):
    def test_edges_belong_to_the_upper_bucket(self):
        self.assertEqual(study.bucket_label(0.1), "0–0.25%")
        self.assertEqual(study.bucket_label(0.25), "0.25–0.5%")
        self.assertEqual(study.bucket_label(1.0), "1–2%")

    def test_special_dividends_land_in_the_open_ended_bucket(self):
        self.assertEqual(study.bucket_label(23.0), "≥5%")


class SummarizeTests(unittest.TestCase):
    def test_the_headline_subset_is_the_measurable_one(self):
        measured = pd.DataFrame({
            "yield_pct": [0.1, 0.1, 1.5, 2.5],
            "drop_ratio": [40.0, -38.0, 0.8, 0.7],
        })
        summary = study.summarize(measured)
        self.assertEqual(summary["headline"]["n"], 2)
        self.assertAlmostEqual(summary["headline"]["median"], 0.75)
        self.assertEqual(summary["high"]["n"], 1)
        self.assertEqual(summary["all"]["n"], 4)

    def test_empty_buckets_do_not_appear(self):
        measured = pd.DataFrame({"yield_pct": [1.5], "drop_ratio": [0.8]})
        labels = [b["label"] for b in study.summarize(measured)["buckets"]]
        self.assertEqual(labels, ["1–2%"])


class ResultsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "RESULTS.md"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_run_creates_the_log_with_the_section(self):
        study.write_results(f"{study.MARK_START}\nfindings\n{study.MARK_END}", self.path)
        text = self.path.read_text()
        self.assertIn("# Results log", text)
        self.assertIn("findings", text)

    def test_a_rerun_replaces_only_its_own_slice(self):
        self.path.write_text(
            "# Results log\n\nkeep this preamble\n\n"
            f"{study.MARK_START}\nold numbers\n{study.MARK_END}\n\n"
            "## Burst 9 — someone else's section\nkeep this too\n")
        study.write_results(f"{study.MARK_START}\nnew numbers\n{study.MARK_END}", self.path)
        text = self.path.read_text()
        self.assertIn("new numbers", text)
        self.assertNotIn("old numbers", text)
        self.assertIn("keep this preamble", text)
        self.assertIn("keep this too", text)

    def test_a_log_without_markers_gets_the_section_appended(self):
        self.path.write_text("# Results log\n\nhand-written notes\n")
        study.write_results(f"{study.MARK_START}\nfindings\n{study.MARK_END}", self.path)
        text = self.path.read_text()
        self.assertIn("hand-written notes", text)
        self.assertIn("findings", text)


class PlotTests(unittest.TestCase):
    def test_the_figure_lands_on_disk(self):
        measured = pd.DataFrame({
            "yield_pct": [0.4, 1.5, 1.6, 2.5, 6.0],
            "drop_ratio": [2.0, 0.8, 0.9, 0.7, 1.1],
        })
        summary = study.summarize(measured)
        with tempfile.TemporaryDirectory() as tmp:
            path = study.render_plot(measured, summary, Path(tmp) / "plot.png")
            self.assertGreater(path.stat().st_size, 10_000)


class TickerTests(unittest.TestCase):
    def test_nse_symbols_take_the_suffix_and_indices_do_not(self):
        self.assertEqual(fetch.ticker_for("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(fetch.ticker_for("^NSEI"), "^NSEI")


class CachedSymbolTests(unittest.TestCase):
    def test_an_index_in_the_cache_is_not_a_universe_symbol(self):
        tmp = Path(tempfile.mkdtemp())
        original = fetch.CACHE_DIR
        fetch.CACHE_DIR = tmp
        try:
            frame = pd.DataFrame({"date": pd.to_datetime(["2026-08-04"]),
                                  "open": [1.0], "high": [1.0], "low": [1.0],
                                  "close": [1.0], "adj_close": [1.0], "volume": [1],
                                  "dividend": [0.0], "split": [0.0]})
            fetch.write_cache("^NSEI", frame)
            fetch.write_cache("RELIANCE", frame)
            self.assertEqual(events.cached_symbols(), ["RELIANCE"])
        finally:
            fetch.CACHE_DIR = original
            shutil.rmtree(tmp, ignore_errors=True)


class NiftyClosesFallbackTests(unittest.TestCase):
    """The gap a committed data/grid/ alone did not close: notify._survivors()
    also needs NIFTY closes to pair every trade against, and a bare runner has
    no live cache for them either. See data/nifty_snapshot.py."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._cache = fetch.CACHE_DIR
        self._snapshot = nifty_snapshot.SNAPSHOT_PATH
        fetch.CACHE_DIR = self.tmp / "cache"
        nifty_snapshot.SNAPSHOT_PATH = self.tmp / "nifty.csv"

    def tearDown(self):
        fetch.CACHE_DIR = self._cache
        nifty_snapshot.SNAPSHOT_PATH = self._snapshot
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_live_cache_is_used_when_present_even_with_a_snapshot(self):
        fetch.write_cache(study.NIFTY_SYMBOL, pd.DataFrame({
            "date": pd.to_datetime(["2026-08-01"]), "open": [1.0], "high": [1.0],
            "low": [1.0], "close": [50.0], "adj_close": [1.0], "volume": [1],
            "dividend": [0.0], "split": [0.0]}))
        nifty_snapshot.write_snapshot(pd.DataFrame(
            {"date": ["2026-08-01"], "close": [999.0]}))
        closes = study.nifty_closes(refresh=False)
        self.assertEqual(closes[pd.Timestamp("2026-08-01")], 50.0)   # live, not the snapshot

    def test_no_live_cache_falls_back_to_the_committed_snapshot(self):
        nifty_snapshot.write_snapshot(pd.DataFrame(
            {"date": ["2026-08-01"], "close": [75.0]}))
        closes = study.nifty_closes(refresh=False)
        self.assertEqual(closes[pd.Timestamp("2026-08-01")], 75.0)

    def test_neither_source_available_still_raises(self):
        with self.assertRaises(RuntimeError) as caught:
            study.nifty_closes(refresh=False)
        self.assertIn("no committed snapshot", str(caught.exception))

    def test_a_refresh_failure_still_tries_the_snapshot_before_giving_up(self):
        nifty_snapshot.write_snapshot(pd.DataFrame(
            {"date": ["2026-08-01"], "close": [42.0]}))
        with mock.patch.object(study.fetch, "refresh",
                               side_effect=RuntimeError("network down")):
            closes = study.nifty_closes(refresh=True)
        self.assertEqual(closes[pd.Timestamp("2026-08-01")], 42.0)


if __name__ == "__main__":
    unittest.main()
