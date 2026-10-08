"""A signal is dated by the bar it was computed from, never by the wall clock.

    python -m unittest discover -s tests -v

The runner's date is UTC and the evening slots land hours late. On 2026-08-28 a
Thursday scan landed after 00:00 UTC, was stamped Friday, and re-proposed
Thursday's three candidates under a second date (signals 81-83) — duplicates the
(date, symbol, rule) guard could not see, because the date was the thing that
changed.
"""

import os
import tempfile
import unittest
from unittest import mock

from src import signals
from src.db import get_connection, init_db

THURSDAY, FRIDAY = "2026-08-27", "2026-08-28"


def _candidate(symbol, day):
    return {"symbol": symbol, "rule": "momentum_continuation", "direction": "long",
            "date": day, "entry": 100.0, "stop": 95.0, "target": 110.0, "size": 10,
            "risk": 50.0}


def _proposal(*candidates):
    return {"candidates": list(candidates), "dropped": [], "target_potential": 0.0,
            "risk": 0.0, "deployed": 0.0, "fired": len(candidates), "excluded": [],
            "unsizeable": [], "evaluated": len(candidates)}


class SignalDatingTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        conn = get_connection(self.path)
        init_db(conn)
        conn.execute(
            "INSERT INTO prices (symbol, date, open, high, low, close, volume, source) "
            "VALUES ('RELIANCE', ?, 100, 100, 100, 100, 1000, 'test')", (THURSDAY,))
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.path)

    def _run(self, proposal):
        with mock.patch.object(signals, "get_connection",
                               side_effect=lambda: get_connection(self.path)), \
             mock.patch.object(signals, "propose", return_value=proposal), \
             mock.patch.object(signals, "today", return_value=FRIDAY):
            signals.run()

    def _dates(self):
        conn = get_connection(self.path)
        try:
            return [r[0] for r in conn.execute("SELECT date FROM signals ORDER BY id")]
        finally:
            conn.close()

    def test_a_scan_landing_after_utc_midnight_keeps_the_bars_date(self):
        self._run(_proposal(_candidate("RELIANCE", THURSDAY)))
        self.assertEqual(self._dates(), [THURSDAY])

    def test_a_rerun_across_midnight_does_not_duplicate(self):
        proposal = _proposal(_candidate("RELIANCE", THURSDAY))
        self._run(proposal)
        self._run(proposal)
        self.assertEqual(self._dates(), [THURSDAY])

    def test_without_a_bar_date_the_newest_stored_session_is_used(self):
        self._run(_proposal(_candidate("RELIANCE", None)))
        self.assertEqual(self._dates(), [THURSDAY])


if __name__ == "__main__":
    unittest.main()
