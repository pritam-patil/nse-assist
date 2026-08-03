"""The sentiment layer: it observes, it does not act, and it fails silently.

    python -m unittest discover -s tests -v

THE MOST IMPORTANT TEST IN THIS FILE IS test_no_decision_module_imports_sentiment.

Every other guarantee here is a promise in a docstring. That one is structural: it
reads the source of the modules that decide things and asserts none of them can
see this one. A future edit that wires sentiment into sizing fails it, which is the
only form of "must not act" that survives contact with a later refactor.
"""

import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src import brief, news, rules_config, sentiment, sentiment_scorecard, weekly
from src.db import get_connection, init_db

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "src"

# Modules that decide what is traded, how much, or in what order.
DECISION_MODULES = ("signals.py", "journal.py", "backtest.py", "walkforward.py",
                    "features.py", "risk_config.py", "rules_config.py")


class NonInterferenceTestCase(unittest.TestCase):
    def test_no_decision_module_imports_sentiment(self):
        """Structural, not a promise. Sentiment reads the assembled portfolio; the
        portfolio must not be able to read sentiment back."""
        for name in DECISION_MODULES:
            with self.subTest(module=name):
                text = (SOURCE / name).read_text()
                for forbidden in ("import sentiment", "from src.sentiment",
                                  "news_sentiment", "sentiment_scorecard"):
                    self.assertNotIn(forbidden, text,
                                     f"{name} must not depend on the sentiment layer")

    def test_the_scan_produces_identical_output_with_and_without_scores(self):
        """The end-to-end version of the same claim: storing a score changes nothing
        about what was proposed."""
        conn = get_connection(":memory:")
        init_db(conn)
        try:
            for i, symbol in enumerate(("AAA", "BBB", "CCC")):
                conn.execute(
                    "INSERT INTO signals (date, symbol, rule, direction, entry, stop, "
                    "target, size, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("2026-08-02", symbol, "momentum_continuation", "long",
                     100.0, 95.0, 110.0, 10, "proposed", "x"))
            conn.commit()
            before = [dict(r) for r in conn.execute("SELECT * FROM signals ORDER BY id")]

            for row in before:
                sentiment.store(conn, row["id"], row["symbol"], row["date"],
                                -0.9, "bad news", [{"title": "x"}])
            after = [dict(r) for r in conn.execute("SELECT * FROM signals ORDER BY id")]
            self.assertEqual(before, after)
        finally:
            conn.close()


class FrozenGraduationGateTestCase(unittest.TestCase):
    """Pre-committed 2026-08-02, before the first score was stored. Same mechanism
    as the paper-trading gate: editing these fails the suite until this file is
    edited too."""

    def test_minimum_annotated_trades_is_sixty(self):
        self.assertEqual(rules_config.SENTIMENT_MIN_ANNOTATED_TRADES, 60)

    def test_it_is_double_the_paper_gate_because_it_is_a_subgroup_analysis(self):
        """The question is about the negative tercile — a third of the sample — so
        the sample has to be bigger for the subgroup to contain anything."""
        self.assertEqual(
            rules_config.SENTIMENT_MIN_ANNOTATED_TRADES,
            2 * rules_config.EVALUATION_MIN_TRADES,
        )

    def test_the_cohort_gap_threshold_is_frozen(self):
        self.assertEqual(rules_config.SENTIMENT_MIN_COHORT_GAP, 200.0)

    def test_the_freeze_date_is_recorded(self):
        self.assertEqual(rules_config.SENTIMENT_GATE_FROZEN_ON, "2026-08-02")

    def test_the_graduated_role_is_veto_only(self):
        """Never a signal generator. A layer that can only remove candidates can be
        evaluated against the counterfactual of not removing them."""
        role = rules_config.SENTIMENT_ROLE_IF_GRADUATED
        self.assertIn("veto-only", role)
        self.assertNotIn("generat", role)


class ScoringTestCase(unittest.TestCase):
    def test_no_headlines_produces_no_score(self):
        self.assertEqual(sentiment.score_headlines("AAA", []), (None, None, None))

    def test_a_score_outside_the_range_is_rejected_not_clipped(self):
        """Clipping turns a misunderstanding of the scale into a confident extreme."""
        for bad in (5.0, -3.2, "very positive", None, float("nan")):
            with self.subTest(value=bad):
                self.assertIsNone(sentiment._clamp(bad))

    def test_valid_scores_survive(self):
        for good, expected in ((-1.0, -1.0), (0, 0.0), (0.4567, 0.457), ("0.5", 0.5)):
            with self.subTest(value=good):
                self.assertEqual(sentiment._clamp(good), expected)

    def test_an_llm_failure_is_a_no_op(self):
        """Every failure mode returns nothing and the run continues."""
        real = sentiment.config.GEMINI_API_KEY
        sentiment.config.GEMINI_API_KEY = "x"
        try:
            from src import llm

            original = llm.generate
            llm.generate = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
            try:
                self.assertEqual(
                    sentiment.score_headlines("AAA", [{"title": "t"}]), (None, None, None))
            finally:
                llm.generate = original
        finally:
            sentiment.config.GEMINI_API_KEY = real

    def test_a_non_object_response_is_a_no_op(self):
        real = sentiment.config.GEMINI_API_KEY
        sentiment.config.GEMINI_API_KEY = "x"
        try:
            from src import llm

            original = llm.generate
            llm.generate = lambda *a, **kw: ["not", "an", "object"]
            try:
                self.assertEqual(
                    sentiment.score_headlines("AAA", [{"title": "t"}]), (None, None, None))
            finally:
                llm.generate = original
        finally:
            sentiment.config.GEMINI_API_KEY = real

    def test_without_any_api_key_scoring_is_skipped(self):
        gem, groq = sentiment.config.GEMINI_API_KEY, sentiment.config.GROQ_API_KEY
        sentiment.config.GEMINI_API_KEY = sentiment.config.GROQ_API_KEY = None
        try:
            self.assertEqual(
                sentiment.score_headlines("AAA", [{"title": "t"}]), (None, None, None))
        finally:
            sentiment.config.GEMINI_API_KEY, sentiment.config.GROQ_API_KEY = gem, groq

    def test_the_prompt_tells_the_model_that_most_days_are_neutral(self):
        """Without it, a model asked for a score returns a confident one every time,
        and the layer measures the model's willingness rather than the news."""
        self.assertIn("Most days are 0.0", sentiment.SYSTEM_PROMPT)


class NewsTestCase(unittest.TestCase):
    def test_malformed_xml_returns_nothing_rather_than_raising(self):
        self.assertEqual(news._parse_rss("<rss><broken", "test"), [])

    def test_items_without_a_title_are_dropped(self):
        feed = "<rss><channel><item><link>x</link></item></channel></rss>"
        self.assertEqual(news._parse_rss(feed, "test"), [])

    def test_a_well_formed_item_parses(self):
        feed = ("<rss><channel><item><title>Reliance beats estimates</title>"
                "<link>http://x</link><pubDate>Mon, 27 Jul 2026 10:00:00 GMT</pubDate>"
                "</item></channel></rss>")
        items = news._parse_rss(feed, "test")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Reliance beats estimates")

    def test_old_headlines_are_dropped(self):
        now = datetime(2026, 8, 2, tzinfo=timezone.utc)
        old = (now - timedelta(days=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        fresh = (now - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        items = [{"title": "old", "published": old}, {"title": "fresh", "published": fresh}]
        kept = [i["title"] for i in news._recent(items, as_of=now)]
        self.assertEqual(kept, ["fresh"])

    def test_undated_headlines_are_kept(self):
        """A feed omitting pubDate would otherwise contribute nothing at all, and
        the alternative failure costs a score nothing acts on."""
        items = [{"title": "undated", "published": ""}]
        self.assertEqual(len(news._recent(items, as_of=datetime(2026, 8, 2))), 1)

    def test_symbol_matching_respects_word_boundaries(self):
        """A bare substring test on three-letter tickers matches a great deal of
        unrelated text."""
        items = [{"title": "IOC posts higher refining margins"},
                 {"title": "Associoc Ltd announces buyback"}]
        matched = [i["title"] for i in news.filter_by_symbol(items, "IOC")]
        self.assertEqual(len(matched), 1)
        self.assertIn("IOC posts", matched[0])

    def test_a_mapped_company_name_is_used_in_the_query(self):
        self.assertIn("Bharat Electronics", news.query_for("BEL"))

    def test_an_unmapped_symbol_still_gets_a_specific_query(self):
        from src import universe

        unmapped = next(s for s in universe.UNIVERSE if s not in news.COMPANY_NAMES)
        query = news.query_for(unmapped)
        self.assertIn(unmapped, query)
        self.assertIn("NSE", query)

    def test_group_tickers_are_mapped_because_the_bare_ticker_misfires(self):
        """Measured, not assumed: searching "RELIANCE NSE share price" against the
        live feed returned coverage of Reliance Infrastructure, a different
        company. Group families are where a bare ticker is actively wrong rather
        than merely vague."""
        for symbol in ("RELIANCE", "TATASTEEL", "BAJFINANCE", "ADANIENT"):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, news.COMPANY_NAMES)

    def test_every_mapped_key_is_a_universe_symbol(self):
        """The half of the mapping risk that is checkable. A key that is not a
        universe symbol is inert — the symbol falls through to the generic query
        and nothing reports the mapping was skipped. This found four bad keys the
        first time it ran."""
        news.assert_consistent()

    def test_price_widget_headlines_are_dropped(self):
        """They are templates with a company name in them, carrying no view, and
        they crowd out the headlines that do — ten is the whole budget."""
        for title in ("Axis Bank Share Price Today, Live NSE Updates",
                      "Stocks to Watch Today: Tata Steel, Swiggy, Mazagon Dock",
                      "TCS Stock Price Live NSE/BSE Updates"):
            with self.subTest(title=title):
                self.assertTrue(news.is_noise(title))

    def test_real_headlines_survive_the_noise_filter(self):
        """A filter that guessed at relevance would be a second unvalidated model
        in front of the first one."""
        for title in ("Bharat Electronics Q1 profit rises 9% YoY to Rs 1,054 crore",
                      "Axis Bank sells stake in small-cap infrastructure firm"):
            with self.subTest(title=title):
                self.assertFalse(news.is_noise(title))


class ScorecardTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _scored_trade(self, score, pnl, symbol=None, exit_date="2026-07-30"):
        symbol = symbol or f"S{abs(hash((score, pnl, exit_date))) % 99999}"
        cursor = self.conn.execute(
            "INSERT INTO signals (date, symbol, rule, direction, entry, stop, target, "
            "size, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-07-27", symbol, "momentum_continuation", "long",
             100.0, 95.0, 110.0, 10, "taken", "x"))
        signal_id = cursor.lastrowid
        self.conn.execute(
            "INSERT INTO paper_trades (signal_id, entry_date, entry_price, exit_date, "
            "exit_price, exit_reason, pnl, status) VALUES (?,?,?,?,?,?,?,?)",
            (signal_id, "2026-07-28", 100.0, exit_date, 105.0,
             "target" if pnl > 0 else "stop", pnl, "closed"))
        if score is not None:
            sentiment.store(self.conn, signal_id, symbol, "2026-07-27", score,
                            "r", [{"title": "h"}])
        self.conn.commit()

    def test_unscored_trades_are_absent_not_neutral(self):
        """Treating them as zero would swamp the sample with trades the layer never
        saw."""
        self._scored_trade(0.5, 100)
        self._scored_trade(None, -100, symbol="NOSCORE")
        self.assertEqual(len(sentiment_scorecard.annotated_trades(self.conn)), 1)

    def test_correlation_is_withheld_on_a_small_sample(self):
        """r on eleven trades is a confident-looking number that means nothing."""
        for i in range(5):
            self._scored_trade(0.1 * i, 100 * i, symbol=f"A{i}")
        text = sentiment_scorecard.build_scorecard(self.conn)
        self.assertIn("correlation withheld", text)

    def test_correlation_is_reported_once_the_sample_is_large_enough(self):
        for i in range(sentiment_scorecard.MIN_TRADES_FOR_CORRELATION):
            self._scored_trade(round(-1 + 0.1 * i, 2), 100 * i, symbol=f"B{i}")
        text = sentiment_scorecard.build_scorecard(self.conn)
        self.assertIn("correlation:", text)

    def test_a_perfect_relationship_reads_as_one(self):
        pairs = [(x / 10, x) for x in range(-10, 11)]
        self.assertAlmostEqual(sentiment_scorecard.correlation(pairs), 1.0, places=9)

    def test_correlation_is_none_without_variance(self):
        """Undefined, not zero. Zero would read as 'measured, no relationship'."""
        self.assertIsNone(sentiment_scorecard.correlation([(0.5, 100)] * 5))

    def test_terciles_split_by_rank_not_by_fixed_cuts(self):
        """Most days score 0.0, so a fixed-cut split would put the whole sample in
        one bucket and report two empty ones as though they were measured."""
        for i in range(9):
            self._scored_trade(0.0 if i < 6 else 0.5, 100, symbol=f"C{i}")
        buckets = sentiment_scorecard.terciles(
            sentiment_scorecard.annotated_trades(self.conn))
        self.assertEqual(len(buckets), 3)
        self.assertTrue(all(b["trades"] == 3 for b in buckets))

    def test_a_thin_bucket_is_marked_rather_than_reported_plainly(self):
        for i in range(6):
            self._scored_trade(0.1 * i, 100, symbol=f"D{i}")
        buckets = sentiment_scorecard.terciles(
            sentiment_scorecard.annotated_trades(self.conn))
        self.assertTrue(any(not b["enough"] for b in buckets))

    def test_the_negative_cohort_gap_is_computed_directly(self):
        """It is the number the graduation gate is written against, so it is not
        left to be eyeballed off the tercile table."""
        for i in range(5):
            self._scored_trade(-0.6, -500, symbol=f"N{i}")
        for i in range(5):
            self._scored_trade(0.4, 300, symbol=f"P{i}")
        gap = sentiment_scorecard.negative_cohort_gap(
            sentiment_scorecard.annotated_trades(self.conn))
        self.assertEqual(gap["negative_trades"], 5)
        self.assertEqual(gap["gap"], -800)

    def test_the_gap_is_none_without_both_cohorts(self):
        for i in range(3):
            self._scored_trade(0.4, 300, symbol=f"P{i}")
        self.assertIsNone(sentiment_scorecard.negative_cohort_gap(
            sentiment_scorecard.annotated_trades(self.conn)))

    def test_an_empty_scorecard_says_what_it_needs(self):
        text = sentiment_scorecard.build_scorecard(self.conn)
        self.assertIn("0 annotated closed trades", text)
        self.assertIn(str(rules_config.SENTIMENT_MIN_ANNOTATED_TRADES), text)

    def test_the_scorecard_restates_that_nothing_acted_on_it(self):
        self._scored_trade(0.5, 100)
        text = sentiment_scorecard.build_scorecard(self.conn)
        self.assertIn("veto-only", text)
        self.assertIn("never as a signal generator", text)

    def test_no_emoji_codepoints(self):
        self._scored_trade(0.5, 100)
        for char in sentiment_scorecard.build_scorecard(self.conn):
            self.assertLess(ord(char), 0x2190, f"{char!r} is a symbol or emoji")


class BriefAnnotationTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self._enable()

    def tearDown(self):
        self._restore()
        self.conn.close()
        os.unlink(self.path)

    def _enable(self):
        from src import signals

        self._saved = signals.ENABLED_RULES
        signals.ENABLED_RULES = ("momentum_continuation",)

    def _restore(self):
        from src import signals

        signals.ENABLED_RULES = self._saved

    def _signal(self, symbol="AAA", score=None, rationale="order book thinning"):
        cursor = self.conn.execute(
            "INSERT INTO signals (date, symbol, rule, direction, entry, stop, target, "
            "size, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-08-02", symbol, "momentum_continuation", "long",
             100.0, 95.0, 110.0, 10, "proposed", "x"))
        if score is not None:
            sentiment.store(self.conn, cursor.lastrowid, symbol, "2026-08-02",
                            score, rationale, [{"title": "h"}])
        self.conn.commit()

    def test_a_score_is_shown_with_its_rationale(self):
        self._signal(score=-0.4)
        text = brief.build_brief(self.conn, "2026-08-02")
        self.assertIn("news -0.40", text)
        self.assertIn("order book thinning", text)

    def test_the_label_sits_on_the_same_line_as_the_number(self):
        """A score shown without its status reads as an input to the decision."""
        self._signal(score=0.6)
        line = [l for l in brief.build_brief(self.conn, "2026-08-02").splitlines()
                if "news +0.60" in l][0]
        self.assertIn("unvalidated", line)
        self.assertIn("informational only", line)

    def test_a_brief_without_any_score_is_complete(self):
        """Sentiment is garnish. Its absence must not be visible as a gap."""
        self._signal(score=None)
        text = brief.build_brief(self.conn, "2026-08-02")
        self.assertIn("AAA", text)
        self.assertNotIn("news ", text)
        self.assertNotIn("unvalidated", text)

    def test_the_brief_states_that_scores_changed_nothing(self):
        self._signal(score=0.6)
        text = brief.build_brief(self.conn, "2026-08-02")
        self.assertIn("did not filter, size, order or veto", text)

    def test_the_disclaimer_is_absent_when_no_scores_exist(self):
        self._signal(score=None)
        self.assertNotIn("did not filter", brief.build_brief(self.conn, "2026-08-02"))


class WeeklyIntegrationTestCase(unittest.TestCase):
    def test_the_weekly_carries_the_shadow_scorecard(self):
        conn = get_connection(":memory:")
        init_db(conn)
        try:
            text = weekly.build_weekly(conn, "2026-08-02")
            self.assertIn("SENTIMENT SHADOW SCORECARD", text)
            self.assertIn("observational", text)
        finally:
            conn.close()


class IdempotencyTestCase(unittest.TestCase):
    def test_a_replayed_evening_keeps_the_original_score(self):
        """Overwriting would replace a point-in-time record with a later view of the
        news, which is the one thing this table exists to prevent."""
        conn = get_connection(":memory:")
        init_db(conn)
        try:
            cursor = conn.execute(
                "INSERT INTO signals (date, symbol, rule, direction, entry, stop, "
                "target, size, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("2026-08-02", "AAA", "momentum_continuation", "long",
                 100.0, 95.0, 110.0, 10, "proposed", "x"))
            signal_id = cursor.lastrowid
            sentiment.store(conn, signal_id, "AAA", "2026-08-02", -0.5, "first",
                            [{"title": "a"}])
            sentiment.store(conn, signal_id, "AAA", "2026-08-02", 0.9, "second",
                            [{"title": "b"}])
            rows = conn.execute("SELECT score, rationale FROM news_sentiment").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["rationale"], "first")
        finally:
            conn.close()


class EmptyScorecardExplanationTestCase(unittest.TestCase):
    """The count is easy; the reason for it is what goes stale.

    This sentence read "with every rule disabled, nothing is being assembled" for
    a day after a rule was enabled — in the same message that carried a banner
    naming the enabled rule four paragraphs above. A count with a wrong
    explanation is worse than a count alone, because the explanation is the part a
    reader acts on. Each branch now has a test.
    """

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        from src import signals

        self._saved = signals.ENABLED_RULES

    def tearDown(self):
        from src import signals

        signals.ENABLED_RULES = self._saved
        self.conn.close()
        os.unlink(self.path)

    def _enable(self, *rules):
        from src import signals

        signals.ENABLED_RULES = rules

    def _scored_signal(self, symbol="AAA", trade_status=None):
        cursor = self.conn.execute(
            "INSERT INTO signals (date, symbol, rule, direction, entry, stop, target, "
            "size, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-08-03", symbol, "momentum_continuation", "long",
             100.0, 95.0, 110.0, 10, "proposed", "x"))
        sentiment.store(self.conn, cursor.lastrowid, symbol, "2026-08-03", 0.1, "r",
                        [{"title": "h"}])
        if trade_status:
            self.conn.execute(
                "INSERT INTO paper_trades (signal_id, entry_date, entry_price, status) "
                "VALUES (?,?,?,?)", (cursor.lastrowid, "2026-08-03", 100.0, trade_status))
        self.conn.commit()

    def test_with_no_rules_enabled_it_says_nothing_is_being_assembled(self):
        self._enable()
        self.assertIn("every rule disabled", sentiment_scorecard._why_empty(self.conn))

    def test_with_a_rule_enabled_and_nothing_scored_it_says_so(self):
        self._enable("momentum_continuation")
        text = sentiment_scorecard._why_empty(self.conn)
        self.assertIn("None has been", text)
        self.assertNotIn("every rule disabled", text)

    def test_scored_candidates_awaiting_a_fill_are_named(self):
        """The live case that was being misdescribed."""
        self._enable("momentum_continuation")
        self._scored_signal()
        text = sentiment_scorecard._why_empty(self.conn)
        self.assertIn("1 candidate(s) scored", text)
        self.assertIn("next open", text)
        self.assertNotIn("every rule disabled", text)

    def test_open_positions_are_named(self):
        self._enable("momentum_continuation")
        self._scored_signal(trade_status="open")
        text = sentiment_scorecard._why_empty(self.conn)
        self.assertIn("1 position(s) still open", text)

    def test_the_explanation_never_contradicts_the_enabled_state(self):
        """The invariant behind all four: a report must not say rules are disabled
        while one is enabled."""
        for rules in ((), ("momentum_continuation",)):
            with self.subTest(enabled=rules):
                self._enable(*rules)
                text = sentiment_scorecard._why_empty(self.conn)
                self.assertEqual(bool(rules), "every rule disabled" not in text)



if __name__ == "__main__":
    unittest.main()
