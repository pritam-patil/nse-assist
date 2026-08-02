"""Portfolio assembly: the caps must hold for every input, not just today's.

    python -m unittest discover -s tests -v

assemble_portfolio() is the only place cumulative risk is bounded, and Burst 7's
combined backtest calls this exact function. If it drifts from what the live scan
does, the backtest measures a strategy nobody trades — and nothing in either output
would show it. So the properties are asserted here rather than observed once in a
scan and assumed to hold.

Every case builds candidates directly instead of going through the database: these
are properties of the assembly arithmetic, and a fixture that needed 260 bars of
price history to test a cap would be testing the wrong thing.
"""

import unittest

from src import risk_config, rules_config, signals


def candidate(symbol, rule=signals.MOMENTUM, entry=1000.0, stop=970.0,
              target=None, size=20, turnover=1e9):
    """One sized candidate, shaped exactly as signals.levels() returns it.

    The target defaults to the configured reward:risk rather than a round number.
    That detail decides which cap binds first: at 2:1 the profit cap fires before
    the loss cap (5,000 upside is exactly 2,500 of risk), so a fixture with an
    invented ratio would test the wrong branch and quietly never reach the loss cap
    at all. Deriving it from rules_config keeps the fixture honest as that ratio is
    tuned.
    """
    if target is None:
        reward_risk = rules_config.TARGET_ATR_MULTIPLE / rules_config.STOP_ATR_MULTIPLE
        target = entry + (entry - stop) * reward_risk
    return {
        "symbol": symbol,
        "rule": rule,
        "direction": signals.LONG,
        "date": "2026-08-02",
        "turnover": turnover,
        "entry": entry,
        "stop": stop,
        "target": target,
        "size": size,
        "risk": round((entry - stop) * size, 2),
        "target_potential": round((target - entry) * size, 2),
        "bound_by": "capital",
    }


class DedupeTestCase(unittest.TestCase):
    def test_one_position_per_symbol(self):
        result = signals.assemble_portfolio([
            candidate("AAA", signals.MOMENTUM),
            candidate("AAA", signals.BREAKOUT),
            candidate("AAA", signals.REVERSION),
        ])
        self.assertEqual(len(result["portfolio"]), 1)

    def test_losing_rules_are_recorded_not_discarded(self):
        """Multi-rule agreement is a testable conviction signal. Throwing it away
        would make the question unanswerable later."""
        result = signals.assemble_portfolio([
            candidate("AAA", signals.MOMENTUM),
            candidate("AAA", signals.BREAKOUT),
        ])
        confirming = result["portfolio"][0]["confirming_rules"]
        self.assertEqual(len(confirming), 1)
        self.assertIn(signals.BREAKOUT, confirming)

    def test_tiebreak_prefers_the_tighter_stop(self):
        """With expectancies tied, the closer stop wins: less risked per share for
        the same ATR view.

        The tie is forced rather than assumed. This test used to rely on every
        configured expectancy being 0.0, so it silently changed meaning the moment
        Burst 7 measured them — testing expectancy precedence instead of the
        tie-break it names. A test whose subject depends on a config value is a test
        you cannot trust the name of.
        """
        original = dict(rules_config.RULE_EXPECTANCY)
        try:
            for rule in rules_config.RULE_EXPECTANCY:
                rules_config.RULE_EXPECTANCY[rule] = 0.0
            wide = candidate("AAA", signals.MOMENTUM, stop=900.0)
            tight = candidate("AAA", signals.BREAKOUT, stop=980.0)
            result = signals.assemble_portfolio([wide, tight])
            self.assertEqual(result["portfolio"][0]["rule"], signals.BREAKOUT)
        finally:
            rules_config.RULE_EXPECTANCY.clear()
            rules_config.RULE_EXPECTANCY.update(original)

    def test_measured_expectancy_drives_the_choice(self):
        """The better-expectancy rule wins a shared symbol, whatever the stops look
        like — asserted against values this test sets, not values it inherits.

        This is the third time a test here has been written against whatever
        rules_config happened to hold. Each time the config changed for a real
        reason and the test failed for an unreal one, which trains you to fix the
        test rather than read it. Any test naming a *mechanism* has to pin the
        inputs that mechanism reads.
        """
        original = dict(rules_config.RULE_EXPECTANCY)
        try:
            rules_config.RULE_EXPECTANCY[signals.MOMENTUM] = -500.0
            rules_config.RULE_EXPECTANCY[signals.REVERSION] = -10.0
            loser = candidate("AAA", signals.MOMENTUM, stop=990.0)   # tighter stop...
            winner = candidate("AAA", signals.REVERSION, stop=900.0)
            result = signals.assemble_portfolio([loser, winner])
            self.assertEqual(result["portfolio"][0]["rule"], signals.REVERSION)
        finally:
            rules_config.RULE_EXPECTANCY.clear()
            rules_config.RULE_EXPECTANCY.update(original)

    def test_assembly_works_with_every_rule_disabled(self):
        """The state a walk-forward can legitimately produce.

        assemble_portfolio() operates on candidates it is handed, so it must not
        care whether the rules that produced them are enabled — but nothing had ever
        exercised the path, and an empty enabled set is exactly where a max() over
        an empty sequence hides.
        """
        original = dict(rules_config.RULE_ENABLED)
        try:
            for rule in rules_config.RULE_ENABLED:
                rules_config.RULE_ENABLED[rule] = False
            self.assertEqual(signals.evaluate({"close": 100.0, "atr_14": 2.0}), [])
            result = signals.assemble_portfolio([])
            self.assertEqual(result["portfolio"], [])
            self.assertEqual(result["risk"], 0)
        finally:
            rules_config.RULE_ENABLED.clear()
            rules_config.RULE_ENABLED.update(original)

    def test_expectancy_outranks_the_tiebreak(self):
        """When Burst 8 fills in measured expectancies, they must dominate."""
        original = dict(rules_config.RULE_EXPECTANCY)
        try:
            rules_config.RULE_EXPECTANCY[signals.MOMENTUM] = 50.0
            wide = candidate("AAA", signals.MOMENTUM, stop=900.0)   # worse stop...
            tight = candidate("AAA", signals.BREAKOUT, stop=980.0)
            result = signals.assemble_portfolio([wide, tight])
            self.assertEqual(result["portfolio"][0]["rule"], signals.MOMENTUM)
        finally:
            rules_config.RULE_EXPECTANCY.clear()
            rules_config.RULE_EXPECTANCY.update(original)

    def test_distinct_symbols_are_untouched(self):
        result = signals.assemble_portfolio([candidate("AAA"), candidate("BBB")])
        self.assertEqual({c["symbol"] for c in result["portfolio"]}, {"AAA", "BBB"})


class CumulativeCapTestCase(unittest.TestCase):
    """Each cap must bind on the portfolio, not merely on each position."""

    def test_combined_loss_never_exceeds_max_daily_loss(self):
        """The cap that was missing before assembly existed.

        Each position individually respected max_daily_loss by construction, but
        several together did not — so the surfaced set could lose more in one day
        than the limit that has 'daily' in its name.
        """
        # Five positions risking 600 each: 3,000 against a 2,500 limit.
        heavy = [candidate(f"S{i}", entry=1000.0, stop=970.0, size=20,
                           turnover=1e9 - i) for i in range(5)]
        self.assertGreater(sum(c["risk"] for c in heavy), risk_config.MAX_DAILY_LOSS,
                           "fixture must actually breach the cap it claims to test")
        result = signals.assemble_portfolio(heavy)
        self.assertLessEqual(result["risk"], risk_config.MAX_DAILY_LOSS)
        self.assertTrue(any("max_daily_loss" in d["dropped_because"]
                            for d in result["dropped"]))

    def test_total_notional_never_exceeds_max_total_capital(self):
        many = [candidate(f"S{i}", entry=1000.0, stop=999.0, size=25,
                          turnover=1e9 - i) for i in range(20)]
        result = signals.assemble_portfolio(many)
        deployed = sum(c["entry"] * c["size"] for c in result["portfolio"])
        self.assertLessEqual(deployed, risk_config.MAX_TOTAL_CAPITAL)

    def test_per_symbol_notional_is_trimmed_not_dropped(self):
        """An oversized position is resized to fit rather than discarded — the setup
        is still valid, only the quantity was wrong."""
        oversized = candidate("AAA", entry=1000.0, stop=990.0, size=1000)  # 1,000,000
        result = signals.assemble_portfolio([oversized])
        kept = result["portfolio"][0]
        self.assertLessEqual(kept["entry"] * kept["size"], risk_config.CAPITAL_PER_TRADE)
        self.assertGreater(kept["size"], 0)

    def test_resizing_keeps_derived_amounts_consistent(self):
        """A trimmed position must not keep the risk and upside of its old size."""
        oversized = candidate("AAA", entry=1000.0, stop=990.0, target=1020.0, size=1000)
        kept = signals.assemble_portfolio([oversized])["portfolio"][0]
        self.assertAlmostEqual(kept["risk"], (kept["entry"] - kept["stop"]) * kept["size"], places=2)
        self.assertAlmostEqual(kept["target_potential"],
                               (kept["target"] - kept["entry"]) * kept["size"], places=2)

    def test_profit_target_caps_surfacing(self):
        rich = [candidate(f"S{i}", entry=1000.0, stop=970.0, target=1200.0, size=20,
                          turnover=1e9 - i) for i in range(10)]
        result = signals.assemble_portfolio(rich)
        self.assertLessEqual(result["target_potential"], risk_config.DAILY_PROFIT_TARGET)

    def test_a_single_oversized_candidate_still_surfaces(self):
        """A cap must not make the best setup on a volatile name unlistable."""
        huge = candidate("AAA", entry=1000.0, stop=970.0,
                         target=1000.0 + risk_config.DAILY_PROFIT_TARGET, size=20)
        result = signals.assemble_portfolio([huge])
        self.assertEqual(len(result["portfolio"]), 1)


class AssemblyContractTestCase(unittest.TestCase):
    def test_every_drop_carries_a_reason(self):
        """A cap that silently removes candidates is indistinguishable from a rule
        that stopped firing, and those need different responses."""
        crowd = [candidate(f"S{i}", turnover=1e9 - i) for i in range(30)]
        result = signals.assemble_portfolio(crowd)
        self.assertTrue(result["dropped"])
        for drop in result["dropped"]:
            self.assertTrue(drop.get("dropped_because"))

    def test_nothing_is_invented(self):
        """Kept plus dropped accounts for every input, once."""
        crowd = [candidate(f"S{i}", turnover=1e9 - i) for i in range(12)]
        result = signals.assemble_portfolio(crowd)
        seen = ([(c["symbol"], c["rule"]) for c in result["portfolio"]]
                + [(d["symbol"], d["rule"]) for d in result["dropped"]])
        self.assertEqual(sorted(seen), sorted((c["symbol"], c["rule"]) for c in crowd))

    def test_deterministic(self):
        """Burst 7's backtest and the live scan must assemble identically, which
        requires the same input to give the same answer every time."""
        crowd = [candidate(f"S{i}", rule=list(signals.RULES)[i % 3], turnover=1e9 - i)
                 for i in range(15)]
        first = signals.assemble_portfolio(list(crowd))
        second = signals.assemble_portfolio(list(reversed(crowd)))
        self.assertEqual([c["symbol"] for c in first["portfolio"]],
                         [c["symbol"] for c in second["portfolio"]])

    def test_empty_input(self):
        result = signals.assemble_portfolio([])
        self.assertEqual(result["portfolio"], [])
        self.assertEqual(result["risk"], 0)

    def test_ordering_is_most_liquid_first(self):
        low = candidate("LOW", turnover=1.0)
        high = candidate("HIGH", turnover=1e12)
        result = signals.assemble_portfolio([low, high])
        self.assertEqual(result["portfolio"][0]["symbol"], "HIGH")


if __name__ == "__main__":
    unittest.main()
