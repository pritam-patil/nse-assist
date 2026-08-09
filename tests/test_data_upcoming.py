"""The forthcoming-dividends prober: parsing, probing, and the fallback order.

    python -m unittest discover -s tests -v

Network never happens here; the probe logic runs against scripted fake
sessions. The empty-body retry is the test that matters — NSE's failure mode
is a 200 that says nothing, and treating that as success would emit an empty
"nothing forthcoming" table that reads exactly like a quiet calendar.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from data import fetch, upcoming


class FakeResponse:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self.text = text if payload is None else "x"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Returns scripted responses in order; records how often it was asked."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


class AmountTests(unittest.TestCase):
    def test_the_subject_line_variants_all_parse(self):
        cases = {
            "Dividend - Rs 14 Per Share": 14.0,
            "Interim Dividend - Rs.5.50 Per Share": 5.5,
            "Dividend - Re 0.50 Per Share": 0.5,
            "Special Dividend - ₹12 Per Share": 12.0,
            "Dividend - Rs 1.06 Per Share": 1.06,
        }
        for text, expected in cases.items():
            self.assertEqual(upcoming.parse_amount(text), expected, text)

    def test_no_rupee_amount_means_none_not_zero(self):
        self.assertIsNone(upcoming.parse_amount("Bonus 1:1"))
        self.assertIsNone(upcoming.parse_amount(None))

    def test_nse_dates_parse_and_garbage_does_not(self):
        self.assertEqual(upcoming.parse_nse_date("10-Aug-2026"),
                         pd.Timestamp("2026-08-10"))
        self.assertIsNone(upcoming.parse_nse_date("-"))
        self.assertIsNone(upcoming.parse_nse_date(None))


class ProbeTests(unittest.TestCase):
    def test_an_empty_body_200_retries_and_can_recover(self):
        session = FakeSession([
            FakeResponse(status=200, text="   "),
            FakeResponse(status=200, payload=[{"symbol": "X"}]),
        ])
        outcome = upcoming.probe(session, "test", "http://x", {}, "http://r")
        self.assertEqual(outcome["rows"], [{"symbol": "X"}])
        self.assertIsNone(outcome["error"])
        self.assertEqual(session.calls, 2)

    def test_two_empty_bodies_are_a_failure_not_a_quiet_calendar(self):
        session = FakeSession([FakeResponse(text=""), FakeResponse(text="")])
        outcome = upcoming.probe(session, "test", "http://x", {}, "http://r")
        self.assertIsNone(outcome["rows"])
        self.assertEqual(outcome["error"], "empty body")

    def test_a_data_wrapped_payload_is_unwrapped(self):
        session = FakeSession([FakeResponse(payload={"data": [{"symbol": "Y"}]})])
        outcome = upcoming.probe(session, "test", "http://x", {}, "http://r")
        self.assertEqual(outcome["rows"], [{"symbol": "Y"}])


class NormalizationTests(unittest.TestCase):
    def test_only_dividend_subjects_become_rows(self):
        rows = upcoming.dividend_actions([
            {"symbol": "A", "subject": "Dividend - Rs 14 Per Share",
             "exDate": "10-Aug-2026", "recDate": "11-Aug-2026"},
            {"symbol": "B", "subject": "Bonus 1:1", "exDate": "12-Aug-2026"},
        ])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "A")
        self.assertEqual(row["amount"], 14.0)
        self.assertEqual(row["ex_date"], pd.Timestamp("2026-08-10"))
        self.assertEqual(row["record_date"], pd.Timestamp("2026-08-11"))
        # Corporate-actions rows carry no seq_id — the seen-ledger falls back.
        self.assertIsNone(row["seq_id"])

    def test_declarations_keep_one_latest_row_per_symbol_with_seq_id(self):
        rows = upcoming.dividend_declarations([
            {"symbol": "A", "desc": "Dividend", "attchmntText": "dividend of Rs 5",
             "an_dt": "08-Aug-2026 10:00:00", "seq_id": "111"},
            {"symbol": "A", "desc": "Record Date", "attchmntText": "dividend Rs 6",
             "an_dt": "09-Aug-2026 12:00:00", "seq_id": "222"},
            {"symbol": "B", "desc": "Board Meeting", "attchmntText": "results"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 6.0)
        self.assertIsNone(rows[0]["ex_date"])
        self.assertEqual(rows[0]["source"], "announcements")
        # The latest filing's seq_id rides along as the seen-ledger key.
        self.assertEqual(rows[0]["seq_id"], "222")

    def test_seq_id_reaches_the_built_table(self):
        rows = [{"symbol": "GOODCO", "ex_date": pd.Timestamp("2026-09-15"),
                 "record_date": pd.NaT, "amount": 5.0, "seq_id": "999",
                 "source": "announcements"}]
        table = upcoming.build_table(rows, ["GOODCO"], {"GOODCO": (100.0, 5e7)},
                                     (1e6, 1e7))
        self.assertIn("seq_id", table.columns)
        self.assertEqual(table.iloc[0]["seq_id"], "999")


class TableTests(unittest.TestCase):
    ROWS = [
        {"symbol": "INUNIVERSE", "ex_date": pd.Timestamp("2026-08-12"),
         "record_date": pd.Timestamp("2026-08-12"), "amount": 5.0,
         "source": "corporate-actions"},
        {"symbol": "OUTSIDER", "ex_date": pd.Timestamp("2026-08-11"),
         "record_date": None, "amount": 2.0, "source": "corporate-actions"},
        {"symbol": "TBASYM", "ex_date": None, "record_date": None,
         "amount": None, "source": "announcements"},
    ]

    def test_the_table_is_universe_filtered_with_yield_and_liquidity(self):
        context = {"INUNIVERSE": (250.0, 5e7), "TBASYM": (100.0, 1e5)}
        table = upcoming.build_table(self.ROWS, ["INUNIVERSE", "TBASYM"],
                                     context, (1e6, 1e7))
        self.assertEqual(list(table["symbol"]), ["INUNIVERSE", "TBASYM"])
        first = table.iloc[0]
        self.assertAlmostEqual(first["est_yield_pct"], 2.0)   # 5 / 250
        self.assertEqual(first["liquidity"], "high")
        second = table.iloc[1]
        self.assertTrue(pd.isna(second["ex_date"]))           # TBA sorts last
        # The DataFrame stores a missing yield as NaN, not None.
        self.assertTrue(pd.isna(second["est_yield_pct"]))
        self.assertEqual(second["liquidity"], "low")

    def test_a_symbol_without_cache_context_is_flagged_unknown(self):
        table = upcoming.build_table(self.ROWS[:1], ["INUNIVERSE"], {}, (1e6, 1e7))
        self.assertEqual(table.iloc[0]["liquidity"], "unknown")
        self.assertTrue(pd.isna(table.iloc[0]["est_yield_pct"]))


class AccessAssertionTests(unittest.TestCase):
    """The runner network gate: check_access and the --assert-access exit code."""

    def test_check_access_returns_the_announcements_outcome(self):
        outcome = {"name": "announcements", "status": 200, "rows": [{"x": 1}],
                   "error": None}
        with mock.patch.object(upcoming, "fetch_announcements",
                               return_value=outcome) as fetch, \
             mock.patch.object(upcoming, "nse_session", return_value="session"):
            self.assertIs(upcoming.check_access(), outcome)
        fetch.assert_called_once_with("session")

    def test_assert_access_exits_zero_when_reachable(self):
        outcome = {"name": "announcements", "status": 200,
                   "rows": [{"x": 1}], "error": None}
        with mock.patch.object(upcoming, "check_access", return_value=outcome):
            self.assertEqual(upcoming.main(["--assert-access"]), 0)

    def test_assert_access_exits_nonzero_when_unreachable(self):
        outcome = {"name": "announcements", "status": 200, "rows": None,
                   "error": "empty body"}
        with mock.patch.object(upcoming, "check_access", return_value=outcome):
            self.assertEqual(upcoming.main(["--assert-access"]), 1)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._out = upcoming.OUT_PATH
        upcoming.OUT_PATH = self.tmp / "upcoming.parquet"

    def tearDown(self):
        upcoming.OUT_PATH = self._out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, actions, announcements):
        with mock.patch.object(upcoming, "nse_session", return_value=None), \
             mock.patch.object(upcoming, "fetch_corporate_actions",
                               return_value=actions), \
             mock.patch.object(upcoming, "fetch_announcements",
                               return_value=announcements), \
             mock.patch.object(upcoming, "cache_context",
                               return_value={"A": (100.0, 5e7)}), \
             mock.patch.object(upcoming.events, "cached_symbols",
                               return_value=["A"]):
            return upcoming.run()

    def test_structured_actions_win_when_available(self):
        actions = {"name": "corporate-actions", "status": 200, "error": None,
                   "rows": [{"symbol": "A", "subject": "Dividend - Rs 4 Per Share",
                             "exDate": "12-Aug-2026", "recDate": "12-Aug-2026"}]}
        announcements = {"name": "announcements", "status": 200, "error": None,
                         "rows": [{"symbol": "A", "desc": "Dividend",
                                   "attchmntText": "dividend of Rs 9",
                                   "an_dt": "09-Aug-2026 10:00:00"}]}
        self.assertEqual(self._run(actions, announcements), 0)
        table = pd.read_parquet(upcoming.OUT_PATH)
        self.assertEqual(table.iloc[0]["source"], "corporate-actions")
        self.assertEqual(table.iloc[0]["amount"], 4.0)

    def test_announcements_carry_the_day_when_actions_fail(self):
        actions = {"name": "corporate-actions", "status": 200,
                   "error": "empty body", "rows": None}
        announcements = {"name": "announcements", "status": 200, "error": None,
                         "rows": [{"symbol": "A", "desc": "Dividend",
                                   "attchmntText": "dividend of Rs 9",
                                   "an_dt": "09-Aug-2026 10:00:00"}]}
        self.assertEqual(self._run(actions, announcements), 0)
        table = pd.read_parquet(upcoming.OUT_PATH)
        self.assertEqual(table.iloc[0]["source"], "announcements")

    def test_both_dead_is_a_loud_failure(self):
        dead = {"name": "x", "status": None, "error": "boom", "rows": None}
        self.assertEqual(self._run(dead, dict(dead, name="y")), 1)


if __name__ == "__main__":
    unittest.main()
