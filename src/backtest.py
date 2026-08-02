"""Stage 4 — replays the rules over stored history to see what they would have done.

Walk-forward and bar-by-bar: for each day, indicators are computed from bars up to
and including that day only, so a rule never sees a price it could not have seen.
Exits are checked on subsequent bars against the stop and target that were fixed at
entry.

One deliberate pessimism: when a bar's range covers both the stop and the target,
the stop is taken. Without intraday data there is no way to know which came first,
and the optimistic assumption is how a backtest ends up flattering a rule.
"""

from datetime import date

from src import costs, features, risk_config, signals, universe
from src.db import get_connection, init_db

# Bars a position is held before being closed at market. Without it a trade that
# never reaches either level stays open forever and quietly inflates the win rate
# by never being counted as a loss.
MAX_HOLD_BARS = 20


def simulate_symbol(bars, rule_names=None, max_hold_bars=MAX_HOLD_BARS):
    """Trades one symbol's history. Returns a list of closed-trade dicts."""
    rule_names = rule_names or list(signals.RULES)
    trades = []
    open_trade = None

    for index in range(features.MIN_BARS, len(bars)):
        bar = bars[index]

        if open_trade:
            direction = open_trade["direction"]
            hit_stop = (
                bar["low"] <= open_trade["stop"] if direction == signals.LONG else bar["high"] >= open_trade["stop"]
            )
            hit_target = (
                bar["high"] >= open_trade["target"]
                if direction == signals.LONG
                else bar["low"] <= open_trade["target"]
            )

            exit_price = exit_reason = None
            # Stop first when both are inside the same bar — see module docstring.
            if hit_stop:
                exit_price, exit_reason = open_trade["stop"], "stop"
            elif hit_target:
                exit_price, exit_reason = open_trade["target"], "target"
            elif index - open_trade["entry_index"] >= max_hold_bars:
                exit_price, exit_reason = bar["close"], "time"

            if exit_price is not None:
                sign = 1 if direction == signals.LONG else -1
                gross = (exit_price - open_trade["entry_price"]) * sign * open_trade["size"]
                held = index - open_trade["entry_index"]
                segment = costs.segment_for(held)
                charges = costs.round_trip(
                    open_trade["entry_price"], exit_price, open_trade["size"], segment
                )["total"]
                open_trade.update(
                    exit_date=bar["date"],
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    held_bars=held,
                    segment=segment,
                    # Costs are charged whichever way the trade went — that is the
                    # point of modelling them. A rule with a thin edge can be
                    # profitable gross and lose money net.
                    gross_pnl=round(gross, 2),
                    costs=round(charges, 2),
                    pnl=round(gross - charges, 2),
                    # A cash-segment short cannot be carried overnight; see costs.py.
                    executable=(direction == signals.LONG) or costs.short_is_executable(held),
                )
                trades.append(open_trade)
                open_trade = None
            # One position per symbol at a time: an entry on the same bar as an
            # exit would need the intraday sequence we do not have.
            continue

        ind = features.compute(bars[: index + 1])
        for rule, direction in signals.evaluate(ind):
            if rule not in rule_names:
                continue
            sized = signals.levels(ind, direction)
            if not sized:
                continue
            open_trade = {
                "symbol": bar.get("symbol"),
                "rule": rule,
                "direction": direction,
                "entry_index": index,
                "entry_date": bar["date"],
                "entry_price": sized["entry"],
                "stop": sized["stop"],
                "target": sized["target"],
                "size": sized["size"],
            }
            break

    return trades


def summarize(trades):
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                "best": 0.0, "worst": 0.0, "expectancy": 0.0}

    pnls = [t["pnl"] for t in trades]
    gross = [t.get("gross_pnl", t["pnl"]) for t in trades]
    charged = [t.get("costs", 0.0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / len(pnls)

    return {
        "trades": len(pnls),
        "wins": len(wins),
        "win_rate": round(win_rate, 3),
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(sum(pnls) / len(pnls), 2),
        "best": round(max(pnls), 2),
        "worst": round(min(pnls), 2),
        # Rupees per trade the rule is worth on average — the number that decides
        # whether it is worth running at all.
        "expectancy": round(win_rate * avg_win + (1 - win_rate) * avg_loss, 2),
        "gross_pnl": round(sum(gross), 2),
        "costs": round(sum(charged), 2),
        "not_executable": sum(1 for t in trades if not t.get("executable", True)),
    }


def _print_direction_split(trades):
    """Longs and shorts, separately, plus how many shorts the market would refuse.

    Worth its own block because the two directions are not symmetric in Indian cash
    equity: a long can be carried, a short cannot. A blended total hides both a
    losing side and an unexecutable one.
    """
    for direction in (signals.LONG, signals.SHORT):
        subset = [t for t in trades if t["direction"] == direction]
        if not subset:
            continue
        st = summarize(subset)
        note = ""
        if direction == signals.SHORT and st["not_executable"]:
            note = (f"  <-- {st['not_executable']}/{st['trades']} held overnight, "
                    f"NOT EXECUTABLE in the cash segment")
        print(
            f"[backtest] {direction:<20} {st['trades']:>7} {st['win_rate'] * 100:>6.1f}% "
            f"{st['gross_pnl']:>11,.0f} {st['costs']:>10,.0f} {st['total_pnl']:>11,.0f} "
            f"{st['expectancy']:>7,.0f}{note}"
        )


def _print_survivorship_warning(conn, tested):
    """Every backtest here is run on TODAY's index membership.

    src/universe.py is a snapshot of the current NIFTY 100, so a name that was in
    the index three years ago and was demoted after a bad run is simply absent from
    the test, while a name promoted *because* it did well is present for its whole
    history. The sample is therefore tilted toward companies that did well enough to
    still be in the index — results read slightly better than the same rules would
    have done live.

    Mild here rather than severe: NIFTY 100 turnover is roughly a handful of names
    per semi-annual review, and none of these are delisted-to-zero cases. It is
    still a systematic upward tilt, not noise, so it is printed with every result
    rather than left in a README nobody rereads.
    """
    span = conn.execute("SELECT MIN(date), MAX(date) FROM prices").fetchone()
    years = 0
    if span and span[0] and span[1]:
        years = (date.fromisoformat(span[1]) - date.fromisoformat(span[0])).days / 365.25

    print(
        f"[backtest] NOTE: mild survivorship bias — these {tested} symbol(s) are today's "
        f"NIFTY 100, backtested over ~{years:.1f}y of history."
    )
    print(
        "[backtest]       Names demoted from the index after poor performance are absent, "
        "so results read slightly better than live trading would have."
    )


def run(dry_run=False, symbols=None, **kwargs):
    """Backtests each rule separately, then the portfolio as a whole. Read-only:
    nothing is written to signals or paper_trades."""
    symbols = symbols or universe.UNIVERSE
    conn = get_connection()
    try:
        init_db(conn)
        all_trades = []
        tested = 0

        for symbol in symbols:
            bars = features.load_bars(conn, symbol)
            if len(bars) < features.MIN_BARS + MAX_HOLD_BARS:
                continue
            for bar in bars:
                bar["symbol"] = symbol
            all_trades.extend(simulate_symbol(bars))
            tested += 1

        print(f"[backtest] {tested} symbol(s), risk: {risk_config.as_dict()}")
        _print_survivorship_warning(conn, tested)
        print(f"[backtest] costs: {costs.describe_example()}")
        header = (f"{'rule':<20} {'trades':>7} {'win%':>7} {'gross':>11} {'costs':>10} "
                  f"{'net':>11} {'exp':>7}")
        print(f"[backtest] {header}")
        for rule in list(signals.RULES) + ["ALL"]:
            subset = all_trades if rule == "ALL" else [t for t in all_trades if t["rule"] == rule]
            st = summarize(subset)
            print(
                f"[backtest] {rule:<20} {st['trades']:>7} {st['win_rate'] * 100:>6.1f}% "
                f"{st['gross_pnl']:>11,.0f} {st['costs']:>10,.0f} {st['total_pnl']:>11,.0f} "
                f"{st['expectancy']:>7,.0f}"
            )

        _print_direction_split(all_trades)
        return summarize(all_trades)

    finally:
        conn.close()
