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
# THESE ARE PLACEHOLDERS AND ARE NOT MEASURED. Every value is 0.0, which makes the
# ordering fall through to the documented tie-breaks rather than encode a guess.
# Ranking rules by a number somebody invented is worse than not ranking them: it
# looks like evidence. Burst 8 replaces these with out-of-sample results, and until
# it does, no rule is claimed to beat another.
RULE_EXPECTANCY = {
    "momentum_continuation": 0.0,
    "oversold_reversion": 0.0,
    "volume_breakout": 0.0,
}

# Applied when expectancies tie, which is currently always. A tighter stop means
# less risked per share for the same ATR view, so the same rupee budget buys more
# shares and the position is denser in the setup rather than in the noise.
DEDUPE_TIEBREAK = "tighter_stop"

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
        f"{TARGET_ATR_MULTIPLE / STOP_ATR_MULTIPLE:.1f}:1 reward:risk, "
        f"stop {STOP_ATR_MULTIPLE} ATR, momentum vol {MOMENTUM_MIN_VOLUME_RATIO}x, "
        f"breakout vol {BREAKOUT_MIN_VOLUME_RATIO}x, reversion RSI {REVERSION_MAX_RSI:.0f}"
    )
