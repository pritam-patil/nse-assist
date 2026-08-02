"""The morning brief: correct numbers, and a tone that does not sell.

    python -m unittest discover -s tests -v

The tone assertions are not decoration. A brief that dresses three ordinary signals
as an opportunity is working against the person reading it before the open, and
that failure is silent — the numbers stay right while the framing does the damage.
Asserting the absence of emojis and hype keeps a future edit from drifting there.
"""

import os
import tempfile
import unittest

from src import brief, risk_config, signals
from src.db import get_connection, init_db

HYPE = ("!", "🚀", "📈", "💰", "opportunity", "huge", "massive", "don't miss",
        "act now", "strong buy", "hot", "surge")


def signal_row(symbol, entry=1000.0, stop=970.0, target=1040.0, size=20,
               rule="momentum_continuation", confirming=None, date="2026-08-01"):
    return (date, symbol, rule, "long", entry, stop, target, size, "proposed", "x", confirming)


class BriefTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self._enabled = signals.ENABLED_RULES
        signals.ENABLED_RULES = tuple(signals.RULES)

    def tearDown(self):
        signals.ENABLED_RULES = self._enabled
        self.conn.close()
        os.unlink(self.path)

    def _add(self, *rows):
        self.conn.executemany(
            "INSERT INTO signals (date,symbol,rule,direction,entry,stop,target,size,status,"
            "created_at,confirming_rules) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    # --- content ---

    def test_lists_every_required_field(self):
        self._add(signal_row("AAA"))
        text = brief.build_brief(self.conn)
        for expected in ("AAA", "momentum_continuation", "entry", "stop", "target",
                         "shares", "at risk"):
            with self.subTest(field=expected):
                self.assertIn(expected, text)

    def test_entry_is_labelled_an_estimate(self):
        """The fill happens at an open that has not happened yet. Calling the
        estimate an entry price is a small lie that compounds."""
        self._add(signal_row("AAA"))
        self.assertIn("estimate", brief.build_brief(self.conn))

    def test_footer_reports_deployed_and_worst_case(self):
        self._add(signal_row("AAA"), signal_row("BBB"))
        text = brief.build_brief(self.conn)
        self.assertIn("deployed", text)
        self.assertIn("Worst case combined loss", text)

    def test_confirming_rules_are_shown(self):
        self._add(signal_row("AAA", confirming="volume_breakout"))
        self.assertIn("volume_breakout", brief.build_brief(self.conn))

    # --- the caps ---

    def test_worst_case_never_exceeds_the_daily_limit(self):
        """Five positions risking 600 each against a 2,500 limit."""
        self._add(*[signal_row(f"S{i}") for i in range(5)])
        _, _, risk = brief.trim_to_daily_loss(brief.enabled_signals(self.conn)[1])
        self.assertLessEqual(risk, risk_config.MAX_DAILY_LOSS)

    def test_trimming_is_stated_not_silent(self):
        self._add(*[signal_row(f"S{i}") for i in range(5)])
        self.assertIn("Trimmed to fit the daily loss limit", brief.build_brief(self.conn))

    def test_disabled_rules_are_not_presented_as_actionable(self):
        """A walk-forward verdict can land between the evening scan and the brief."""
        self._add(signal_row("AAA", rule="volume_breakout"))
        signals.ENABLED_RULES = ("momentum_continuation",)
        self.assertNotIn("AAA", brief.build_brief(self.conn))

    # --- the empty day ---

    def test_no_rules_fired_is_stated_plainly(self):
        self._add(signal_row("AAA", rule="volume_breakout"))
        signals.ENABLED_RULES = ("momentum_continuation",)
        self.assertIn("No rules fired", brief.build_brief(self.conn))

    def test_no_rules_enabled_is_distinct_from_nothing_firing(self):
        """Nothing fired invites you to check tomorrow. Nothing can fire does not."""
        self._add(signal_row("AAA"))
        signals.ENABLED_RULES = ()
        text = brief.build_brief(self.conn)
        self.assertIn("No rules enabled", text)
        self.assertNotIn("No rules fired", text)

    def test_an_empty_day_is_not_an_error(self):
        signals.ENABLED_RULES = ("momentum_continuation",)
        self.assertIsInstance(brief.build_brief(self.conn), str)

    # --- previous session ---

    def test_previous_session_line_when_nothing_closed(self):
        self.assertIn("no paper trades closed", brief.build_brief(self.conn))

    def test_previous_session_line_counts_and_nets(self):
        self.conn.execute(
            "INSERT INTO paper_trades (signal_id,entry_date,entry_price,exit_date,"
            "exit_price,exit_reason,pnl) VALUES (1,'2026-07-30',100,'2026-08-01',110,'target',940)")
        self.conn.commit()
        text = brief.build_brief(self.conn)
        self.assertIn("1 closed", text)
        self.assertIn("1 winner,", text, "should not read '1 winners'")

    # --- tone ---

    def test_no_emojis_or_hype(self):
        self._add(signal_row("AAA"), signal_row("BBB", confirming="volume_breakout"))
        text = brief.build_brief(self.conn).lower()
        for token in HYPE:
            with self.subTest(token=token):
                self.assertNotIn(token.lower(), text)

    def test_no_emoji_codepoints(self):
        self._add(signal_row("AAA"))
        for char in brief.build_brief(self.conn):
            self.assertLess(ord(char), 0x2190, f"{char!r} is a symbol or emoji")

    def test_carries_the_paper_trading_disclaimer(self):
        self._add(signal_row("AAA"))
        self.assertIn("Not investment advice", brief.build_brief(self.conn))


if __name__ == "__main__":
    unittest.main()
