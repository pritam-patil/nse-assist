"""Notifications: eligibility legs, the caps, backoff, and the one-write rule.

    python -m unittest discover -s tests -v

Two tests carry the safety weight: the alert path must write to the paper
ledger BEFORE it touches Telegram (one write, two readers — they cannot
diverge), and every eligibility leg must be able to disqualify a row on its
own, with "unknown" liquidity failing closed.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from data import notify, paper, upcoming

SURVIVOR = {"cell": (20, 0), "n": 2158, "median_return": 0.0095,
            "p25": 0.0032, "p75": 0.0131, "hit_rate": 0.56,
            "median_excess": 0.002, "stressed_excess": 0.0005}


def calendar_row(symbol="GOODCO", ex_date="2026-09-15", est_yield=1.5,
                 liquidity="high", amount=5.0):
    return {"symbol": symbol,
            "ex_date": pd.Timestamp(ex_date) if ex_date else pd.NaT,
            "record_date": pd.NaT, "amount": amount,
            "est_yield_pct": est_yield, "liquidity": liquidity,
            "source": "corporate-actions"}


def calendar_table(rows):
    return pd.DataFrame(rows)


class EligibilityTests(unittest.TestCase):
    UNIVERSE = {"GOODCO"}

    def _check(self, row, survivors=(SURVIVOR,)):
        return notify.eligibility(row, self.UNIVERSE, list(survivors))

    def test_a_row_meeting_every_leg_is_eligible(self):
        ok, reasons = self._check(calendar_row())
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_each_leg_disqualifies_alone(self):
        cases = {
            "outside backtested universe": calendar_row(symbol="STRANGER"),
            "bottom liquidity tercile": calendar_row(liquidity="low"),
            "below the surviving": calendar_row(est_yield=0.5),
            "special (yield rule)": calendar_row(est_yield=6.0),
        }
        for expected_reason, row in cases.items():
            ok, reasons = self._check(row)
            self.assertFalse(ok, expected_reason)
            self.assertTrue(any(expected_reason in r for r in reasons),
                            f"{expected_reason} not in {reasons}")

    def test_unknown_liquidity_fails_closed(self):
        ok, reasons = self._check(calendar_row(liquidity="unknown"))
        self.assertFalse(ok)
        self.assertTrue(any("unverifiable" in r for r in reasons))

    def test_an_empty_model_scope_disqualifies_everything(self):
        ok, reasons = self._check(calendar_row(), survivors=())
        self.assertFalse(ok)
        self.assertTrue(any("model scope empty" in r for r in reasons))


class KeyTests(unittest.TestCase):
    def test_seq_id_is_the_key_when_present(self):
        row = {**calendar_row(), "seq_id": "12345"}
        self.assertEqual(notify._key(row), "seq:12345")

    def test_symbol_ex_date_is_the_fallback_key(self):
        row = {**calendar_row(symbol="ACME", ex_date="2026-09-15"),
               "seq_id": None}
        self.assertEqual(notify._key(row), "ACME|2026-09-15")

    def test_a_nan_seq_id_falls_back(self):
        row = {**calendar_row(symbol="ACME", ex_date="2026-09-15"),
               "seq_id": float("nan")}
        self.assertEqual(notify._key(row), "ACME|2026-09-15")


class SurvivorDegradationTests(unittest.TestCase):
    def test_a_missing_grid_degrades_to_empty_not_a_crash(self):
        # A runner has no grid; loading it raises. The fail-safe answer is [],
        # never an exception and never a fabricated survivor.
        with mock.patch.object(notify.signal.study_specials, "load_grid_trades",
                               side_effect=RuntimeError("no stored grid")):
            self.assertEqual(notify._survivors(), [])


class UniverseTests(unittest.TestCase):
    def test_the_cache_is_used_when_present(self):
        with mock.patch.object(notify.events, "cached_symbols",
                               return_value=["AAA", "BBB"]):
            self.assertEqual(notify._backtested_universe(), {"AAA", "BBB"})

    def test_it_falls_back_to_the_committed_constituent_list(self):
        # No cache on a runner — the committed NIFTY 500 list stands in, so a
        # real member is never mislabeled out-of-universe.
        with mock.patch.object(notify.events, "cached_symbols", return_value=[]):
            universe = notify._backtested_universe()
        self.assertGreater(len(universe), 400)
        self.assertIn("HINDPETRO", universe)


class UrgencyTests(unittest.TestCase):
    def test_a_closing_entry_window_is_urgent(self):
        # e=20 needs 28 calendar days: last entry for a 2026-09-08 ex-date is
        # 2026-08-11 — two days from the 9th, inside the window.
        row = calendar_row(ex_date="2026-09-08")
        self.assertTrue(notify.urgent(row, 20, "2026-08-09"))

    def test_a_distant_ex_date_is_not_urgent_yet(self):
        row = calendar_row(ex_date="2026-11-20")
        self.assertFalse(notify.urgent(row, 20, "2026-08-09"))

    def test_a_missed_window_is_not_urgent_either(self):
        row = calendar_row(ex_date="2026-08-20")   # last entry already past
        self.assertFalse(notify.urgent(row, 20, "2026-08-09"))

    def test_tba_cannot_be_urgent(self):
        self.assertFalse(notify.urgent(calendar_row(ex_date=None), 20,
                                       "2026-08-09"))


class SendTests(unittest.TestCase):
    def _creds(self):
        return mock.patch.object(notify, "credentials",
                                 return_value=("token", "chat"))

    def test_backoff_retries_then_gives_up(self):
        bad = mock.Mock(ok=False, status_code=500)
        with self._creds(), \
             mock.patch.object(notify.requests, "post", return_value=bad) as post, \
             mock.patch.object(notify.time, "sleep") as slept:
            self.assertFalse(notify.send("hello"))
        self.assertEqual(post.call_count, notify.MAX_SEND_ATTEMPTS)
        self.assertEqual([call.args[0] for call in slept.call_args_list],
                         [1.0, 2.0, 4.0, 8.0])

    def test_a_mid_sequence_success_stops_the_retries(self):
        responses = [mock.Mock(ok=False, status_code=502),
                     mock.Mock(ok=True, status_code=200)]
        with self._creds(), \
             mock.patch.object(notify.requests, "post", side_effect=responses), \
             mock.patch.object(notify.time, "sleep"):
            self.assertTrue(notify.send("hello"))

    def test_dry_run_never_touches_the_network(self):
        with mock.patch.object(notify.requests, "post") as post:
            self.assertTrue(notify.send("hello", dry_run=True))
        post.assert_not_called()

    def test_missing_credentials_is_a_clear_no(self):
        with mock.patch.object(notify, "credentials", return_value=(None, None)):
            self.assertFalse(notify.send("hello"))


class DigestTests(unittest.TestCase):
    def test_an_empty_model_scope_is_announced_in_those_words(self):
        rows = [calendar_row(), calendar_row(symbol="BIGYIELD", est_yield=4.4)]
        text = notify.build_digest(rows, {"GOODCO", "BIGYIELD"}, [], 100_000,
                                   "2026-08-09")
        self.assertIn("MODEL SCOPE IS EMPTY", text)
        self.assertIn("Nothing below is a signal", text)
        self.assertIn("FYI, OUTSIDE MODEL SCOPE", text)
        self.assertIn("BIGYIELD", text)
        self.assertIn("Not advice", text)

    def test_eligible_rows_rank_with_expectation_and_dispersion(self):
        rows = [calendar_row(), calendar_row(symbol="RICHER", est_yield=2.5)]
        text = notify.build_digest(rows, {"GOODCO", "RICHER"}, [SURVIVOR],
                                   100_000, "2026-08-09")
        self.assertIn("MODEL-ELIGIBLE (2)", text)
        self.assertLess(text.index("RICHER"), text.index("GOODCO"))
        self.assertIn("expected net ~+950", text)
        self.assertIn("+0.32% and +1.31%", text)

    def test_the_fyi_section_is_capped(self):
        rows = [calendar_row(symbol=f"N{i}", est_yield=3.0 + i / 10)
                for i in range(8)]
        text = notify.build_digest(rows, set(), [], 100_000, "2026-08-09")
        self.assertIn(f"({notify.FYI_CAP} of 8)", text)


class StatefulTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._state, self._paper = notify.STATE_PATH, paper.PAPER_DIR
        notify.STATE_PATH = self.tmp / "state.json"
        paper.PAPER_DIR = self.tmp / "paper"
        self.out = self.tmp / "upcoming.parquet"

    def tearDown(self):
        notify.STATE_PATH = self._state
        paper.PAPER_DIR = self._paper
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, survivors, table, sent):
        import contextlib
        table.to_parquet(self.out, index=False)
        stack = contextlib.ExitStack()
        for patcher in (
            mock.patch.object(notify, "_survivors", return_value=survivors),
            mock.patch.object(notify, "_notional", return_value=100_000),
            mock.patch.object(notify.events, "cached_symbols",
                              return_value=["GOODCO", "URGENT1", "URGENT2"]),
            mock.patch.object(upcoming, "OUT_PATH", self.out),
            mock.patch.object(notify, "send", side_effect=sent),
        ):
            stack.enter_context(patcher)
        return stack


class DigestRunTests(StatefulTestCase):
    def test_one_digest_per_day_and_seen_rows_stay_seen(self):
        sent = []
        table = calendar_table([calendar_row()])
        with self._env([], table, lambda text, dry_run=False: sent.append(text) or True):
            self.assertEqual(notify.digest(today="2026-08-09"), 0)
            self.assertEqual(notify.digest(today="2026-08-09"), 0)   # same day
            self.assertEqual(notify.digest(today="2026-08-10"), 0)   # nothing new
        self.assertEqual(len(sent), 1)


class AlertTests(StatefulTestCase):
    def _urgent_table(self, count):
        return calendar_table([
            calendar_row(symbol=f"URGENT{i}", ex_date="2026-09-08",
                         est_yield=2.0 + i / 10)
            for i in range(1, count + 1)])

    def test_paper_is_written_before_telegram(self):
        order = []
        table = self._urgent_table(1)
        with self._env([SURVIVOR], table,
                       lambda text, dry_run=False: order.append("send") or True), \
             mock.patch.object(paper, "record",
                               side_effect=lambda *a, **k: order.append("record") or True):
            self.assertEqual(notify.alerts(today="2026-08-09"), 0)
        self.assertEqual(order, ["record", "send"])

    def test_the_daily_cap_holds_across_runs(self):
        sent = []
        table = self._urgent_table(2)
        with self._env([SURVIVOR], table,
                       lambda text, dry_run=False: sent.append(text) or True):
            notify.alerts(today="2026-08-09")
            # A second run the same day: cap is 5, two sent, the same two are
            # already in paper — nothing further goes out.
            notify.alerts(today="2026-08-09")
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(paper.read_signals()), 2)

    def test_alerts_carry_the_paper_label_and_dispersion(self):
        sent = []
        with self._env([SURVIVOR], self._urgent_table(1),
                       lambda text, dry_run=False: sent.append(text) or True):
            notify.alerts(today="2026-08-09")
        self.assertIn("SIGNAL ALERT (paper)", sent[0])
        self.assertIn("+0.32% to +1.31%", sent[0])
        self.assertIn("Logged to paper", sent[0])

    def test_no_survivors_means_no_alert_machinery_at_all(self):
        with self._env([], self._urgent_table(3), lambda *a, **k: True):
            self.assertEqual(notify.alerts(today="2026-08-09"), 0)
        self.assertFalse(paper.signals_path().exists())

    def test_an_already_seen_filing_is_not_re_alerted(self):
        # The seen-ledger gate: the second daily run finds run 1's filing seen
        # and surfaces nothing, so the runs never duplicate an alert.
        sent = []
        table = self._urgent_table(1)
        row_key = notify._key(table.iloc[0])
        notify.write_state({"last_digest": None, "seen": [row_key], "alerts": {}})
        with self._env([SURVIVOR], table,
                       lambda text, dry_run=False: sent.append(text) or True):
            self.assertEqual(notify.alerts(today="2026-08-09"), 0)
        self.assertEqual(sent, [])
        self.assertFalse(paper.signals_path().exists())

    def test_a_sent_alert_is_marked_seen_for_the_next_run(self):
        sent = []
        table = self._urgent_table(1)
        with self._env([SURVIVOR], table,
                       lambda text, dry_run=False: sent.append(text) or True):
            notify.alerts(today="2026-08-09")
        self.assertEqual(len(sent), 1)
        self.assertIn(notify._key(table.iloc[0]),
                      set(notify.read_state().get("seen", [])))


if __name__ == "__main__":
    unittest.main()
