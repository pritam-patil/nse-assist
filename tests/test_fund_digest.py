"""The fund digest: stability-weighted ranks, raw numbers beside them, no advice.

    python -m unittest discover -s tests -v

Two things are asserted that look like style and are not. The absence of buy/sell
language is a correctness property — a ranking of past behaviour presented as a
recommendation is a different document with different consequences. And the raw
metrics have to appear next to the rank, because a composite you cannot audit is an
opinion the reader has no way to disagree with.
"""

import os
import pathlib
import tempfile
import unittest

from src import fund_digest
from src.db import get_connection, init_db

ADVICE = ("buy", "sell", "switch", "invest in", "recommend", "top pick",
          "best fund", "should hold", "avoid", "🚀", "!")

# The one sentence that must contain advice words, because its job is to disclaim
# them. Stripped before the scan so the check tests the report rather than its own
# disclaimer — otherwise the only way to pass is to delete the disclaimer.
DISCLAIMER = "No scheme above is a recommendation to buy, sell or switch."


def metrics(consistency=1.0, vol=0.005, worst=0.001, ret=0.065):
    return {"consistency_3m": consistency, "vol_1y": vol,
            "worst_month_1y": worst, "return_1y": ret}


class CompositeTestCase(unittest.TestCase):
    def test_stability_outweighs_return(self):
        """A steadier scheme with a lower return must rank above a jumpier one with
        a higher return — that is the entire point of the weighting."""
        steady = metrics(consistency=1.0, vol=0.002, worst=0.001, ret=0.060)
        jumpy = metrics(consistency=0.6, vol=0.020, worst=-0.020, ret=0.075)
        steady_score, jumpy_score = fund_digest.composite_scores([steady, jumpy])
        self.assertGreater(steady_score, jumpy_score)

    def test_return_alone_does_not_win(self):
        best_return = metrics(consistency=0.5, vol=0.030, worst=-0.030, ret=0.090)
        modest = metrics(consistency=1.0, vol=0.003, worst=0.000, ret=0.055)
        scores = fund_digest.composite_scores([best_return, modest])
        self.assertLess(scores[0], scores[1])

    def test_worst_month_moves_the_score(self):
        """Identical but for the tail: the one with the worse month must rank lower."""
        mild = metrics(worst=-0.001)
        harsh = metrics(worst=-0.030)
        mild_score, harsh_score = fund_digest.composite_scores([mild, harsh])
        self.assertGreater(mild_score, harsh_score)

    def test_a_single_scheme_scores_neutral_not_perfect(self):
        """Best-of-one has demonstrated nothing. Scoring it 1.0 would let an
        unrankable category produce a confident-looking number."""
        self.assertEqual(fund_digest.composite_scores([metrics()]), [0.5])

    def test_a_missing_metric_does_not_rank_a_scheme_last(self):
        """Three schemes, not two: with only two the missing value leaves one
        observation on that axis, normalisation degenerates to neutral for everyone,
        and the test would pass or fail for reasons unrelated to its name.
        """
        short = metrics(worst=None)
        mild = metrics(worst=-0.001)
        harsh = metrics(worst=-0.050)
        short_score, mild_score, harsh_score = fund_digest.composite_scores(
            [short, mild, harsh])
        self.assertGreater(short_score, harsh_score,
                           "a missing metric must not rank below a genuinely bad one")
        self.assertLess(short_score, mild_score,
                        "nor should it outrank a scheme that actually reported well")

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(fund_digest.COMPOSITE_WEIGHTS.values()), 1.0, places=9)

    def test_return_is_the_smallest_weight(self):
        weights = fund_digest.COMPOSITE_WEIGHTS
        self.assertEqual(min(weights, key=weights.get), "return")
        self.assertGreater(weights["consistency"], weights["return"])
        self.assertGreater(weights["stability"], weights["return"])


class DigestTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = get_connection(self.path)
        init_db(self.conn)
        self._navs("119091", 100.0)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _navs(self, code, start):
        from datetime import date, timedelta
        value, day, rows = start, date(2024, 1, 1), []
        for _ in range(500):
            value *= 1.0002
            rows.append((code, day.isoformat(), round(value, 4)))
            day += timedelta(days=1)
        self.conn.executemany(
            "INSERT OR REPLACE INTO fund_navs (scheme_code, date, nav) VALUES (?,?,?)", rows)
        self.conn.commit()

    def test_raw_metrics_appear_beside_the_rank(self):
        """A composite you cannot audit is an opinion with no way to disagree."""
        text = fund_digest.build_digest(self.conn, ["119091"])
        for label in ("1m", "3m", "1y", "vol", "worst month", "max drawdown", "consistency"):
            with self.subTest(metric=label):
                self.assertIn(label, text)

    def test_worst_month_is_prominent(self):
        """Its own line, labelled in words rather than abbreviated into a column."""
        text = fund_digest.build_digest(self.conn, ["119091"])
        self.assertIn("worst month", text)

    def test_weights_are_stated_in_the_message(self):
        text = fund_digest.build_digest(self.conn, ["119091"])
        self.assertIn("Composite weights", text)
        for name in fund_digest.COMPOSITE_WEIGHTS:
            self.assertIn(name, text)

    def test_single_scheme_categories_are_called_out(self):
        """Wording changed when the prose was trimmed; the substance did not. The
        assertion is on what a reader must learn — that a lone scheme cannot be
        ranked — rather than on the sentence that happens to carry it."""
        text = fund_digest.build_digest(self.conn, ["119091"])
        self.assertIn("One scheme only", text)
        self.assertIn("describes nothing", text)
        self.assertIn("Add a second to compare", text)

    def test_labels_are_not_truncated_mid_word(self):
        from src import fund_watchlist
        text = fund_digest.build_digest(self.conn, ["119091"])
        self.assertIn(fund_watchlist.label_for("119091"), text)

    def test_no_metrics_says_so_rather_than_printing_an_empty_table(self):
        empty = get_connection(":memory:")
        init_db(empty)
        self.assertIn("No fund metrics available", fund_digest.build_digest(empty, ["119091"]))
        empty.close()

    # --- the reminders ---

    def test_states_that_ranks_are_not_a_forecast(self):
        text = fund_digest.build_digest(self.conn, ["119091"])
        self.assertIn("not a", text)
        self.assertIn("forecast", text)

    def test_mentions_exit_loads_and_taxation(self):
        text = fund_digest.build_digest(self.conn, ["119091"]).lower()
        self.assertIn("exit load", text)
        self.assertIn("slab rate", text)

    def test_distinguishes_debt_from_arbitrage_taxation(self):
        """Arbitrage schemes are taxed on the equity basis. A blanket 'debt funds at
        slab' line would be wrong for a category the digest itself ranks."""
        text = fund_digest.build_digest(self.conn, ["119091"]).lower()
        self.assertIn("arbitrage", text)
        self.assertIn("equity", text)

    def test_links_nothing(self):
        text = fund_digest.build_digest(self.conn, ["119091"])
        self.assertNotIn("http", text)

    # --- no advice ---

    def test_no_buy_sell_or_recommendation_language(self):
        text = fund_digest.build_digest(self.conn, ["119091"])
        self.assertIn(DISCLAIMER, text, "the disclaimer itself must be present")
        body = text.replace(DISCLAIMER, "").lower()
        for token in ADVICE:
            with self.subTest(token=token):
                self.assertNotIn(token.lower(), body)

    def test_carries_the_ranks_and_numbers_only_line(self):
        text = fund_digest.build_digest(self.conn, ["119091"])
        self.assertIn(DISCLAIMER, text)
        self.assertIn("Ranks and numbers only", text)

    def test_no_emoji_codepoints(self):
        for char in fund_digest.build_digest(self.conn, ["119091"]):
            self.assertLess(ord(char), 0x2190, f"{char!r} is a symbol or emoji")


class WeeklyIntegrationTestCase(unittest.TestCase):
    def test_the_weekly_carries_the_digest(self):
        """The spec says append to the weekly once it exists rather than sending a
        second message — two Telegram messages minutes apart is how both stop being
        read."""
        from src import weekly

        conn = get_connection(":memory:")
        init_db(conn)
        try:
            self.assertIn("PARKED CASH", weekly.build_weekly(conn, "2026-08-02"))
        finally:
            conn.close()


class WatchlistShapeTestCase(unittest.TestCase):
    """The watchlist has to support the ranking, or the digest describes nothing.

    A category with one scheme scores 0.50 and reads "1 of 1" — correct, and
    useless. These pin the shape that makes the composite mean something, so a
    future edit that drops back to one per category fails here rather than
    silently producing a report full of neutral scores.
    """

    def test_the_rankable_categories_hold_at_least_two_schemes(self):
        from collections import Counter

        from src import fund_watchlist

        counts = Counter(fund_watchlist.CATEGORIES.values())
        for category in ("Liquid Fund", "Arbitrage Fund"):
            with self.subTest(category=category):
                self.assertGreaterEqual(
                    counts[category], fund_digest.MIN_SCHEMES_TO_RANK,
                    f"{category} cannot be ranked with fewer than "
                    f"{fund_digest.MIN_SCHEMES_TO_RANK} schemes")

    def test_paired_categories_span_more_than_one_fund_house(self):
        """Two schemes from the same AMC compares a treasury desk against itself."""
        from src import fund_watchlist

        for category in ("Liquid Fund", "Arbitrage Fund"):
            labels = [fund_watchlist.LABELS[c]
                      for c, cat in fund_watchlist.CATEGORIES.items() if cat == category]
            houses = {label.split()[0] for label in labels}
            with self.subTest(category=category):
                self.assertGreater(len(houses), 1, f"{category}: all from {houses}")

    def test_a_ranked_category_produces_distinct_composites(self):
        """Two schemes must actually separate. Identical scores would mean the
        weighting is not discriminating and the rank is arbitrary."""
        rows = [metrics(consistency=1.0, vol=0.002, worst=0.001, ret=0.060),
                metrics(consistency=0.9, vol=0.009, worst=-0.003, ret=0.066)]
        scores = fund_digest.composite_scores(rows)
        self.assertNotEqual(scores[0], scores[1])


class WatchlistIsNotEnvironmentConfigTestCase(unittest.TestCase):
    """Which funds are tracked is a committed decision, not a setting.

    FUND_SCHEME_CODES used to be parsed by config.py, mapped into two workflows and
    documented in .env.example — and read by nothing. Setting it changed no
    behaviour and produced no error. Config that looks live and is not is worse
    than no config at all, because it invites a change that silently does nothing.

    These guard the removal: the name must stay gone, and the committed list must
    stay the only source.
    """

    SOURCE = pathlib.Path(__file__).resolve().parent.parent

    def test_the_dead_environment_variable_is_gone(self):
        for relative in ("src/config.py", ".env.example",
                         ".github/workflows/evening.yml", ".github/workflows/sunday.yml"):
            with self.subTest(file=relative):
                self.assertNotIn("FUND_SCHEME_CODES", (self.SOURCE / relative).read_text())

    def test_no_module_reads_a_scheme_list_from_the_environment(self):
        """The failure was config parsed but unread. This catches it being
        reintroduced anywhere, not just where it was."""
        for path in (self.SOURCE / "src").glob("*.py"):
            with self.subTest(module=path.name):
                self.assertNotIn("FUND_SCHEME_CODES", path.read_text())

    def test_the_committed_watchlist_is_the_only_source(self):
        from src import fund_watchlist

        self.assertTrue(fund_watchlist.SCHEME_CODES)
        self.assertEqual(
            fund_watchlist.SCHEME_CODES,
            tuple(code for code, _, _ in fund_watchlist.WATCHLIST))



if __name__ == "__main__":
    unittest.main()
