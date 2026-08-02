"""The two hand-maintained descriptions of the same pipeline must agree.

    python -m unittest discover -s tests -v

`main.ALL_STAGES` and the evening workflow's list of `--stage` steps describe the
same daily chain, separately, in two languages. They had already drifted once —
`funds` sat in a different position in each — harmlessly, because `funds` happens
to be independent of everything around it. The next drift will not be a stage that
happens to be independent.

This is the cheapest possible guard: read the YAML, read the tuple, compare.
"""

import pathlib
import re
import unittest

import main

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVENING = ROOT / ".github" / "workflows" / "evening.yml"

# Runs as its own workflow step but deliberately outside ALL_STAGES: it is
# observational, and it costs an LLM call per candidate with a rate-limit floor
# between them. `--stage all` is what you reach for locally.
WORKFLOW_ONLY = {"sentiment"}


def workflow_stages():
    """Stage names in the order the evening workflow invokes them, deduplicated.

    Deduplicated because ingest appears three times — the bhavcopy retry ladder
    calls it twice more inside one step.
    """
    text = EVENING.read_text()
    seen, ordered = set(), []
    for name in re.findall(r"python main\.py --stage ([a-z-]+)", text):
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


class PipelineWiringTestCase(unittest.TestCase):
    def setUp(self):
        self.declared = [m.__name__.split(".")[-1] for m in main.ALL_STAGES]
        self.scheduled = workflow_stages()

    def test_the_workflow_actually_lists_stages(self):
        """Guards the guard: a renamed workflow file or a changed invocation would
        otherwise make every assertion below vacuously true."""
        self.assertTrue(EVENING.exists(), "evening.yml moved — this test is now blind")
        self.assertGreater(len(self.scheduled), 3)

    def test_every_scheduled_stage_exists(self):
        for name in self.scheduled:
            with self.subTest(stage=name):
                self.assertIn(name, main.STAGES, f"evening.yml runs --stage {name}, which main.py does not define")

    def test_the_daily_chain_matches_the_workflow_order(self):
        """Order matters: signals read features, journal fills from signals,
        delivery reports the ledger."""
        self.assertEqual(self.declared,
                         [s for s in self.scheduled if s not in WORKFLOW_ONLY])

    def test_stages_left_out_of_the_daily_chain_are_deliberate(self):
        """Anything the workflow runs but ALL_STAGES omits has to be on the list of
        known exclusions, so a stage cannot fall out of `--stage all` silently."""
        self.assertEqual(set(self.scheduled) - set(self.declared), WORKFLOW_ONLY)

    def test_sentiment_is_not_in_the_daily_chain(self):
        """It costs an LLM call per candidate. `--stage all` must stay free."""
        self.assertNotIn("sentiment", self.declared)

    def test_expensive_research_stages_stay_out(self):
        """backtest replays years across the universe; walkforward is slower still.
        Both answer research questions, not daily ones."""
        for name in ("backtest", "walkforward", "backfill", "doctor", "verify-data", "poll"):
            with self.subTest(stage=name):
                self.assertNotIn(name, self.declared)

    def test_every_declared_stage_is_dispatchable(self):
        """ALL_STAGES holds modules; STAGES maps names to them. A module in one and
        not the other is a stage you cannot invoke by name."""
        for name in self.declared:
            with self.subTest(stage=name):
                self.assertIn(name, main.STAGES)


if __name__ == "__main__":
    unittest.main()
