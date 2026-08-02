"""Risk parameters — committed, not environment-driven.

These are decisions, not credentials, so they belong in git history where a change
is reviewable ("raised capital_per_trade from 25k to 40k on this date") and where a
backtest can be re-run against the exact limits that were live at the time.

All amounts are INR.

    >>> python main.py --stage doctor      # prints these back, so a bad edit is caught early
"""

# Notional deployed per position. Position size is derived from this and the stop
# distance, so a wider stop buys fewer shares rather than risking more money.
CAPITAL_PER_TRADE = 25_000

# Hard stop for the day. Once realised + open loss crosses this, journal.py marks
# the session done and signals.py stops emitting new entries until tomorrow.
MAX_DAILY_LOSS = 2_500

# The other side of the same switch: hit this and the day is over too. Giving back
# a good morning is the more expensive of the two failure modes.
DAILY_PROFIT_TARGET = 5_000

# Most positions the ledger may hold at once. Caps total exposure at
# MAX_OPEN_POSITIONS * CAPITAL_PER_TRADE regardless of how many rules fire.
MAX_OPEN_POSITIONS = 5

# Ceiling on notional deployed across every open position at once.
#
# MAX_OPEN_POSITIONS bounds the *count* and CAPITAL_PER_TRADE bounds each one, so
# their product is the implied maximum — stating it separately makes it a decision
# rather than an accident of two other numbers, and lets you hold five positions
# while refusing to have more than this much of your capital in the market.
MAX_TOTAL_CAPITAL = 125_000

# Fraction of CAPITAL_PER_TRADE risked between entry and stop.
#
# Derived, not picked: MAX_DAILY_LOSS / MAX_OPEN_POSITIONS / CAPITAL_PER_TRADE
# = 2500 / 5 / 25000 = 2%, i.e. a full book stopped out on the same day lands exactly
# on the daily loss limit. The three numbers stay consistent that way instead of
# quietly contradicting each other. assert_coherent() below enforces the
# relationship if any of the inputs is edited.
RISK_PER_TRADE_FRACTION = 0.02

# Below this, a signal is dropped rather than taken: one share is the smallest
# tradable unit, so a position that small has a realised risk set by the share price
# rather than by RISK_PER_TRADE_FRACTION. Raising it to 2 or 3 shrinks the tradable
# universe further but makes every position that *is* taken carry the intended risk.
# `--stage doctor` reports how much of the universe clears this at current capital.
MIN_SHARES = 1

# Stop distance in ATR(14) multiples, and the reward:risk the target is placed at.
# 1.5 ATR is wide enough to sit outside normal daily noise on these names.
ATR_STOP_MULTIPLE = 1.5
REWARD_RISK_RATIO = 2.0

# A signal not acted on within this many sessions is stale — the setup that
# justified it has moved on. journal.py expires those rather than filling them late.
SIGNAL_VALID_SESSIONS = 2


def max_shares(entry_price, stop_price):
    """Position size: the smaller of the risk-derived and the capital-derived count.

    Risk-derived keeps any single loss near RISK_PER_TRADE_FRACTION of the trade's
    capital; capital-derived stops a low-priced stock from consuming an outsized
    notional. Returns 0 when the stop is at or through the entry, which is the
    caller's cue to drop the signal rather than guess a size.
    """
    risk_per_share = abs(entry_price - stop_price)
    if entry_price <= 0 or risk_per_share <= 0:
        return 0
    by_risk = (CAPITAL_PER_TRADE * RISK_PER_TRADE_FRACTION) / risk_per_share
    by_capital = CAPITAL_PER_TRADE / entry_price
    return int(min(by_risk, by_capital))


def assert_coherent():
    """Catches the parameter edits that are individually reasonable but jointly wrong.

    Editing MAX_DAILY_LOSS without RISK_PER_TRADE_FRACTION is the easy mistake: the
    limit moves but position sizes do not, so either the cap becomes unreachable or a
    single bad morning blows through it. Called by the doctor stage.
    """
    risk_per_position = CAPITAL_PER_TRADE * RISK_PER_TRADE_FRACTION
    book_risk = risk_per_position * MAX_OPEN_POSITIONS

    if risk_per_position <= 0:
        raise RuntimeError("capital_per_trade * risk_per_trade_fraction must be positive")
    # Two-thirds to double the cap. Tighter than that and the loss limit can never
    # actually be hit; looser and one round of stops overshoots it badly.
    if not 0.66 <= book_risk / MAX_DAILY_LOSS <= 2.0:
        raise RuntimeError(
            f"a fully stopped-out book risks {book_risk:,.0f} against a "
            f"{MAX_DAILY_LOSS:,} daily loss limit — reconcile "
            f"risk_per_trade_fraction, max_open_positions and max_daily_loss"
        )
    if DAILY_PROFIT_TARGET < risk_per_position:
        raise RuntimeError("daily_profit_target is smaller than the risk on a single trade")

    # Both daily limits are evaluated against P&L *realised that day*, and only an
    # open position can realise anything — so MAX_OPEN_POSITIONS caps how far the
    # day's number can travel in either direction. A limit set beyond that ceiling
    # is not a conservative limit, it is a disabled one: the circuit breaker reads
    # as armed in the config and can never trip. This is the failure mode that looks
    # safest on paper, which is exactly why it is worth failing the doctor stage.
    best_day = risk_per_position * REWARD_RISK_RATIO * MAX_OPEN_POSITIONS
    if DAILY_PROFIT_TARGET > best_day:
        raise RuntimeError(
            f"daily_profit_target {DAILY_PROFIT_TARGET:,} can never be reached — at most "
            f"{MAX_OPEN_POSITIONS} position(s) can close in a day, worth "
            f"{best_day:,.0f} if every one hits its target"
        )
    if MAX_DAILY_LOSS > book_risk:
        raise RuntimeError(
            f"max_daily_loss {MAX_DAILY_LOSS:,} can never be reached — at most "
            f"{MAX_OPEN_POSITIONS} position(s) can close in a day, risking {book_risk:,.0f}"
        )

    return (
        f"{risk_per_position:,.0f}/position, {book_risk:,.0f} full book vs "
        f"{MAX_DAILY_LOSS:,} cap, {best_day:,.0f} best day vs {DAILY_PROFIT_TARGET:,} target"
    )


def sizing_coverage(quotes):
    """How much of the universe is actually tradable at the current capital.

    `quotes` is an iterable of (symbol, entry_price, stop_distance). A position that
    rounds below MIN_SHARES cannot be taken at all, and one that rounds to exactly
    MIN_SHARES carries whatever risk one share happens to be rather than the intended
    amount. Both distort a paper record, and both grow as CAPITAL_PER_TRADE shrinks —
    so this is reported rather than left to be discovered from a thin ledger.
    """
    intended = CAPITAL_PER_TRADE * RISK_PER_TRADE_FRACTION
    untradable, minimal, tradable = [], [], []

    for symbol, entry, stop_distance in quotes:
        if entry <= 0 or stop_distance <= 0:
            continue
        shares = max_shares(entry, entry - stop_distance)
        if shares < MIN_SHARES:
            untradable.append(symbol)
        elif shares == MIN_SHARES:
            minimal.append(symbol)
        else:
            tradable.append((symbol, (shares * stop_distance) / intended))

    return {
        "untradable": sorted(untradable),
        "minimal": sorted(minimal),
        "tradable": len(tradable),
        "total": len(untradable) + len(minimal) + len(tradable),
    }


def as_dict():
    """The committed values, for the doctor stage and for stamping backtest runs."""
    return {
        "capital_per_trade": CAPITAL_PER_TRADE,
        "max_total_capital": MAX_TOTAL_CAPITAL,
        "max_daily_loss": MAX_DAILY_LOSS,
        "daily_profit_target": DAILY_PROFIT_TARGET,
        "risk_per_trade_fraction": RISK_PER_TRADE_FRACTION,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "atr_stop_multiple": ATR_STOP_MULTIPLE,
        "reward_risk_ratio": REWARD_RISK_RATIO,
        "signal_valid_sessions": SIGNAL_VALID_SESSIONS,
    }
