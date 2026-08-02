"""Stage 3 — turns the feature frame into dated, sized, long-only candidates.

Each rule is a pure function of one symbol's features: a mapping in, a direction or
None out. No database, no clock, no state. That is what lets the same three
functions serve the live scan and the backtest without either growing its own
variant, and it is why every threshold lives in rules_config.py rather than in a
rule body — a constant inside a function cannot be swept.

Rows land with status='proposed'. Nothing here decides to trade; journal.py fills.

TWO THINGS ABOUT `entry` THAT ARE EASY TO GET WRONG

A signal is generated after the close and traded at the *next* session's open, so
the actual fill price does not exist yet when the signal is written. `entry` is
therefore the as-of close, used as the planning reference, and journal.py fills at
the real open when it arrives.

It is tempting to store the next open instead — in a backtest it is right there in
the table. Doing so would be lookahead, and not the harmless kind. Size and levels
derive from `entry`, and the profit cap below decides *which candidates surface*
from those sizes. Reaching forward for the open would mean tomorrow's price
choosing which of today's signals you are shown. The estimate is the honest
version; the gap between it and the fill is real slippage, and pretending
otherwise would only hide it.

SIZING DERIVES FROM RISK, NEVER FROM THE TARGET

    size = floor(min(capital_per_trade / entry, max_daily_loss / (entry - stop)))

The first term caps notional, the second caps rupees at risk. daily_profit_target
appears nowhere in it — it is used only to stop listing candidates once their
combined target potential exceeds it. Letting a profit goal size a position is how
a bad day becomes a worse one.
"""

import math
from datetime import datetime, timezone

from src import features, risk_config, rules_config, universe
from src.db import get_connection, init_db
from src.runlog import today

LONG = "long"
SHORT = "short"

STATUS_PROPOSED = "proposed"

MOMENTUM = "momentum_continuation"
REVERSION = "oversold_reversion"
BREAKOUT = "volume_breakout"


# --- rules: pure functions of one symbol's features ---------------------------


def rule_momentum_continuation(f):
    """Strength near its highs, confirmed by volume and an intact trend."""
    close = f.get("close")
    distance = f.get("dist_52w_high")
    above = f.get(f"sma_{rules_config.MOMENTUM_REQUIRE_ABOVE_SMA}")
    volume_ratio = f.get("volume_ratio_20")
    if None in (close, distance, above, volume_ratio):
        return None

    # dist_52w_high is <= 0 by construction, so "within 3%" is a floor on it.
    near_high = distance >= -rules_config.MOMENTUM_MAX_DIST_FROM_52W_HIGH
    if near_high and close > above and volume_ratio > rules_config.MOMENTUM_MIN_VOLUME_RATIO:
        return LONG
    return None


def rule_oversold_reversion(f):
    """A dip inside an uptrend — not a repricing on news."""
    close = f.get("close")
    rsi = f.get("rsi_14")
    trend = f.get(f"sma_{rules_config.REVERSION_TREND_SMA}")
    gap = f.get("gap_pct")
    if None in (close, rsi, trend, gap):
        return None

    if rsi >= rules_config.REVERSION_MAX_RSI:
        return None
    if close <= trend:
        return None
    # An earnings gap means the facts changed. Oversold on new facts is just cheap
    # for a reason, and mean reversion has nothing to revert to.
    if abs(gap) > rules_config.REVERSION_MAX_ABS_GAP:
        return None
    return LONG


def rule_volume_breakout(f):
    """A close through the prior 20-day high, on conviction volume."""
    close = f.get("close")
    prior_high = f.get("prior_high_20")
    volume_ratio = f.get("volume_ratio_20")
    if None in (close, prior_high, volume_ratio):
        return None

    if close > prior_high and volume_ratio > rules_config.BREAKOUT_MIN_VOLUME_RATIO:
        return LONG
    return None


RULES = {
    MOMENTUM: rule_momentum_continuation,
    REVERSION: rule_oversold_reversion,
    BREAKOUT: rule_volume_breakout,
}

ENABLED_RULES = tuple(RULES)
# Long only. A cash-segment short cannot be carried overnight — see costs.py — and
# every signal here is held for days.
ENABLED_DIRECTIONS = (LONG,)


# --- universal gates ----------------------------------------------------------


def has_discontinuity(f):
    """True when the indicator window spans a price cliff.

    An unadjusted split or a demerger leaves a step no average survives: the 200-day
    mean sits between two price regimes that never coexisted. Excluding the symbol
    is the honest response — the indicators cannot be computed from this data, so
    there is no signal to have an opinion about. It lifts by itself once the cliff
    ages out of the lookback window.
    """
    return bool(f) and (f.get("max_jump") or 0) >= features.DISCONTINUITY_THRESHOLD


def is_tradeable(f):
    """Filters that are not a strategy view, applied before any rule runs."""
    close = f.get("close")
    if not close or close < rules_config.MIN_PRICE:
        return False
    daily = f.get("daily_return")
    if daily is not None and abs(daily) > rules_config.MAX_ABS_DAILY_RETURN:
        return False
    return not has_discontinuity(f)


def evaluate(f, rules=None, directions=None):
    """Every enabled rule that fires, as (rule, direction) pairs."""
    if not f or not is_tradeable(f):
        return []
    allowed_rules = ENABLED_RULES if rules is None else rules
    allowed_directions = ENABLED_DIRECTIONS if directions is None else directions
    return [
        (name, direction)
        for name, rule in RULES.items()
        if name in allowed_rules and (direction := rule(f)) in allowed_directions
    ]


# --- levels and size ----------------------------------------------------------


def levels(f, direction=LONG):
    """Entry, stop, target and size for one candidate, or None if it cannot be sized.

    Long-only, so the stop is below and the target above. Both are ATR multiples:
    the same rupee risk then buys a tighter stop on a quiet stock and a wider one on
    a volatile one, which is the entire point of sizing off volatility.
    """
    entry = f.get("close")
    atr = f.get("atr_14")
    if not entry or not atr or direction != LONG:
        return None

    stop = entry - atr * rules_config.STOP_ATR_MULTIPLE
    target = entry + atr * rules_config.TARGET_ATR_MULTIPLE
    risk_per_share = entry - stop
    if risk_per_share <= 0 or stop <= 0:
        return None

    # Two independent caps, both in shares, and the tighter one wins. The profit
    # target is deliberately absent — see the module docstring.
    by_capital = risk_config.CAPITAL_PER_TRADE / entry
    by_risk = risk_config.MAX_DAILY_LOSS / risk_per_share
    size = math.floor(min(by_capital, by_risk))
    if size < risk_config.MIN_SHARES:
        return None

    return {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "size": size,
        "risk": round(risk_per_share * size, 2),
        "target_potential": round((target - entry) * size, 2),
        "bound_by": "capital" if by_capital <= by_risk else "risk",
    }


def rank_key(f):
    """Turnover: close times 20-day average volume.

    Liquidity, not conviction. When the profit cap forces a choice between
    candidates, the one you can actually fill near the quoted price is worth more
    than the one with a marginally prettier setup.
    """
    close = f.get("close") or 0
    volume = f.get("avg_volume_20") or 0
    return close * volume


def cap_by_profit_target(candidates, budget=None):
    """Take candidates, most liquid first, until their combined target potential
    would exceed the daily profit target.

    The target is a *surfacing* limit, never a sizing input. If the day's realistic
    upside is already accounted for, another candidate adds exposure without adding
    reward worth having.

    A candidate whose own potential exceeds the whole budget is still surfaced —
    otherwise the highest-conviction setup on a volatile name would be silently
    unlistable, which is the opposite of what a cap is for.
    """
    budget = risk_config.DAILY_PROFIT_TARGET if budget is None else budget
    kept, used = [], 0.0
    for candidate in candidates:
        potential = candidate["target_potential"]
        if kept and used + potential > budget:
            break
        kept.append(candidate)
        used += potential
    return kept, used


# --- proposing ----------------------------------------------------------------


def propose(conn, as_of=None, symbols=None):
    """Ranked, capped candidates as of a date. Pure read — writes nothing."""
    symbols = tuple(symbols or universe.UNIVERSE)
    frame = features.feature_frame(conn, as_of=as_of, symbols=symbols)

    candidates, excluded, unsizeable = [], [], []
    for row in frame.to_dict("records"):
        if has_discontinuity(row):
            excluded.append((row["symbol"], row.get("max_jump"), row.get("max_jump_date")))
            continue
        for rule, direction in evaluate(row):
            sized = levels(row, direction)
            if not sized:
                unsizeable.append(row["symbol"])
                continue
            candidates.append({
                "symbol": row["symbol"],
                "rule": rule,
                "direction": direction,
                "date": row.get("date"),
                "turnover": rank_key(row),
                **sized,
            })

    candidates.sort(key=lambda c: -c["turnover"])
    kept, used = cap_by_profit_target(candidates)
    return {
        "candidates": kept,
        "dropped_by_cap": candidates[len(kept):],
        "target_potential": used,
        "excluded": excluded,
        "unsizeable": sorted(set(unsizeable)),
        "evaluated": len(frame),
    }


def _already_proposed(conn, date, symbol, rule):
    """Re-running the stage on the same day must not duplicate a row — the table has
    no natural key that would prevent it."""
    return conn.execute(
        "SELECT 1 FROM signals WHERE date = ? AND symbol = ? AND rule = ?",
        (date, symbol, rule),
    ).fetchone() is not None


def run(dry_run=False, symbols=None, as_of=None, **kwargs):
    conn = get_connection()
    try:
        init_db(conn)
        date = features._iso(as_of) if as_of else today()
        result = propose(conn, as_of=as_of, symbols=symbols)

        print(f"[signals] rules: {', '.join(ENABLED_RULES)} / {', '.join(ENABLED_DIRECTIONS)}")
        print(f"[signals] {result['evaluated']} symbol(s) evaluated as of {date}")

        written = 0
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for candidate in result["candidates"]:
            if _already_proposed(conn, date, candidate["symbol"], candidate["rule"]):
                continue
            if not dry_run:
                conn.execute(
                    """INSERT INTO signals
                       (date, symbol, rule, direction, entry, stop, target, size, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (date, candidate["symbol"], candidate["rule"], candidate["direction"],
                     candidate["entry"], candidate["stop"], candidate["target"],
                     candidate["size"], STATUS_PROPOSED, stamp),
                )
            written += 1
        if not dry_run:
            conn.commit()

        suffix = " (dry run, not stored)" if dry_run else ""
        print(f"[signals] {len(result['candidates'])} candidate(s) proposed, {written} new{suffix}")
        for c in result["candidates"]:
            print(f"[signals]   {c['symbol']:<12} {c['rule']:<22} entry {c['entry']:>9.2f} "
                  f"stop {c['stop']:>9.2f} target {c['target']:>9.2f} x{c['size']:<4} "
                  f"risk {c['risk']:>7,.0f} ({c['bound_by']}-bound)")

        print(f"[signals] target potential {result['target_potential']:,.0f} of "
              f"{risk_config.DAILY_PROFIT_TARGET:,} daily target")
        if result["dropped_by_cap"]:
            names = ", ".join(f"{c['symbol']}/{c['rule']}" for c in result["dropped_by_cap"][:6])
            print(f"[signals] {len(result['dropped_by_cap'])} candidate(s) held back by the "
                  f"profit cap (least liquid first): {names}")
        if result["excluded"]:
            print(f"[signals] {len(result['excluded'])} symbol(s) excluded — price "
                  f"discontinuity in the indicator window:")
            for symbol, jump, when in sorted(result["excluded"]):
                print(f"[signals]   {symbol:<12} {jump:.0%} move on {when}")
        if result["unsizeable"]:
            print(f"[signals] {len(result['unsizeable'])} firing(s) too expensive to size at "
                  f"{risk_config.CAPITAL_PER_TRADE:,}/trade: {', '.join(result['unsizeable'][:6])}")
        return written
    finally:
        conn.close()
