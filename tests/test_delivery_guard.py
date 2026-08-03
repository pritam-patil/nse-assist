"""One evening report per session, however many times the workflow runs.

    python -m unittest discover -s tests -v

The evening workflow is scheduled three times because GitHub drops scheduled
events — measured 2026-08-03, roughly 25 of 29 poll slots and the sole evening
slot produced nothing at all. Every stage in the chain is idempotent, so retrying
is free.

Your attention is the exception. Three identical reports an hour apart is how the
evening report stops being read, which costs more than the missed run the retries
were insuring against. This guard is the only non-idempotent part of the chain
made idempotent by hand.
"""

import os
import tempfile
import unittest

from src import deliver, universe
from src.db import get_connection, get_state, init_db


class DeliveryGuardTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self.sent = []
        self._real_send = deliver.send_message

        # Honours dry_run the way the real one does — it returns before sending.
        # A mock that records every call would count a dry run as a delivery and
        # make the test below pass for the wrong reason.
        def fake_send(text, dry_run=False, **kw):
            if dry_run:
                return []
            self.sent.append(text)
            return []

        self.fake_send = fake_send
        deliver.send_message = fake_send
        # A FRESH connection per call, not the shared one: deliver.run() closes the
        # connection it opens, and handing it the test's would close that too.
        self._real_conn = deliver.get_connection
        deliver.get_connection = lambda *a, **kw: get_connection(self.path)
        self._session("2026-08-03")

    def _state(self, key):
        conn = get_connection(self.path)
        try:
            return get_state(conn, key)
        finally:
            conn.close()

    def tearDown(self):
        deliver.send_message = self._real_send
        deliver.get_connection = self._real_conn
        self.conn.close()
        os.unlink(self.path)

    def _session(self, day):
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, "
            "volume, source) VALUES (?,?,?,?,?,?,?,?)",
            [(s, day, 100, 100, 100, 100, 1000, "test") for s in universe.UNIVERSE])
        self.conn.commit()

    def test_the_first_run_sends(self):
        deliver.run()
        self.assertEqual(len(self.sent), 1)

    def test_a_retry_for_the_same_session_does_not(self):
        """The whole point: three scheduled slots, one message."""
        deliver.run()
        deliver.run()
        deliver.run()
        self.assertEqual(len(self.sent), 1)

    def test_a_new_session_sends_again(self):
        deliver.run()
        self._session("2026-08-04")
        deliver.run()
        self.assertEqual(len(self.sent), 2)

    def test_force_re_sends(self):
        """For a manual dispatch when you actually want the message again."""
        deliver.run()
        deliver.run(force=True)
        self.assertEqual(len(self.sent), 2)

    def test_a_dry_run_does_not_claim_the_session(self):
        """Otherwise `--dry-run` silently suppresses the real report that follows."""
        deliver.run(dry_run=True)
        self.assertIsNone(self._state(deliver.DELIVERED_KEY))
        deliver.run()
        self.assertEqual(len(self.sent), 1)

    def test_the_guard_keys_on_the_session_not_the_calendar_day(self):
        """A catch-up run on Tuesday reporting Monday's session has reported
        Monday. Keying on today's date would let it re-report on Wednesday."""
        deliver.run()
        self.assertEqual(self._state(deliver.DELIVERED_KEY), "2026-08-03")

    def test_a_failed_send_does_not_mark_the_session_delivered(self):
        """Otherwise one transport failure costs the whole day's report, silently,
        with the retries suppressed by the guard that was meant to protect them."""
        def explode(text, **kw):
            raise RuntimeError("telegram down")

        deliver.send_message = explode
        with self.assertRaises(RuntimeError):
            deliver.run()
        self.assertIsNone(self._state(deliver.DELIVERED_KEY))

        deliver.send_message = self.fake_send
        deliver.run()
        self.assertEqual(len(self.sent), 1)

    def test_an_empty_price_table_does_not_block_delivery_forever(self):
        """With no session, the guard has no key to compare — it must fall through
        to sending rather than treating None as 'already done'."""
        self.conn.execute("DELETE FROM prices")
        self.conn.commit()
        deliver.run()
        deliver.run()
        # No session to key on, so no suppression. Two messages is the honest
        # outcome; silence would hide that the pipeline has no data at all.
        self.assertEqual(len(self.sent), 2)


class EveningScheduleTestCase(unittest.TestCase):
    def test_the_workflow_has_more_than_one_slot(self):
        import pathlib
        import re

        text = (pathlib.Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "evening.yml").read_text()
        crons = re.findall(r'cron: "(\d+) (\d+) \* \* 1-5"', text)
        self.assertGreaterEqual(len(crons), 3, "retries removed — the guard now protects nothing")

    def test_the_retries_are_off_the_hour(self):
        """The top of the hour is when GitHub's scheduling contention is worst, and
        it is where the slot that never fired was."""
        import pathlib
        import re

        text = (pathlib.Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "evening.yml").read_text()
        crons = re.findall(r'cron: "(\d+) (\d+) \* \* 1-5"', text)
        retries = crons[1:]
        for minute, hour in retries:
            with self.subTest(cron=f"{minute} {hour}"):
                self.assertNotEqual(int(minute), 0)

    def test_the_slots_are_spread_rather_than_adjacent(self):
        """A busy half-hour should not be able to swallow all three."""
        import pathlib
        import re

        text = (pathlib.Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "evening.yml").read_text()
        crons = [(int(h) * 60 + int(m))
                 for m, h in re.findall(r'cron: "(\d+) (\d+) \* \* 1-5"', text)]
        gaps = [b - a for a, b in zip(sorted(crons), sorted(crons)[1:])]
        self.assertTrue(all(gap >= 45 for gap in gaps), f"slots too close: {gaps} minutes")


if __name__ == "__main__":
    unittest.main()
