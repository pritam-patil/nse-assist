"""Every threshold the signal rules read — committed, and deliberately all in one place.

A number buried in a rule body cannot be tuned honestly. You cannot sweep it, you
cannot diff a change to it, and you cannot tell afterwards whether a backtest
result came from the idea or from a constant somebody nudged. Everything the rules
in signals.py compare against lives here, and signals.py contains no numeric
literals of its own.

Tuning discipline, for whoever runs the sweep:

  - Change one group at a time and re-measure. Moving three thresholds together
    produces a number you cannot attribute.
  - A threshold that only works in a narrow band is not a parameter, it is a
    coincidence. Prefer values that hold over a range.
  - Re-measure net of costs. src/costs.py puts a ~0.29% floor under any edge, and
    a rule that fires more often after loosening a threshold can trade itself into
    a loss while its gross number improves.
  - The point-in-time suite must stay green. A "better" result that arrives with a
    failing test in tests/test_point_in_time.py is lookahead, not alpha.

Provenance of the current values: these are the *stated* starting points, not
tuned ones. They have not been fitted to anything.
"""

# --- rule 1: momentum continuation -------------------------------------------
# Buying strength near its highs, on the theory that a stock making new highs on
# heavy volume keeps going. The trend filter is what stops it buying a spike in a
# downtrend.
MOMENTUM_MAX_DIST_FROM_52W_HIGH = 0.03   # within 3% of the 52-week high
MOMENTUM_MIN_VOLUME_RATIO = 1.5          # vs the 20-day average
MOMENTUM_REQUIRE_ABOVE_SMA = 50          # close must be above this SMA

# --- rule 2: oversold mean-reversion ------------------------------------------
# Buying a dip, but only inside an uptrend — above the 200-day, so the position is
# "this pulled back", not "this is falling".
REVERSION_MAX_RSI = 30.0
REVERSION_TREND_SMA = 200                # close must be above this SMA
# An earnings gap is a repricing, not a dip: the stock is oversold because the
# facts changed, and mean reversion has no reason to apply. Excluded by absolute
# overnight move, which is the only earnings signal available without a calendar.
REVERSION_MAX_ABS_GAP = 0.03

# --- rule 3: volume-spike breakout --------------------------------------------
# A close above the prior 20-day high on conviction volume. The volume floor is
# higher than the momentum rule's because a breakout without participation is the
# classic false one.
BREAKOUT_MIN_VOLUME_RATIO = 2.5
BREAKOUT_LOOKBACK_DAYS = 20              # must match features.BREAKOUT_LOOKBACK

# --- levels -------------------------------------------------------------------
# Stop distance and reward:risk, both in ATR(14) multiples. 1.5 ATR sits outside
# normal daily noise on NIFTY 100 names; 2.0 reward:risk means the rules can be
# wrong more often than right and still pay.
STOP_ATR_MULTIPLE = 1.5
TARGET_ATR_MULTIPLE = 2.0

# --- universal filters --------------------------------------------------------
# Applied to every rule, so a single bad-data symbol cannot reach any of them.
# Kept here rather than inside the rules because they are not a strategy view.
MIN_PRICE = 10.0                         # sub-10-rupee names round badly at any size
MAX_ABS_DAILY_RETURN = 0.20              # a 20% day is news; wait for it to settle

# --- rule ranking -------------------------------------------------------------
# Net expectancy per trade, in rupees, used for two decisions: which rule executes
# when several fire on one symbol, and which candidate is dropped first when a
# cumulative cap binds (smallest edge goes first).
#
# INTERIM — FULL-SAMPLE BURST 7 VALUES, PENDING OVERWRITE BY BURST 8.
#
# Measured, so they beat the zeros they replace: assembly now prefers the one rule
# that makes money, which is right regardless of how the number was obtained.
#
# But they are IN-SAMPLE and therefore systematically rosy. These rules were written
# while looking at this same history, so the thresholds have already been fitted to
# it once, informally, by the person choosing them. Full-sample expectancy measures
# how well a rule describes the past, not how well it predicts. Burst 8's
# out-of-sample values are the honest ones and its persist-on-every-cycle step
# overwrites this block; the distinction is marked here rather than remembered,
# because a number in a config file loses its provenance the moment nobody
# remembers where it came from.
#
# Source: --stage backtest, per-rule isolated replay, 2023-07-13 to 2026-07-31,
# 99 symbols, net of costs and slippage.
RULE_EXPECTANCY_BASIS = "out-of-sample walk-forward"
RULE_EXPECTANCY = {
    "momentum_continuation": -257.2,
    "oversold_reversion": -669.9,
    "volume_breakout": -662.2,
}

# Which rules the live scan may emit. A losing rule is disabled here rather than
# deleted: the record of what was tried and failed is worth more than the tidiness
# of removing it, and a deleted rule gets reinvented in six months by someone who
# does not know it was already measured.
#
# Set by --stage walkforward --apply from out-of-sample verdicts. Left all-True
# until a walk-forward run has actually judged them.
RULE_ENABLED = {
    "momentum_continuation": False,
    "oversold_reversion": False,
    "volume_breakout": False,
}

# Hit rate each rule achieved in the backtest, for the live-versus-backtest column
# in --stage journal-report. Separate from RULE_EXPECTANCY because a rule can match
# its expected hit rate while missing its expectancy badly, and the two failures
# mean different things: a hit-rate match with an expectancy miss says the entries
# are fine and the exits or costs are not.
#
# INTERIM, FULL-SAMPLE. These come from the Burst 7 per-rule replay over the whole
# history, so they are the optimistic version — the same caveat RULE_EXPECTANCY
# carried before walk-forward overwrote it. Walk-forward should replace them with
# out-of-sample rates, and until it does, a live rate matching these has matched a
# flattered target.
RULE_BACKTEST_HIT_RATE_BASIS = "full-sample Burst 7, interim — not yet out-of-sample"
RULE_BACKTEST_HIT_RATE = {
    "momentum_continuation": 0.472,
    "oversold_reversion": 0.524,
    "volume_breakout": 0.478,
}

# Applied when expectancies tie, which is currently always. A tighter stop means
# less risked per share for the same ATR view, so the same rupee budget buys more
# shares and the position is denser in the setup rather than in the noise.
DEDUPE_TIEBREAK = "tighter_stop"

# --- the evaluation gate: FROZEN ----------------------------------------------
#
# ══════════════════════════════════════════════════════════════════════════════
#  PRE-COMMITTED ON 2026-08-02, BEFORE ANY PAPER TRADE WAS ENTERED.
#  DO NOT EDIT THESE VALUES AFTER EVALUATION BEGINS.
# ══════════════════════════════════════════════════════════════════════════════
#
# Five criteria. All five must hold simultaneously for a PASS. They are frozen
# because the failure mode this gate exists to prevent is not a bad rule — it is a
# good-faith reader looking at a near-miss and deciding the threshold was always a
# little too strict. That decision feels like judgement and is indistinguishable
# from fitting the test to the result.
#
# They were set before any evidence existed, which is the only moment at which it
# is possible to set them honestly.
#
# CHANGING THEM: tests/test_gate.py asserts every value below. An edit here fails
# the suite until the test is edited too, so moving a goalpost costs a second
# deliberate commit that says so in the diff. That is the entire mechanism, and it
# is meant to be annoying.
#
# A FAIL at the end of the window is a SUCCESS OF THIS SYSTEM. It means the gate
# did the job it was built for: the rules go back for another walk-forward cycle,
# or the project stays paper-only. The failure would have been finding out with
# real money.

GATE_FROZEN_ON = "2026-08-02"
GATE_BASIS = "pre-committed, frozen before evaluation began"

# 1. SAMPLE — both, whichever comes later.
#    Six weeks is long enough to span more than one market mood and short enough
#    to be a real deadline. 30 trades is the same evidence floor the walk-forward
#    uses, and it is usually the binding one: a rule can sit through six weeks and
#    still have nothing to say if it barely fired.
EVALUATION_WEEKS_REQUIRED = 6
EVALUATION_DAYS_REQUIRED = EVALUATION_WEEKS_REQUIRED * 7   # 42
EVALUATION_MIN_TRADES = 30

# 2. CUMULATIVE P&L — strictly positive, after costs.
#    Zero is a fail. Break-even means the rules paid for their own transaction
#    costs and nothing else, which is not a reason to risk money.
GATE_MIN_CUMULATIVE_PNL = 0.0

# 3. EXPECTANCY PER TRADE — strictly positive.
#    Separate from cumulative P&L because one large winner can carry a losing
#    process. Both must hold.
GATE_MIN_EXPECTANCY = 0.0

# 4. HIT-RATE DRIFT — live vs backtest, below 15 percentage points.
#    A larger gap means one of them is wrong about the market. On a sample this
#    size that is the backtest, and a backtest that mis-describes the past has no
#    standing to describe the future.
HIT_RATE_DRIFT_FLAG = 0.15
GATE_MAX_HIT_RATE_DRIFT = HIT_RATE_DRIFT_FLAG

# 5. AGAINST THE INDEX — paper P&L at least the NIFTY return over the same days.
#    The comparison the strategy has to win. Buy-and-hold costs one round trip and
#    no attention; anything that underperforms it has charged you its transaction
#    costs and your evenings for the privilege. Ties pass — matching the index
#    while holding cash most of the time is a real result.
GATE_BEAT_BENCHMARK = True

# Kept for the older EVALUATION_BASIS references in reports.
EVALUATION_BASIS = GATE_BASIS

# --- the sentiment graduation gate: FROZEN ------------------------------------
#
# ══════════════════════════════════════════════════════════════════════════════
#  PRE-COMMITTED 2026-08-02, BEFORE THE FIRST SENTIMENT SCORE WAS STORED.
# ══════════════════════════════════════════════════════════════════════════════
#
# The sentiment layer observes and does not act. These are the conditions under
# which it would become worth DESIGNING an acting role — not the conditions for
# giving it one. Clearing this bar buys a design discussion, nothing more.
#
# Both must hold:
#   1. at least SENTIMENT_MIN_ANNOTATED_TRADES closed trades carrying a score
#   2. a visible outcome difference between the negative-sentiment cohort and the
#      rest — the gap has to be large enough to matter, in the direction that
#      would make a veto useful (negative sentiment doing WORSE)
#
# 60 rather than the paper gate's 30 because this is a subgroup analysis: the
# question is about the negative tercile, which is a third of the sample, so the
# sample has to be bigger for the subgroup to contain anything.
#
# IF IT GRADUATES IT ENTERS AS A VETO ONLY. Never a signal generator. A layer that
# can only remove candidates can be evaluated against the counterfactual of not
# removing them; one that can propose them has changed the strategy into a
# different strategy whose backtest does not exist.
#
# Pinned by tests/test_sentiment.py on the same principle as the paper gate.
SENTIMENT_GATE_FROZEN_ON = "2026-08-02"
SENTIMENT_MIN_ANNOTATED_TRADES = 60
# Expectancy gap, in rupees per trade, between the negative cohort and the rest.
# Stated as a magnitude the eye can check rather than a p-value: on a sample of 60
# with this variance, a significance test is a coin flip dressed as arithmetic.
SENTIMENT_MIN_COHORT_GAP = 200.0
SENTIMENT_ROLE_IF_GRADUATED = "veto-only filter, evaluated in its own right"

# --- surfacing ----------------------------------------------------------------
# Candidates are ranked before the profit cap is applied, so the cap keeps the best
# of them rather than whichever the scan happened to reach first. Turnover, because
# a signal you cannot fill at a sane price is not a signal.
RANK_BY = "turnover"                     # close * 20-day average volume


def as_dict():
    """The committed values, for the doctor stage and for stamping a backtest."""
    return {
        name: value
        for name, value in sorted(globals().items())
        if name.isupper() and not name.startswith("_")
    }


def assert_consistent():
    """Catches threshold edits that are individually plausible but jointly wrong."""
    from src import features

    if BREAKOUT_LOOKBACK_DAYS != features.BREAKOUT_LOOKBACK:
        raise RuntimeError(
            f"breakout lookback disagrees: rules_config says {BREAKOUT_LOOKBACK_DAYS}, "
            f"features computes {features.BREAKOUT_LOOKBACK}"
        )
    if REVERSION_TREND_SMA not in features.SMA_PERIODS:
        raise RuntimeError(f"no SMA_{REVERSION_TREND_SMA} is computed by features.py")
    if MOMENTUM_REQUIRE_ABOVE_SMA not in features.SMA_PERIODS:
        raise RuntimeError(f"no SMA_{MOMENTUM_REQUIRE_ABOVE_SMA} is computed by features.py")
    if TARGET_ATR_MULTIPLE <= STOP_ATR_MULTIPLE:
        raise RuntimeError(
            f"reward:risk is {TARGET_ATR_MULTIPLE / STOP_ATR_MULTIPLE:.2f} — a target "
            f"nearer than the stop needs a win rate above 50% just to break even"
        )
    missing = set(RULE_EXPECTANCY) ^ {"momentum_continuation", "oversold_reversion", "volume_breakout"}
    if missing:
        raise RuntimeError(f"RULE_EXPECTANCY does not cover exactly the rules: {sorted(missing)}")
    if not 0 < REVERSION_MAX_RSI < 50:
        raise RuntimeError("reversion_max_rsi outside a sane oversold band")
    return (
        f"expectancy {RULE_EXPECTANCY_BASIS}, "
        f"{TARGET_ATR_MULTIPLE / STOP_ATR_MULTIPLE:.1f}:1 reward:risk, "
        f"stop {STOP_ATR_MULTIPLE} ATR, momentum vol {MOMENTUM_MIN_VOLUME_RATIO}x, "
        f"breakout vol {BREAKOUT_MIN_VOLUME_RATIO}x, reversion RSI {REVERSION_MAX_RSI:.0f}"
    )
