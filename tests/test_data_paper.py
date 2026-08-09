"""Paper tracking: the ledger, the fills, and the real-money gate.

    python -m unittest discover -s tests -v

The gate tests are the point: each of the rule's three legs must be able to
hold the gate shut on its own, and the expected band a trade is graded against
must be the one frozen at log time — the ledger's numbers, not anything
recomputed later.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from data import fetch, paper, upcoming
from tests.test_data_backtest import NOCOST


def ledger_frame(rows):
    """[(status, exit_date, realized_return, realized_net, p25, p75)]"""
    count = len(rows)
    return pd.DataFrame({
        "logged_at": pd.to_datetime(["2026-05-01"] * count),
        "entry_days_before": [20] * count, "exit_days_after": [0] * count,
        "symbol": [f"S{i}" for i in range(count)],
        "ex_date": pd.to_datetime(["2026-06-01"] * count),
        "expected_median_return": [0.009] * count,
        "expected_p25": [r[4] for r in rows],
        "expected_p75": [r[5] for r in rows],
        "status": [r[0] for r in rows],
        "entry_date": pd.to_datetime(["2026-05-05"] * count),
        "entry_close": [100.0] * count,
        "exit_date": pd.to_datetime([r[1] for r in rows]),
        "exit_close": [101.0] * count,
        "dividend": [1.0] * count,
        "realized_net": [r[3] for r in rows],
        "realized_return": [r[2] for r in rows],
    })


def runs_frame(first):
    return pd.DataFrame({"ran_at": pd.to_datetime([first]),
                         "survivors": [1], "candidates_logged": [3]})


class PaperDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._paper = paper.PAPER_DIR
        paper.PAPER_DIR = self.tmp / "paper"

    def tearDown(self):
        paper.PAPER_DIR = self._paper
        shutil.rmtree(self.tmp, ignore_errors=True)


class GateTests(unittest.TestCase):
    GOOD = [("closed", "2026-06-01", 0.008, 80.0, 0.003, 0.013)] * 35

    def test_all_three_legs_met_clears_the_gate(self):
        cleared, reasons = paper.gate(ledger_frame(self.GOOD),
                                      runs_frame("2026-05-01"),
                                      today="2026-08-09")
        self.assertTrue(cleared)
        self.assertEqual(reasons, [])

    def test_no_runs_means_the_clock_has_not_started(self):
        cleared, reasons = paper.gate(ledger_frame([]),
                                      pd.DataFrame(columns=["ran_at"]),
                                      today="2026-08-09")
        self.assertFalse(cleared)
        self.assertIn("clock has not started", reasons[0])

    def test_three_months_must_actually_elapse(self):
        cleared, reasons = paper.gate(ledger_frame(self.GOOD),
                                      runs_frame("2026-07-01"),
                                      today="2026-08-09")
        self.assertFalse(cleared)
        self.assertTrue(any("three months complete on 2026-10-01" in r
                            for r in reasons))

    def test_the_trade_floor_holds_the_gate_alone(self):
        few = self.GOOD[:5]
        cleared, reasons = paper.gate(ledger_frame(few), runs_frame("2026-05-01"),
                                      today="2026-08-09")
        self.assertFalse(cleared)
        self.assertTrue(any("of 30 required" in r for r in reasons))

    def test_a_realized_median_outside_the_frozen_band_fails_dispersion(self):
        bad = [("closed", "2026-06-01", -0.004, -40.0, 0.003, 0.013)] * 35
        cleared, reasons = paper.gate(ledger_frame(bad), runs_frame("2026-05-01"),
                                      today="2026-08-09")
        self.assertFalse(cleared)
        self.assertTrue(any("outside the expected band" in r for r in reasons))

    def test_pending_trades_do_not_count_as_evidence(self):
        rows = self.GOOD[:35] + [("pending", "2026-06-01", None, None,
                                  0.003, 0.013)] * 10
        cleared, _ = paper.gate(ledger_frame(rows), runs_frame("2026-05-01"),
                                today="2026-08-09")
        self.assertTrue(cleared)   # the 35 closed carry it; pendings are inert


class MonthlyTests(unittest.TestCase):
    def test_closed_trades_group_by_exit_month(self):
        rows = [("closed", "2026-06-15", 0.01, 100.0, 0.0, 0.02),
                ("closed", "2026-06-20", -0.01, -100.0, 0.0, 0.02),
                ("closed", "2026-07-02", 0.02, 200.0, 0.0, 0.02)]
        table = paper.monthly(ledger_frame(rows))
        self.assertEqual(list(table["month"]), ["2026-06", "2026-07"])
        self.assertEqual(int(table.iloc[0]["closed"]), 2)
        self.assertAlmostEqual(table.iloc[0]["total_net"], 0.0)


class LogTests(PaperDirTestCase):
    def _logging(self, survivors):
        """All the derivation mocks a log() call needs, as one context."""
        import contextlib
        exit_stack = contextlib.ExitStack()
        for patcher in (
            mock.patch.object(paper.signal.study_grid, "with_context",
                              return_value="trades"),
            mock.patch.object(paper.signal.study_specials, "load_grid_trades",
                              return_value="logs"),
            mock.patch.object(paper.signal.study_exdate, "nifty_closes",
                              return_value={}),
            mock.patch.object(paper.signal, "surviving_cells",
                              return_value=survivors),
        ):
            exit_stack.enter_context(patcher)
        return exit_stack

    def test_an_empty_run_is_still_a_run_on_the_clock(self):
        with self._logging([]):
            self.assertEqual(paper.log(), 0)
        runs = paper.read_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(int(runs.iloc[0]["candidates_logged"]), 0)
        self.assertFalse(paper.signals_path().exists())

    def test_signals_are_logged_once_however_many_runs_see_them(self):
        survivor = {"cell": (20, 0), "median_return": 0.0095, "p25": 0.0032,
                    "p75": 0.0131, "n": 100, "hit_rate": 0.55,
                    "median_excess": 0.002, "stressed_excess": 0.001}
        calendar = pd.DataFrame({
            "symbol": ["FAR"], "ex_date": [pd.Timestamp("2026-12-01")],
            "record_date": [pd.NaT], "amount": [5.0], "est_yield_pct": [1.0],
            "liquidity": ["high"], "source": ["corporate-actions"]})
        out = self.tmp / "upcoming.parquet"
        calendar.to_parquet(out, index=False)
        with self._logging([survivor]), \
             mock.patch.object(upcoming, "OUT_PATH", out):
            paper.log(today="2026-08-09")
            paper.log(today="2026-08-09")
        ledger = paper.read_signals()
        self.assertEqual(len(ledger), 1)
        row = ledger.iloc[0]
        self.assertEqual(row["status"], paper.STATUS_PENDING)
        self.assertAlmostEqual(row["expected_p25"], 0.0032)   # frozen band
        self.assertEqual(len(paper.read_runs()), 2)


class FillTests(PaperDirTestCase):
    def setUp(self):
        super().setUp()
        self._cache = fetch.CACHE_DIR
        fetch.CACHE_DIR = self.tmp / "cache"
        frame = pd.DataFrame({
            "date": pd.to_datetime(["2026-08-04", "2026-08-05", "2026-08-06",
                                    "2026-08-07"]),
            "open": [100.0] * 4, "high": [100.0] * 4, "low": [100.0] * 4,
            "close": [104.0, 104.0, 101.0, 103.0],
            "adj_close": [104.0, 104.0, 101.0, 103.0],
            "volume": [1000] * 4,
            "dividend": [0.0, 0.0, 2.0, 0.0], "split": [0.0] * 4,
        })
        fetch.write_cache("X", frame)

    def tearDown(self):
        fetch.CACHE_DIR = self._cache
        super().tearDown()

    def _pending(self, exit_after):
        row = ledger_frame([("pending", "2026-06-01", None, None, 0.0, 0.02)])
        row.loc[0, ["symbol", "ex_date", "entry_days_before",
                    "exit_days_after"]] = ["X", pd.Timestamp("2026-08-06"), 1,
                                           exit_after]
        row.loc[0, ["entry_date", "exit_date", "entry_close", "exit_close",
                    "dividend"]] = [pd.NaT, pd.NaT, None, None, None]
        paper.write_signals(row)

    def test_a_settleable_signal_captures_actual_prices_and_settles(self):
        self._pending(exit_after=1)
        self.assertEqual(paper.fill(cfg=NOCOST, notional=10_000), 0)
        row = paper.read_signals().iloc[0]
        self.assertEqual(row["status"], paper.STATUS_CLOSED)
        self.assertEqual(row["entry_close"], 104.0)   # 1 session before ex
        self.assertEqual(row["exit_close"], 103.0)    # 1 session after
        self.assertEqual(row["dividend"], 2.0)        # the ACTUAL payout
        # 96 shares nocost: (103 - 104) x 96 + 192 = +96.
        self.assertAlmostEqual(row["realized_net"], 96.0)

    def test_a_signal_whose_exit_has_not_traded_yet_stays_pending(self):
        self._pending(exit_after=5)
        paper.fill(cfg=NOCOST, notional=10_000)
        self.assertEqual(paper.read_signals().iloc[0]["status"],
                         paper.STATUS_PENDING)


class ReportTests(PaperDirTestCase):
    def test_the_header_carries_the_rule_and_the_gate_status(self):
        with mock.patch("builtins.print") as told:
            self.assertEqual(paper.report(today="2026-08-09"), 0)
        text = "\n".join(str(call) for call in told.call_args_list)
        self.assertIn("RULE — no real money before 3 months", text)
        self.assertIn("NOT CLEARED", text)
        self.assertIn("clock has not started", text)


if __name__ == "__main__":
    unittest.main()
