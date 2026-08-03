"""The brief has to say when it was written, because no cron can be relied on.

    python -m unittest discover -s tests -v

Measured 2026-08-03: the morning workflow was scheduled for 08:45 IST and ran at
12:07 — three hours and twenty-two minutes late. That turned a message written to
be read before the open into one read mid-session, describing fills that had
already happened, with nothing in it saying so.

Moving the schedule earlier buys margin. It does not buy a guarantee, and the
message is the only place the guarantee can actually live.
"""

import unittest
from datetime import datetime

from src import brief


class TimingNoteTestCase(unittest.TestCase):
    def test_before_the_open_it_counts_down(self):
        note = brief.timing_note(datetime(2026, 8, 3, 7, 50))
        self.assertIn("07:50 IST", note)
        self.assertIn("85 minutes before the open", note)

    def test_the_countdown_is_correct_close_to_the_bell(self):
        self.assertIn("5 minutes before", brief.timing_note(datetime(2026, 8, 3, 9, 10)))

    def test_after_the_open_it_says_late_in_the_first_word(self):
        """Buried mid-sentence it gets skimmed. This is the one line whose whole
        job is to stop you acting on the numbers underneath it."""
        note = brief.timing_note(datetime(2026, 8, 3, 12, 7))
        self.assertTrue(note.startswith("LATE"))

    def test_a_late_brief_reframes_the_entries_as_history(self):
        note = brief.timing_note(datetime(2026, 8, 3, 12, 7))
        self.assertIn("already happened", note)
        self.assertIn("not as prices you can still get", note)

    def test_exactly_at_the_open_counts_as_late(self):
        """09:15:00 is the bell. A brief arriving with it is not pre-open."""
        self.assertTrue(brief.timing_note(datetime(2026, 8, 3, 9, 15)).startswith("LATE"))

    def test_the_note_is_in_the_message(self):
        from src.db import get_connection, init_db

        conn = get_connection(":memory:")
        init_db(conn)
        try:
            text = brief.build_brief(conn)
            self.assertTrue("Generated" in text or "LATE" in text)
        finally:
            conn.close()

    def test_the_schedule_leaves_room_for_a_delay_that_has_actually_happened(self):
        """The cron is the margin, not the fix. 02:20 UTC is 07:50 IST, 85 minutes
        of slack — a schedule with less than an hour would have been overtaken by
        the delay already measured."""
        import pathlib
        import re

        text = (pathlib.Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "morning.yml").read_text()
        cron = re.search(r'cron: "(\d+) (\d+) \* \* 1-5"', text)
        self.assertIsNotNone(cron, "morning cron not found or reshaped")
        minute, hour = int(cron.group(1)), int(cron.group(2))
        ist_minutes = (hour * 60 + minute) + 330      # UTC -> IST
        slack = (9 * 60 + 15) - ist_minutes
        self.assertGreaterEqual(slack, 60, "less than an hour of slack before the open")
        self.assertNotEqual(minute, 0, "top of the hour is when GitHub delays are worst")


if __name__ == "__main__":
    unittest.main()
