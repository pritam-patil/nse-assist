"""Stage 3 — turns indicators into dated, sized signal rows.

A rule is a plain function taking the indicator dict from features.compute() and
returning a direction ('long'/'short') or None. Adding one means writing the
function and adding it to RULES; nothing else changes.

Levels are computed once, here, and stored on the row. Neither the delivery
message nor a later backtest recomputes them, so what you were shown at 9am is
what gets measured.
"""

from datetime import datetime, timezone

from src import features, risk_config, universe
from src.db import get_connection, init_db
from src.runlog import today

LONG = "long"
SHORT = "short"

# Conviction filter applied to every rule, not baked into any one of them.
#
# 1.0 (an average-or-better day), not the 1.2 you might reach for first. Measured
# over ~800 sessions of MARUTI/TITAN/TMPV, the crossover rule fires at a median
# relative volume near 1.0 — a moving-average cross is a slow event, it does not
# coincide with a volume spike the way a breakout does. A 1.2 floor silently
# discarded about two-thirds of all firings. Re-measure before moving this; that
# is what --stage backtest is for.
MIN_REL_VOLUME = 1.0

# RSI band for the pullback rule. 40/60, not the textbook 30/70: a NIFTY 100 name
# in an intact uptrend rarely reaches a true oversold reading, and demanding one
# meant the rule never fired in ~800 sessions of testing.
RSI_PULLBACK_LONG = 40
RSI_PULLBACK_SHORT = 60


def rule_sma_crossover(ind):
    """20/50 crossing, in the direction of the 200. The 200-day filter is what
    keeps this from buying every bounce in a downtrend."""
    needed = (ind["sma_fast"], ind["sma_slow"], ind["prev_sma_fast"], ind["prev_sma_slow"], ind["sma_trend"])
    if any(v is None for v in needed):
        return None

    crossed_up = ind["prev_sma_fast"] <= ind["prev_sma_slow"] and ind["sma_fast"] > ind["sma_slow"]
    crossed_down = ind["prev_sma_fast"] >= ind["prev_sma_slow"] and ind["sma_fast"] < ind["sma_slow"]

    if crossed_up and ind["close"] > ind["sma_trend"]:
        return LONG
    if crossed_down and ind["close"] < ind["sma_trend"]:
        return SHORT
    return None


def rule_pullback_in_trend(ind):
    """A dip inside an intact trend — a different trade from a crossover, and worth
    tracking separately so the backtest can price them apart.

    Three conditions, each doing one job: above the 200-day says the trend is intact,
    below the 20-day says price has actually pulled back into it, and the RSI band
    says the pullback has gone far enough to be worth buying. Requiring price to be
    *above* the 50-day here (the obvious-looking extra confirmation) contradicts the
    pullback itself — that combination fired zero times in ~800 sessions.
    """
    if None in (ind["rsi"], ind["sma_trend"], ind["sma_fast"]):
        return None
    if ind["close"] > ind["sma_trend"] and ind["close"] < ind["sma_fast"] and ind["rsi"] < RSI_PULLBACK_LONG:
        return LONG
    if ind["close"] < ind["sma_trend"] and ind["close"] > ind["sma_fast"] and ind["rsi"] > RSI_PULLBACK_SHORT:
        return SHORT
    return None


RULES = {
    "sma_crossover": rule_sma_crossover,
    "pullback_in_trend": rule_pullback_in_trend,
}


def levels(ind, direction):
    """Entry/stop/target/size for a firing rule, or None when it cannot be sized.

    The stop is ATR-based rather than a fixed percentage: the same rupee risk then
    means a tighter stop on a quiet stock and a wider one on a volatile one, which
    is the whole point of sizing off volatility.
    """
    if not ind["atr"] or not ind["close"]:
        return None

    entry = ind["close"]
    distance = ind["atr"] * risk_config.ATR_STOP_MULTIPLE
    if direction == LONG:
        stop = entry - distance
        target = entry + distance * risk_config.REWARD_RISK_RATIO
    else:
        stop = entry + distance
        target = entry - distance * risk_config.REWARD_RISK_RATIO

    # Below MIN_SHARES there is no position to take — one share is the smallest
    # tradable unit. The caller counts these rather than letting them vanish: at
    # small capital this silently removes the expensive end of the universe, and a
    # paper record with an invisible price bias is worse than no record.
    size = risk_config.max_shares(entry, stop)
    if size < risk_config.MIN_SHARES:
        return None
    return {"entry": entry, "stop": stop, "target": target, "size": size}


def has_discontinuity(ind):
    """True when the indicator window spans a price cliff.

    An unadjusted split or a demerger leaves a step in the series that no average
    survives: the 200-day mean sits between two price regimes that never coexisted,
    the ATR reads a range no session actually traded, and the rules then fire on the
    artefact. Verified cases at the time of writing are TRENT (yfinance applied the
    split factor from the wrong date) and VEDL (a demerger it did not adjust at all).

    Excluding the symbol is the honest response — the indicators cannot be computed
    from this data, so there is no signal to have an opinion about. It lifts by
    itself once the cliff ages out of the lookback window.
    """
    return bool(ind) and (ind.get("max_jump") or 0) >= features.DISCONTINUITY_THRESHOLD


def evaluate(ind):
    """Every rule that fires for one symbol's indicators, as (rule, direction) pairs."""
    if ind is None:
        return []
    # Before any rule runs: a corrupted window cannot produce a trustworthy signal.
    if has_discontinuity(ind):
        return []
    # Conviction filter: a signal on well-below-average volume is usually drift.
    if ind["rel_volume"] is not None and ind["rel_volume"] < MIN_REL_VOLUME:
        return []
    return [(name, direction) for name, rule in RULES.items() if (direction := rule(ind))]


def _already_recorded(conn, date, symbol, rule):
    """Re-running the stage the same day must not duplicate a signal — the table has
    no natural key that would enforce it."""
    row = conn.execute(
        "SELECT 1 FROM signals WHERE date = ? AND symbol = ? AND rule = ?", (date, symbol, rule)
    ).fetchone()
    return row is not None


def run(dry_run=False, symbols=None, **kwargs):
    symbols = symbols or universe.UNIVERSE
    conn = get_connection()
    try:
        init_db(conn)
        date = today()
        created = []
        skipped_for_history = 0
        skipped_for_size = []
        excluded = []

        for symbol in symbols:
            ind = features.compute_for(conn, symbol)
            if ind is None:
                skipped_for_history += 1
                continue

            if has_discontinuity(ind):
                excluded.append((symbol, ind["max_jump"], ind["max_jump_date"]))
                continue

            for rule, direction in evaluate(ind):
                sized = levels(ind, direction)
                if not sized:
                    skipped_for_size.append(symbol)
                    continue
                if _already_recorded(conn, date, symbol, rule):
                    continue

                row = (
                    date,
                    symbol,
                    rule,
                    direction,
                    round(sized["entry"], 2),
                    round(sized["stop"], 2),
                    round(sized["target"], 2),
                    sized["size"],
                    "new",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
                if not dry_run:
                    conn.execute(
                        """INSERT INTO signals
                           (date, symbol, rule, direction, entry, stop, target, size, status, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        row,
                    )
                created.append((symbol, rule, direction, sized["entry"], sized["size"]))

        if not dry_run:
            conn.commit()

        suffix = " (dry run, not stored)" if dry_run else ""
        print(f"[signals] {len(created)} signal(s) on {date}{suffix}")
        for symbol, rule, direction, entry, size in created:
            print(f"[signals]   {symbol:<12} {rule:<18} {direction:<5} entry {entry:>9.2f} x{size}")
        if skipped_for_history:
            print(f"[signals] {skipped_for_history} symbol(s) skipped for thin history")
        if excluded:
            print(
                f"[signals] {len(excluded)} symbol(s) excluded — price discontinuity inside "
                f"the indicator window (unadjusted split or demerger):"
            )
            for symbol, jump, when in sorted(excluded):
                # Magnitude only: max_jump() is absolute, so a signed format would
                # print "+65%" for what was a 65% drop.
                print(f"[signals]   {symbol:<12} {jump:.0%} move on {when}")
        if skipped_for_size:
            names = ", ".join(sorted(set(skipped_for_size)))
            print(
                f"[signals] {len(set(skipped_for_size))} rule firing(s) dropped — "
                f"too expensive to size at {risk_config.CAPITAL_PER_TRADE:,}/trade: {names}"
            )
        return len(created)
    finally:
        conn.close()
