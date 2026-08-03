"""Every scheduled message opens with the same shaped title.

    python -m unittest discover -s tests -v

Four reports arrive in one Telegram thread, some twice a day. Until these titles
they opened with four different terse variants and a bare ISO date, which made a
week's scrollback hard to tell apart and gave no clue whether a report had arrived
when it was supposed to.

The clock time is the load-bearing part. A morning brief stamped 12:07 IST is a
different document from one stamped 07:50, and only the header says which you are
holding.
"""

import unittest
from datetime import datetime

from src import brief, deliver, message, weekly
from src.db import get_connection, init_db


class TitleTestCase(unittest.TestCase):
    def test_the_kind_comes_first(self):
        """It is what you scan for. The date is context, not identity."""
        first = message.title(message.WEEKLY, datetime(2026, 8, 3, 19, 30)).splitlines()[0]
        self.assertTrue(first.startswith("NSE-ASSIST · "))
        self.assertIn("WEEKLY REVIEW", first)

    def test_the_date_is_spelled_out_without_a_leading_zero(self):
        """'03 August' reads like a form field. strftime %-d is a GNU extension and
        not portable, so the date is built by hand."""
        second = message.title(message.EVENING, datetime(2026, 8, 3, 19, 30)).splitlines()[1]
        self.assertIn("Monday 3 August 2026", second)
        self.assertNotIn("03 August", second)

    def test_the_ist_clock_time_is_present(self):
        """The thing that says whether a report arrived when it should have."""
        self.assertIn("19:30 IST",
                      message.title(message.EVENING, datetime(2026, 8, 3, 19, 30)))

    def test_a_single_digit_hour_keeps_its_zero(self):
        """07:50 is a time; 7:50 is a typo you have to read twice."""
        self.assertIn("07:50 IST",
                      message.title(message.MORNING, datetime(2026, 8, 3, 7, 50)))

    def test_december_and_january_are_named_correctly(self):
        """Hand-built month names are exactly where an off-by-one hides."""
        self.assertIn("1 January 2027",
                      message.title(message.GATE, datetime(2027, 1, 1, 9, 0)))
        self.assertIn("31 December 2026",
                      message.title(message.GATE, datetime(2026, 12, 31, 9, 0)))

    def test_every_weekday_name_is_right(self):
        for day, name in ((3, "Monday"), (4, "Tuesday"), (8, "Saturday"), (9, "Sunday")):
            with self.subTest(day=day):
                self.assertIn(name, message.title(message.WEEKLY, datetime(2026, 8, day, 9, 0)))

    def test_no_emoji_or_shouting_punctuation(self):
        """The tone rules that apply to the body apply to the header: it identifies,
        it does not sell."""
        text = message.title(message.EVENING, datetime(2026, 8, 3, 19, 30))
        self.assertNotIn("!", text)
        for char in text:
            self.assertLess(ord(char), 0x2190, f"{char!r} is a symbol or emoji")

    def test_the_kinds_are_distinct(self):
        kinds = (message.EVENING, message.MORNING, message.WEEKLY,
                 message.GATE, message.FUNDS)
        self.assertEqual(len(set(kinds)), len(kinds))


class MessagesCarryTheirTitleTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_brief_is_titled(self):
        self.assertIn("MORNING BRIEF", brief.build_brief(self.conn))

    def test_the_evening_report_is_titled_and_escaped(self):
        """HTML parse mode: the header goes through html.escape like everything
        else in that message."""
        text = deliver.build_report(self.conn, "2026-08-03")
        self.assertIn("EVENING REPORT", text)
        self.assertIn("<b>", text.splitlines()[0])

    def test_the_weekly_is_titled(self):
        self.assertIn("WEEKLY REVIEW", weekly.build_weekly(self.conn, "2026-08-03"))

    def test_the_weekly_fits_one_telegram_message(self):
        """It split into two before the prose was trimmed, which pushed the closing
        disclaimers into a second message a reader might not open.

        WEAK ON ITS OWN — an empty database builds a much shorter weekly than the
        real one (1,962 chars against 3,725 measured 2026-08-03), so passing here
        does not prove the production message fits. The prose-budget test below is
        the one with teeth, because the fixed trailer is what grew and it is the
        same length whatever the data.
        """
        text = weekly.build_weekly(self.conn, "2026-08-03")
        self.assertLessEqual(len(deliver.split_message(text)), 1,
                             f"weekly is {len(text)} chars, limit is "
                             f"{deliver.TELEGRAM_MAX_MESSAGE_CHARS}")

    def test_the_digests_fixed_prose_stays_within_budget(self):
        """The data-independent half of the length problem.

        Per-scheme lines scale with the watchlist and are the numbers you asked
        for; the trailer is explanation, and explanation is what pushed the weekly
        past 4,096. 900 chars is the current 758 plus room for one more caveat —
        past that, trim rather than raise it.
        """
        from src import fund_digest

        digest = fund_digest.build_digest(self.conn, [])
        _, _, trailer = digest.partition("Composite weights")
        self.assertLess(len(trailer), 900, "digest prose grew — trim it, do not raise this")

    def test_the_pipeline_banner_appears_once_not_twice(self):
        """The weekly header carries it and the embedded gate used to repeat it."""
        text = weekly.build_weekly(self.conn, "2026-08-03")
        self.assertLessEqual(text.count("PIPELINE TEST ACTIVE"), 1)


if __name__ == "__main__":
    unittest.main()
