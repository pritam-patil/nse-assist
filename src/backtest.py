"""Stage 4 — replays the rules over stored history to see what they would have done.

Walk-forward and bar-by-bar: for each day, indicators are computed from bars up to
and including that day only, so a rule never sees a price it could not have seen.
Exits are checked on subsequent bars against the stop and target that were fixed at
entry.

One deliberate pessimism: when a bar's range covers both the stop and the target,
the stop is taken. Without intraday data there is no way to know which came first,
and the optimistic assumption is how a backtest ends up flattering a rule.
"""

from src import features, risk_config, signals, universe
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
                open_trade.update(
                    exit_date=bar["date"],
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    pnl=round((exit_price - open_trade["entry_price"]) * sign * open_trade["size"], 2),
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
    }


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
        header = f"{'rule':<20} {'trades':>7} {'win%':>7} {'total':>12} {'expectancy':>12}"
        print(f"[backtest] {header}")
        for rule in list(signals.RULES) + ["ALL"]:
            subset = all_trades if rule == "ALL" else [t for t in all_trades if t["rule"] == rule]
            stats = summarize(subset)
            print(
                f"[backtest] {rule:<20} {stats['trades']:>7} {stats['win_rate'] * 100:>6.1f}% "
                f"{stats['total_pnl']:>12,.0f} {stats['expectancy']:>12,.0f}"
            )
        return summarize(all_trades)
    finally:
        conn.close()
