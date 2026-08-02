"""The /whatif command: validated input, honest statistics, no forecast.

    python -m unittest discover -s tests -v

Two properties here are correctness rather than style. The reply must never read
as a prediction — it is a count of past windows, and the moment it stops saying so
it becomes advice with a rupee figure attached. And the authorisation check must
hold: a bot token is a URL anyone can talk to once they know it.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from src import config, fund_watchlist, funds, whatif
from src.db import get_connection, get_state, init_db

CODE = fund_watchlist.SCHEME_CODES[0]
OTHER = fund_watchlist.SCHEME_CODES[1]


class ParseTestCase(unittest.TestCase):
    def test_a_well_formed_command_parses(self):
        self.assertEqual(whatif.parse_command(f"/whatif {CODE} 6 25000"), (CODE, 6, 25000.0))

    def test_the_group_chat_form_parses(self):
        """Telegram appends @botname when the command is typed in a group."""
        self.assertEqual(whatif.parse_command(f"/whatif@nse_bot {CODE} 6 25000")[1], 6)

    def test_commas_and_rupee_signs_in_the_amount(self):
        """What a person actually types on a phone."""
        for raw in ("25,000", "₹25000", "25000.00", "Rs25000"):
            with self.subTest(amount=raw):
                self.assertEqual(whatif.parse_command(f"/whatif {CODE} 6 {raw}")[2], 25000.0)

    def test_wrong_argument_count_is_rejected(self):
        for text in (f"/whatif {CODE}", f"/whatif {CODE} 6", f"/whatif {CODE} 6 25000 extra"):
            with self.subTest(text=text):
                with self.assertRaises(whatif.CommandError):
                    whatif.parse_command(text)

    def test_a_non_numeric_scheme_code_is_rejected(self):
        with self.assertRaises(whatif.CommandError):
            whatif.parse_command("/whatif HDFCLIQUID 6 25000")

    def test_an_unwatched_scheme_is_rejected_rather_than_queried(self):
        """No history is stored for it, so the honest answer names the reason."""
        with self.assertRaises(whatif.CommandError) as caught:
            whatif.parse_command("/whatif 999999 6 25000")
        self.assertIn("watchlist", str(caught.exception))

    def test_weeks_must_be_a_whole_number_in_range(self):
        for raw in ("0", "-3", "1.5", "500", "six"):
            with self.subTest(weeks=raw):
                with self.assertRaises(whatif.CommandError):
                    whatif.parse_command(f"/whatif {CODE} {raw} 25000")

    def test_absurd_amounts_are_rejected(self):
        for raw in ("0", "-100", "99999999999"):
            with self.subTest(amount=raw):
                with self.assertRaises(whatif.CommandError):
                    whatif.parse_command(f"/whatif {CODE} 6 {raw}")

    def test_the_error_message_names_what_to_fix(self):
        """A rejection that only says 'invalid' costs a round trip to guess from."""
        with self.assertRaises(whatif.CommandError) as caught:
            whatif.parse_command(f"/whatif {CODE} 6")
        self.assertIn("3 arguments", str(caught.exception))


class DistributionTestCase(unittest.TestCase):
    def _navs(self, daily=1.0002, days=1200, start=100.0, end_day="2026-08-02"):
        last = date.fromisoformat(end_day)
        out, value = [], start
        for offset in range(days, 0, -1):
            value *= daily
            out.append(((last - timedelta(days=offset - 1)).isoformat(), round(value, 4)))
        return out

    def test_a_steady_riser_never_ends_negative(self):
        stats = whatif.distribution(self._navs(), weeks=6, amount=25000)
        self.assertEqual(stats["negative_share"], 0.0)
        self.assertGreater(stats["worst"], 0)

    def test_rupees_track_the_amount_linearly(self):
        """The rupee figure is the return times the amount and nothing else — no
        compounding of a per-window number that is already a total return."""
        small = whatif.distribution(self._navs(), weeks=6, amount=10000)
        large = whatif.distribution(self._navs(), weeks=6, amount=50000)
        self.assertAlmostEqual(large["median"], small["median"] * 5, places=4)

    def test_best_is_not_below_worst_and_median_sits_between(self):
        stats = whatif.distribution(self._navs(), weeks=8, amount=25000)
        self.assertLessEqual(stats["worst"], stats["median"])
        self.assertLessEqual(stats["median"], stats["best"])

    def test_a_falling_series_reports_every_window_negative(self):
        stats = whatif.distribution(self._navs(daily=0.9995), weeks=6, amount=25000)
        self.assertEqual(stats["negative_share"], 1.0)
        self.assertLess(stats["median"], 0)

    def test_windows_are_drawn_only_from_the_past_three_years(self):
        """A ten-year series must not quietly answer with a decade of windows.

        Compared against a series that holds only the three years, rather than
        against a loose upper bound — an assertion that a decade produces "fewer
        than 1095 windows" would pass with eight years of them in there.
        """
        decade = whatif.distribution(self._navs(days=3650), weeks=6, amount=25000)
        three_years = whatif.distribution(self._navs(days=1096), weeks=6, amount=25000)
        self.assertEqual(decade["windows"], three_years["windows"])

    def test_too_little_history_returns_nothing_rather_than_four_numbers(self):
        self.assertIsNone(whatif.distribution(self._navs(days=40), weeks=6, amount=25000))

    def test_the_longest_window_still_fits_inside_the_three_years(self):
        """MAX_WEEKS is picked so a two-year window leaves a year of start dates.
        If it ever exceeds the lookback the reply must decline, not extend the span
        it claims to describe."""
        self.assertLess(whatif.MAX_WEEKS * 7, whatif.LOOKBACK_DAYS)
        stats = whatif.distribution(self._navs(days=1200), weeks=whatif.MAX_WEEKS, amount=25000)
        self.assertIsNotNone(stats)
        self.assertGreaterEqual(stats["windows"], whatif.MIN_WINDOWS)


class AnswerTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self._store(CODE)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _store(self, code, daily=1.0002, days=1200, start=100.0):
        last = date.fromisoformat("2026-08-02")
        rows, value = [], start
        for offset in range(days, 0, -1):
            value *= daily
            rows.append((code, (last - timedelta(days=offset - 1)).isoformat(), round(value, 4)))
        funds.store_navs(self.conn, rows)

    def _reply(self, text=f"/whatif {CODE} 6 25000"):
        return whatif.answer(self.conn, text, as_of="2026-08-02")

    def test_reports_median_best_worst_and_the_negative_share(self):
        text = self._reply().lower()
        for term in ("median", "best", "worst", "negative"):
            with self.subTest(term=term):
                self.assertIn(term, text)

    def test_states_that_it_is_history_not_a_forecast(self):
        text = self._reply().lower()
        self.assertIn("not a forecast", text)
        self.assertIn("already happened", text)

    def test_states_that_nothing_is_deducted(self):
        """The spec's 'net of nothing' — and NAV already being net of the expense
        ratio is the one exception, so saying 'net of nothing' flatly would be wrong."""
        text = self._reply().lower()
        self.assertIn("exit load", text)
        self.assertIn("tax", text)
        self.assertIn("expense ratio", text)

    def test_names_the_overlapping_window_caveat(self):
        """N overlapping windows is not N independent observations, and the count
        on its own reads as a far larger sample than it is."""
        self.assertIn("overlap", self._reply().lower())

    def test_reports_the_window_count_and_the_span(self):
        text = self._reply()
        self.assertIn("windows", text)
        self.assertIn("2026-08-02", text)

    def test_the_scheme_label_appears_not_just_the_code(self):
        self.assertIn(fund_watchlist.label_for(CODE), self._reply())

    def test_a_malformed_command_answers_with_usage(self):
        text = whatif.answer(self.conn, "/whatif")
        self.assertIn("Usage:", text)
        self.assertIn("/whatif SCHEMECODE WEEKS AMOUNT", text)

    def test_usage_lists_the_watchlist_codes(self):
        text = whatif.answer(self.conn, "/whatif nonsense")
        for code in fund_watchlist.SCHEME_CODES:
            with self.subTest(code=code):
                self.assertIn(code, text)

    def test_a_scheme_with_no_stored_navs_says_so(self):
        text = whatif.answer(self.conn, f"/whatif {OTHER} 6 25000")
        self.assertIn("No NAV history", text)

    def test_thin_history_declines_rather_than_quoting_a_median_of_four(self):
        self._store(OTHER, days=40)
        text = whatif.answer(self.conn, f"/whatif {OTHER} 6 25000", as_of="2026-08-02")
        self.assertIn("Not enough history", text)

    def test_no_advice_language(self):
        text = self._reply().lower()
        for token in ("you should", "recommend", "buy", "sell", "safe bet", "guarantee", "🚀"):
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_no_emoji_codepoints(self):
        for char in self._reply():
            self.assertLess(ord(char), 0x2190, f"{char!r} is a symbol or emoji")


class PollTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self.sent = []
        self._real_send = whatif.deliver.send_reply
        whatif.deliver.send_reply = lambda chat_id, text, **kw: self.sent.append((chat_id, text))
        self._real_chat = config.TELEGRAM_CHAT_ID
        config.TELEGRAM_CHAT_ID = "12345"

    def tearDown(self):
        whatif.deliver.send_reply = self._real_send
        config.TELEGRAM_CHAT_ID = self._real_chat
        self.conn.close()
        os.unlink(self.path)

    def _message(self, text, chat_id="12345"):
        return {"chat": {"id": chat_id}, "message_id": 7, "text": text}

    def test_a_command_from_the_configured_chat_is_answered(self):
        whatif.handle_message(self.conn, self._message("/help"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Usage:", self.sent[0][1])

    def test_a_command_from_any_other_chat_is_ignored(self):
        """A bot token is a URL anyone can talk to once they know it."""
        outcome = whatif.handle_message(self.conn, self._message("/help", chat_id="999"))
        self.assertEqual(self.sent, [])
        self.assertIn("unauthorised", outcome)

    def test_the_reply_does_not_confirm_the_bot_to_an_unauthorised_chat(self):
        whatif.handle_message(self.conn, self._message("/whatif 1 1 1", chat_id="999"))
        self.assertEqual(self.sent, [])

    def test_plain_chat_is_not_treated_as_a_command(self):
        self.assertIsNone(whatif.handle_message(self.conn, self._message("morning")))
        self.assertEqual(self.sent, [])

    def test_an_unknown_command_gets_usage_rather_than_silence(self):
        """From the phone end a typo and a dead bot look identical."""
        whatif.handle_message(self.conn, self._message("/whatiff 119091 6 25000"))
        self.assertIn("Usage:", self.sent[0][1])

    def test_the_offset_survives_in_the_database(self):
        from src.db import set_state

        set_state(self.conn, whatif.OFFSET_KEY, 42)
        self.conn.commit()
        self.assertEqual(get_state(self.conn, whatif.OFFSET_KEY), "42")
        self.assertEqual(whatif._load_offset(self.conn), 42)

    def test_a_missing_offset_reads_as_none_not_zero(self):
        """offset=0 is a valid Telegram value meaning 'confirm nothing'; None means
        'send me everything pending', which is what a first run needs."""
        self.assertIsNone(whatif._load_offset(self.conn))

    def test_a_corrupt_offset_does_not_crash_the_run(self):
        from src.db import set_state

        set_state(self.conn, whatif.OFFSET_KEY, "not-a-number")
        self.conn.commit()
        self.assertIsNone(whatif._load_offset(self.conn))

    def test_run_advances_the_offset_past_every_update_seen(self):
        updates = [
            {"update_id": 100, "message": self._message("/help")},
            {"update_id": 101, "message": self._message("morning")},
        ]
        real_get = whatif.deliver.get_updates
        whatif.deliver.get_updates = lambda offset=None, timeout=0: updates
        real_connection = whatif.get_connection
        whatif.get_connection = lambda *a, **kw: self.conn
        try:
            whatif.run()
        finally:
            whatif.deliver.get_updates = real_get
            whatif.get_connection = real_connection
        # The connection is closed by run(); reopen to read the persisted offset.
        conn = get_connection(self.path)
        try:
            self.assertEqual(get_state(conn, whatif.OFFSET_KEY), "102")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
