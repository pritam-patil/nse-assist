"""Freshness, coverage, and the degradation each report is supposed to survive.

    python -m unittest discover -s tests -v

The property under test throughout: a degraded run still produces a message, and
the message says what is wrong with it. Both halves matter and they fail in
opposite directions — a pipeline that stops on the first missing symbol is useless,
and one that computes silently on last week's bars is worse than useless, because
its output is indistinguishable from a correct one.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from src import brief, deliver, funds, health, universe, weekly
from src.db import get_connection, init_db

# A Thursday inside the calendar's coverage, after the bhavcopy publication hour.
EVENING = datetime(2026, 7, 30, 19, 30)
MORNING = datetime(2026, 7, 30, 8, 45)


class FreshnessTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _session(self, day, symbols=None, close=100.0):
        rows = [(s, day, close, close, close, close, 1000, "test")
                for s in (symbols or universe.UNIVERSE)]
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, "
            "volume, source) VALUES (?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def _full_week(self, days=("2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30")):
        for day in days:
            self._session(day)

    # --- the expectation itself ---

    def test_the_expected_session_is_today_after_the_bhavcopy_hour(self):
        self.assertEqual(health.expected_session(EVENING), "2026-07-30")

    def test_before_the_bhavcopy_hour_the_expectation_is_yesterday(self):
        """A morning run must not call itself stale for lacking a file the exchange
        has not published yet."""
        self.assertEqual(health.expected_session(MORNING), "2026-07-29")

    def test_a_weekend_expects_the_friday(self):
        sunday = datetime(2026, 8, 2, 19, 30)
        self.assertEqual(health.expected_session(sunday), "2026-07-31")

    # --- staleness ---

    def test_current_data_produces_no_staleness_note(self):
        self._full_week()
        self.assertIsNone(health.staleness_note(self.conn, now=EVENING))

    def test_a_missed_session_is_named_with_the_last_good_date(self):
        """The exact requirement: the date computed on, not a vague warning."""
        self._full_week(days=("2026-07-27", "2026-07-28"))
        note = health.staleness_note(self.conn, now=EVENING)
        self.assertIn("2026-07-28", note)
        self.assertIn("behind", note)

    def test_the_note_counts_sessions_not_calendar_days(self):
        """Friday to Monday is one session behind, not three days."""
        self._full_week(days=("2026-07-27", "2026-07-28", "2026-07-29"))
        status = health.price_status(self.conn, now=EVENING)
        self.assertEqual(status["behind"], 1)

    def test_an_empty_price_table_says_nothing_is_computed_from_bars(self):
        note = health.staleness_note(self.conn, now=EVENING)
        self.assertIn("No usable price data", note)

    # --- coverage ---

    def test_a_thin_session_is_not_treated_as_the_latest_good_date(self):
        """Eleven symbols is not a session. Computing a scan on it would produce
        signals for whichever names happened to be in the file."""
        self._full_week(days=("2026-07-29",))
        self._session("2026-07-30", symbols=universe.UNIVERSE[:11])
        usable, _ = health.data_through(self.conn)
        self.assertEqual(usable, "2026-07-29")

    def test_a_nearly_complete_session_is_still_usable(self):
        """Partial ingest computes on what arrived — the floor is 90%, not 100%."""
        self._full_week(days=("2026-07-29",))
        self._session("2026-07-30", symbols=universe.UNIVERSE[:97])
        usable, _ = health.data_through(self.conn)
        self.assertEqual(usable, "2026-07-30")

    def test_missing_symbols_are_listed_by_name(self):
        self._full_week(days=("2026-07-29",))
        self._session("2026-07-30", symbols=universe.UNIVERSE[:97])
        absent = health.missing_symbols(self.conn, "2026-07-30")
        self.assertEqual(sorted(absent), sorted(universe.UNIVERSE[97:]))

    def test_the_coverage_note_names_them_and_caps_the_list(self):
        """A footer listing forty tickers is one nobody reads to the end of."""
        self._full_week(days=("2026-07-29",))
        self._session("2026-07-30", symbols=universe.UNIVERSE[:60])
        note = health.coverage_note(self.conn, "2026-07-30")
        self.assertIn("not scanned", note)
        self.assertIn("more", note)
        self.assertLessEqual(note.count(","), health.MAX_NAMED_SYMBOLS + 1)

    def test_a_symbol_that_had_not_listed_yet_is_not_missing(self):
        """Otherwise every session before a new listing reports it absent."""
        self._session("2026-07-29", symbols=universe.UNIVERSE[:50])
        self._session("2026-07-30", symbols=universe.UNIVERSE[:50])
        self.assertEqual(health.missing_symbols(self.conn, "2026-07-29"), [])

    # --- signals ---

    def test_no_signals_is_not_stale_while_every_rule_is_disabled(self):
        """Reporting a decision as a fault. Nothing can fire, so nothing missing.

        Forces the disabled state rather than reading it: the whole point is the
        branch taken when nothing is enabled, and that branch stops being exercised
        the moment a rule is turned on for any reason.
        """
        from src import signals

        saved = signals.ENABLED_RULES
        signals.ENABLED_RULES = ()
        try:
            status = health.signal_status(self.conn, now=EVENING)
            self.assertFalse(status["stale"])
            self.assertIn("disabled", status["detail"])
        finally:
            signals.ENABLED_RULES = saved

    def test_missing_signals_are_stale_once_a_rule_can_fire(self):
        """The other half: with a rule enabled, an empty signals table IS a fault."""
        from src import signals

        saved = signals.ENABLED_RULES
        signals.ENABLED_RULES = ("momentum_continuation",)
        try:
            status = health.signal_status(self.conn, now=EVENING)
            self.assertIsNone(status["latest"])
            self.assertIn("scan has not run", status["detail"])
        finally:
            signals.ENABLED_RULES = saved


class FooterTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _session(self, day, symbols=None):
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, "
            "volume, source) VALUES (?,?,?,?,?,?,?,?)",
            [(s, day, 100, 100, 100, 100, 1000, "test") for s in (symbols or universe.UNIVERSE)])
        self.conn.commit()

    def test_current_data_states_the_date_it_is_current_through(self):
        """Not silence. A footer that only appears when something is wrong is one
        whose absence you learn to read as 'fine', including when it is broken."""
        self._session("2026-07-30")
        footer = health.footer(self.conn, now=EVENING)
        self.assertIn("2026-07-30", footer)
        self.assertIn("current", footer)

    def test_stale_data_replaces_the_current_line_rather_than_joining_it(self):
        self._session("2026-07-27")
        footer = health.footer(self.conn, now=EVENING)
        self.assertIn("Based on data through 2026-07-27", footer)
        self.assertNotIn("Data current through", footer)


class BriefDegradationTestCase(unittest.TestCase):
    """Ingest failed for a date. The brief must still go out, and must say so."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, "
            "volume, source) VALUES (?,?,?,?,?,?,?,?)",
            [(s, "2026-07-27", 100, 100, 100, 100, 1000, "test") for s in universe.UNIVERSE])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_the_brief_still_produces_a_message(self):
        self.assertTrue(brief.build_brief(self.conn))

    def test_the_message_labels_the_data_it_used(self):
        text = brief.build_brief(self.conn)
        self.assertIn("DATA IS BEHIND", text)
        self.assertIn("2026-07-27", text)

    def test_the_staleness_sentence_appears_exactly_once(self):
        """It is under the header AND the footer would carry it. Saying it twice
        reads as boilerplate, which is the one fate this line must avoid."""
        text = brief.build_brief(self.conn)
        self.assertEqual(text.count("Based on data through"), 1)

    def test_the_staleness_appears_above_the_disclaimer_not_only_in_it(self):
        """Buried at the bottom it is decoration. The reader has to meet it before
        the numbers, not after."""
        text = brief.build_brief(self.conn)
        self.assertLess(text.index("DATA IS BEHIND"), text.index("Not investment advice"))


class FundHistoryDegradationTestCase(unittest.TestCase):
    """mfapi.in down. Long-window metrics are skipped and the reason is named."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _navs(self, code, days):
        from datetime import date

        last = date(2026, 8, 2)
        rows = [(code, (last - timedelta(days=d)).isoformat(), 100.0 + d * 0.01)
                for d in range(days)]
        funds.store_navs(self.conn, rows)

    def test_a_short_series_reports_which_metrics_were_skipped(self):
        from src import fund_watchlist

        code = fund_watchlist.SCHEME_CODES[0]
        self._navs(code, 30)
        metrics = funds.metrics_for(self.conn, code)
        skipped = funds.skipped_for_short_history(self.conn, code, metrics)
        self.assertIn("return_1y", skipped)
        self.assertIn("vol_1y", skipped)

    def test_a_long_series_skips_nothing(self):
        from src import fund_watchlist

        code = fund_watchlist.SCHEME_CODES[0]
        self._navs(code, 500)
        metrics = funds.metrics_for(self.conn, code)
        self.assertEqual(funds.skipped_for_short_history(self.conn, code, metrics), [])

    def test_the_note_names_mfapi_when_it_is_the_recorded_cause(self):
        from src import fund_watchlist

        code = fund_watchlist.SCHEME_CODES[0]
        self._navs(code, 30)
        funds.record_history_outcome(self.conn, ok=False, detail="HTTP 503")
        note = funds.history_note(self.conn, [code])
        self.assertIn("mfapi.in", note)
        self.assertIn("503", note)

    def test_the_note_is_silent_when_history_is_adequate(self):
        """It has to stay quiet to stay worth reading when it speaks."""
        from src import fund_watchlist

        code = fund_watchlist.SCHEME_CODES[0]
        self._navs(code, 500)
        funds.record_history_outcome(self.conn, ok=False, detail="HTTP 503")
        self.assertIsNone(funds.history_note(self.conn, [code]))

    def test_a_scheme_with_a_single_nav_is_reported_as_short(self):
        """The exact case the note exists for — a watchlist entry added while the
        history source was down. Its span is 0 days, same as an empty scheme, and
        conflating the two silenced the note about precisely this scheme."""
        from src import fund_watchlist

        code = fund_watchlist.SCHEME_CODES[0]
        funds.store_navs(self.conn, [(code, "2026-08-01", 100.0)])
        funds.record_history_outcome(self.conn, ok=False, detail="Connection refused")
        note = funds.history_note(self.conn, [code])
        self.assertIsNotNone(note)
        self.assertIn("mfapi.in", note)

    def test_a_scheme_with_no_navs_at_all_produces_no_note(self):
        """Nothing was ever asked for, so there is nothing to explain."""
        from src import fund_watchlist

        self.assertIsNone(funds.history_note(self.conn, [fund_watchlist.SCHEME_CODES[0]]))

    def test_a_short_series_does_not_report_a_one_year_volatility(self):
        """A 1-year vol computed from a month is a number with the wrong label, and
        unlike a missing value a wrong label is invisible."""
        from src import fund_watchlist

        code = fund_watchlist.SCHEME_CODES[0]
        self._navs(code, 30)
        metrics = funds.metrics_for(self.conn, code)
        self.assertIsNone(metrics["vol_1y"])
        self.assertIsNone(metrics["max_drawdown_1y"])

    def test_a_full_series_still_reports_one(self):
        """The guard must not suppress the metric it is meant to qualify."""
        from src import fund_watchlist

        code = fund_watchlist.SCHEME_CODES[0]
        self._navs(code, 500)
        self.assertIsNotNone(funds.metrics_for(self.conn, code)["vol_1y"])

    def test_a_failed_outcome_round_trips(self):
        funds.record_history_outcome(self.conn, ok=False, detail="timeout")
        outcome = funds.history_outcome(self.conn)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["detail"], "timeout")


class EveryScheduledMessageCarriesHealthTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, "
            "volume, source) VALUES (?,?,?,?,?,?,?,?)",
            [(s, "2026-07-27", 100, 100, 100, 100, 1000, "test") for s in universe.UNIVERSE])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_the_evening_report_carries_it(self):
        self.assertIn("2026-07-27", deliver.build_report(self.conn, "2026-08-02"))

    def test_the_morning_brief_carries_it(self):
        self.assertIn("2026-07-27", brief.build_brief(self.conn))

    def test_the_weekly_carries_it(self):
        self.assertIn("2026-07-27", weekly.build_weekly(self.conn, "2026-08-02"))


if __name__ == "__main__":
    unittest.main()
