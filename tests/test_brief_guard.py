"""One morning brief per signal date, however many scheduled slots fire.

    python -m unittest discover -s tests -v

morning.yml is about to get the same multi-slot treatment evening.yml already
has, for the same reason — GitHub's `schedule` trigger is unreliable at any
single time. Without this guard, every slot that fires the same morning would
resend the identical brief; deliver.py's already_delivered()/mark_delivered()
already solved exactly this for the evening report, generalized here with an
explicit `key` so the two guards keep independent state rather than one
silently satisfying the other.
"""

import os
import tempfile
import unittest

from src import brief, deliver
from src.db import get_connection, get_state, init_db


class BriefGuardTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self.sent = []
        self._real_send = deliver.send_message

        def fake_send(text, dry_run=False, **kw):
            if dry_run:
                return []
            self.sent.append(text)
            return []

        deliver.send_message = fake_send
        # brief.run() opens and closes its own connection via src.db's module-level
        # get_connection; patch brief's reference to it, not deliver's, and hand
        # back a fresh connection each call the way the real one behaves.
        self._real_conn = brief.get_connection
        brief.get_connection = lambda *a, **kw: get_connection(self.path)
        self._signal("2026-08-03")

    def tearDown(self):
        deliver.send_message = self._real_send
        brief.get_connection = self._real_conn
        self.conn.close()
        os.unlink(self.path)

    def _state(self, key):
        conn = get_connection(self.path)
        try:
            return get_state(conn, key)
        finally:
            conn.close()

    def _signal(self, day, symbol="TESTCO"):
        self.conn.execute(
            "INSERT INTO signals (date, symbol, rule, direction, status) "
            "VALUES (?,?,?,?,?)", (day, symbol, "test_rule", "long", "proposed"))
        self.conn.commit()

    def test_the_first_run_sends(self):
        brief.run()
        self.assertEqual(len(self.sent), 1)

    def test_a_retry_for_the_same_signal_date_does_not(self):
        """The whole point: several scheduled slots, one message."""
        brief.run()
        brief.run()
        brief.run()
        self.assertEqual(len(self.sent), 1)

    def test_a_new_signal_date_sends_again(self):
        brief.run()
        self._signal("2026-08-04")
        brief.run()
        self.assertEqual(len(self.sent), 2)

    def test_force_re_sends(self):
        """For a manual dispatch when you actually want the message again."""
        brief.run()
        brief.run(force=True)
        self.assertEqual(len(self.sent), 2)

    def test_a_dry_run_never_marks_delivered(self):
        brief.run(dry_run=True)
        self.assertIsNone(self._state(brief.BRIEF_DELIVERED_KEY))
        brief.run()
        self.assertEqual(len(self.sent), 1)

    def test_no_signals_at_all_never_blocks_a_resend(self):
        """A None signal_date must not be treated as a session already covered
        — the same edge case deliver.py's guard already has, inherited here
        rather than reintroduced differently."""
        empty_path_handle, empty_path = tempfile.mkstemp(suffix=".db")
        os.close(empty_path_handle)
        try:
            get_connection(empty_path).close()
            init_db(get_connection(empty_path))
            brief.get_connection = lambda *a, **kw: get_connection(empty_path)
            brief.run()
            brief.run()
            self.assertEqual(len(self.sent), 2)
        finally:
            os.unlink(empty_path)

    def test_the_brief_guard_and_the_evening_guard_do_not_share_state(self):
        """Two independent messages, two independent keys — the reason
        already_delivered()/mark_delivered() were generalized rather than
        reused with the evening report's own key."""
        deliver.mark_delivered(self.conn, "2026-08-03")   # evening's own key
        brief.run()
        self.assertEqual(len(self.sent), 1)   # the brief guard did not see it


if __name__ == "__main__":
    unittest.main()
