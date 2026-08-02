"""Stage — replays the rules over history, per rule and as a portfolio.

    python main.py --stage backtest

POINT-IN-TIME. Every indicator read goes through features.compute_as_of(), the API
tests/test_point_in_time.py holds to account. Nothing here slices a bar list by
index or reaches for a later row.

ENTRY AND LEVELS COME FROM DIFFERENT PRICES, ON PURPOSE

A signal is generated after the close of day D and filled at the open of D+1. The
fill is what P&L is measured from. But the stop and target stay anchored to the
close-based estimate signals.py computed on D — they are NOT recomputed from the
fill.

That looks like sloppiness and is the opposite. journal.py fills live signals from
levels fixed at signal time, because on the evening of D the next open does not
exist yet. If the backtest re-anchored to the fill it would run different trades
than the journal does from identical signals — different stops, different exits —
and the live-versus-backtest comparison would drift with nothing to reveal it. The
estimate-to-fill gap is shared slippage that both sides carry.

EXITS RESOLVE AGAINST YOU

  stop     low <= stop exits at the stop, unless the day OPENED below it, in which
           case it exits at the open. A gap through a stop does not politely fill at
           your price; it fills at the worse one.
  target   high >= target exits at the target, unless the day opened above it, in
           which case it exits at the open. Same rule, opposite sign.
  both     a day whose range contains stop and target counts as a STOP. Daily bars
           cannot say which came first, and assuming the good one is how a backtest
           learns to flatter a strategy.
  time     neither hit within MAX_HOLD_BARS sessions: exit at that last close.

PER-RULE VERSUS COMBINED

Per-rule replays one strategy alone, which is how you judge the rule. Combined runs
each day's candidates through signals.assemble_portfolio() — the same function the
live scan calls — so dedupe and every cumulative cap apply identically. If the
portfolio backtest assembled differently it would be measuring a strategy nobody
trades, and neither output would reveal it.
"""

from collections import defaultdict
from datetime import date

from src import costs, features, risk_config, rules_config, signals, universe
from src.db import get_connection, init_db

# Sessions a position is held before being closed at market.
MAX_HOLD_BARS = 10

EXIT_STOP = "stop"
EXIT_TARGET = "target"
EXIT_TIME = "time"

# The index, stored alongside the stocks. Every query filters by universe
# membership, so it never leaks into a scan; keeping it in `prices` makes the
# baseline as offline and reproducible as everything else.
BENCHMARK_SYMBOL = "NIFTY50"
BENCHMARK_TICKER = "^NSEI"


# --- benchmark ----------------------------------------------------------------


def ensure_benchmark(conn, start=None, end=None):
    """Fetch the index if it is missing. Best-effort: a missing baseline weakens the
    report, it does not invalidate a single trade."""
    stored = conn.execute(
        "SELECT COUNT(*) FROM prices WHERE symbol = ?", (BENCHMARK_SYMBOL,)
    ).fetchone()[0]
    if stored:
        return stored

    span = conn.execute("SELECT MIN(date), MAX(date) FROM prices").fetchone()
    start = start or (span[0] if span else None)
    end = end or (span[1] if span else None)
    if not start or not end:
        return 0

    try:
        import yfinance as yf

        frame = yf.download([BENCHMARK_TICKER], start=start, end=end, interval="1d",
                            auto_adjust=True, actions=False, group_by="ticker",
                            progress=False, threads=False)
        series = frame[BENCHMARK_TICKER] if hasattr(frame.columns, "levels") else frame
        rows = [
            (BENCHMARK_SYMBOL, stamp.date().isoformat(),
             float(row["Open"]), float(row["High"]), float(row["Low"]),
             float(row["Close"]), int(row["Volume"] or 0), "yfinance-adj")
            for stamp, row in series.iterrows()
            if row["Close"] == row["Close"]
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO prices "
            "(symbol, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        return len(rows)
    except Exception as exc:
        print(f"[backtest] benchmark unavailable ({exc}) — reporting without it")
        return 0


def benchmark_return(conn, start, end):
    """Buy-and-hold over exactly the period the strategy traded."""
    rows = conn.execute(
        "SELECT date, close FROM prices WHERE symbol = ? AND date BETWEEN ? AND ? ORDER BY date",
        (BENCHMARK_SYMBOL, start, end),
    ).fetchall()
    if len(rows) < 2 or not rows[0]["close"]:
        return None
    return rows[-1]["close"] / rows[0]["close"] - 1.0


# --- one position -------------------------------------------------------------


def resolve_exit(bar, stop, target, held, max_hold=MAX_HOLD_BARS):
    """Does this bar close the position, and at what price? (price, reason) or (None, None).

    THE SINGLE DEFINITION OF A FILL. journal.py imports this rather than owning a
    copy, because the moment the two disagree the live-versus-backtest comparison
    stops meaning anything — and it would not fail, it would just quietly compare
    two different strategies. Before this existed journal.py had its own exit loop
    and a 20-session hold against the backtest's 10.

    Order is load-bearing. The stop is tested first, so a bar whose range contains
    both levels resolves as a stop: daily data cannot say which came first, and
    picking the good one flatters every ambiguous day for the life of the strategy.
    A gap through a level fills at the open, which is worse than the stop and better
    than the target — that asymmetry is not a choice, it is what a resting order
    actually does.
    """
    low, high, opening = bar["low"], bar["high"], bar["open"]

    if low is not None and low <= stop:
        return (opening if (opening is not None and opening <= stop) else stop), EXIT_STOP
    if high is not None and high >= target:
        return (opening if (opening is not None and opening >= target) else target), EXIT_TARGET
    if held >= max_hold:
        return bar["close"], EXIT_TIME
    return None, None


def simulate_position(bars, signal_index, plan, max_hold=MAX_HOLD_BARS):
    """Fill at the next open, walk forward, exit by the rules in the docstring.

    Returns (trade, None) or (None, reason) when the position is never taken.
    """
    fill_index = signal_index + 1
    if fill_index >= len(bars):
        return None, "no session after the signal yet"

    fill = bars[fill_index]["open"]
    stop, target, size = plan["stop"], plan["target"], plan["size"]
    if not fill or size <= 0:
        return None, "no fill price"

    # The open can gap past a level fixed the previous evening. Entering below your
    # own stop is not a trade, it is an instant loss you chose to take.
    if fill <= stop:
        return None, "gapped through the stop before entry"
    if fill >= target:
        return None, "gapped past the target before entry"

    window = bars[fill_index:fill_index + max_hold]
    exit_price = exit_reason = exit_date = None
    held = 0

    for offset, bar in enumerate(window):
        held = offset + 1
        exit_price, exit_reason = resolve_exit(bar, stop, target, held, max_hold)
        if exit_price is None:
            continue
        exit_date = bar["date"]
        break

    if exit_price is None:
        # End of the sample, not a time stop: history ran out before the position
        # resolved. Marking it out at the last close keeps an unfinished trade from
        # being silently dropped, which would bias the sample toward trades that
        # happened to finish. journal.py deliberately does NOT do this — a live
        # position with sessions still to run stays open.
        last = window[-1]
        exit_price, exit_reason, exit_date = last["close"], EXIT_TIME, last["date"]
        held = len(window)

    gross = (exit_price - fill) * size
    charges = costs.round_trip(fill, exit_price, size, costs.DELIVERY)["total"]
    return {
        "symbol": plan.get("symbol"),
        "rule": plan.get("rule"),
        "signal_date": bars[signal_index]["date"],
        "entry_date": bars[fill_index]["date"],
        "entry_estimate": plan["entry"],
        "entry_price": round(fill, 2),
        # What the close-to-open gap cost before the trade even began.
        "entry_slippage": round((fill - plan["entry"]) * size, 2),
        "stop": stop,
        "target": target,
        "size": size,
        "exit_date": exit_date,
        "exit_price": round(exit_price, 2),
        "exit_reason": exit_reason,
        "held_bars": held,
        "gross_pnl": round(gross, 2),
        "costs": round(charges, 2),
        "pnl": round(gross - charges, 2),
    }, None


# --- scanning history ---------------------------------------------------------


def load_universe_bars(conn, symbols, as_of=None):
    return {symbol: features.load_bars(conn, symbol, as_of=as_of) for symbol in symbols}


def scan_history(bars_by_symbol, warmup=None):
    """Every rule firing across every symbol and date, sized at signal time.

    Computed once and reused by both replays: the features are identical either way,
    and computing them is nearly the whole cost of this stage.
    """
    warmup = warmup or features.MIN_BARS
    firings = []
    for symbol, bars in bars_by_symbol.items():
        for index in range(warmup, len(bars)):
            as_of = bars[index]["date"]
            computed = features.compute_as_of(bars, as_of)
            if computed is None:
                continue
            for rule, direction in signals.evaluate(computed, rules=tuple(signals.RULES)):
                plan = signals.levels(computed, direction)
                if not plan:
                    continue
                firings.append({
                    "symbol": symbol, "rule": rule, "direction": direction,
                    "date": as_of, "index": index,
                    "turnover": signals.rank_key(computed), **plan,
                })
    firings.sort(key=lambda f: (f["date"], -f["turnover"]))
    return firings


# --- replays ------------------------------------------------------------------


def replay_rule(firings, bars_by_symbol, rule):
    """One strategy alone, one position per symbol at a time.

    Isolation is the point: this answers "is the rule any good" without the
    portfolio's caps deciding which of its signals were taken.
    """
    trades, skipped = [], defaultdict(int)
    busy_until = {}

    for firing in (f for f in firings if f["rule"] == rule):
        symbol = firing["symbol"]
        if firing["date"] <= busy_until.get(symbol, ""):
            skipped["already in a position"] += 1
            continue
        trade, reason = simulate_position(bars_by_symbol[symbol], firing["index"], firing)
        if trade is None:
            skipped[reason] += 1
            continue
        trades.append(trade)
        busy_until[symbol] = trade["exit_date"]
    return trades, dict(skipped)


def replay_portfolio(firings, bars_by_symbol):
    """The whole system: every rule, assembled daily by signals.assemble_portfolio().

    Date-major rather than symbol-major, because the caps are cross-sectional — you
    cannot tell whether a candidate fits the daily loss budget without seeing the
    others competing for it that day.
    """
    by_date = defaultdict(list)
    for firing in firings:
        by_date[firing["date"]].append(firing)

    trades, skipped = [], defaultdict(int)
    open_until = {}

    for day in sorted(by_date):
        open_until = {s: d for s, d in open_until.items() if d > day}
        slots = risk_config.MAX_OPEN_POSITIONS - len(open_until)
        if slots <= 0:
            skipped["position limit reached"] += len(by_date[day])
            continue

        # Identical code to the live scan. If this diverged, the portfolio backtest
        # would be measuring a strategy nobody trades.
        assembled = signals.assemble_portfolio(by_date[day])
        skipped["dropped by assembly"] += len(assembled["dropped"])

        for candidate in assembled["portfolio"]:
            if slots <= 0:
                skipped["position limit reached"] += 1
                continue
            if candidate["symbol"] in open_until:
                skipped["already in a position"] += 1
                continue
            trade, reason = simulate_position(
                bars_by_symbol[candidate["symbol"]], candidate["index"], candidate
            )
            if trade is None:
                skipped[reason] += 1
                continue
            trade["confirming_rules"] = candidate.get("confirming_rules") or []
            trades.append(trade)
            open_until[candidate["symbol"]] = trade["exit_date"]
            slots -= 1
    return trades, dict(skipped)


# --- metrics ------------------------------------------------------------------


def max_drawdown(trades):
    """Deepest peak-to-trough fall of cumulative net P&L, ordered by exit.

    In rupees, not percent: there is no account balance modelled here, only a stream
    of realised trades, and a percentage would need an equity base this stage does
    not have.
    """
    running = peak = worst = 0.0
    for trade in sorted(trades, key=lambda t: t["exit_date"]):
        running += trade["pnl"]
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return round(worst, 2)


def summarize(trades):
    if not trades:
        return {"trades": 0, "wins": 0, "hit_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "expectancy": 0.0, "profit_factor": 0.0, "gross_pnl": 0.0, "costs": 0.0,
                "net_pnl": 0.0, "max_drawdown": 0.0, "avg_held": 0.0}

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "trades": len(pnls),
        "wins": len(wins),
        "hit_rate": round(len(wins) / len(pnls), 4),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        # Mean net P&L per trade — the number that decides whether to run the rule.
        "expectancy": round(sum(pnls) / len(pnls), 2),
        # Rupees won per rupee lost. Infinite when nothing lost, which means a
        # sample too small to trust rather than a perfect strategy.
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "gross_pnl": round(sum(t["gross_pnl"] for t in trades), 2),
        "costs": round(sum(t["costs"] for t in trades), 2),
        "net_pnl": round(sum(pnls), 2),
        "max_drawdown": max_drawdown(trades),
        "avg_held": round(sum(t["held_bars"] for t in trades) / len(trades), 1),
    }


def decompose_exits(trades):
    """Where the money actually went, split by how each trade ended.

    A headline profit factor says a strategy loses; it does not say why, and the two
    likely causes point at opposite fixes. If would-be winners are dying at the time
    stop before reaching target, then widening the target makes truncation WORSE and
    the lever is the holding horizon. If instead the shortfall is gap fills and
    costs, the reward:risk geometry itself is what is broken. Same grid to sweep,
    opposite reading of which combinations mean anything.

    `realized_rr` is the number to compare against the nominal ratio: average win
    over average loss, both as magnitudes. Nominal is what the levels promise;
    realized is what the exits delivered.
    """
    by_reason = defaultdict(list)
    for trade in trades:
        by_reason[trade["exit_reason"]].append(trade)

    breakdown = {}
    for reason, group in by_reason.items():
        pnls = [t["pnl"] for t in group]
        winners = [p for p in pnls if p > 0]
        breakdown[reason] = {
            "trades": len(group),
            "share": round(len(group) / len(trades), 4) if trades else 0.0,
            "wins": len(winners),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "net": round(sum(pnls), 2),
            "avg_gross": round(sum(t["gross_pnl"] for t in group) / len(group), 2),
        }

    # Stop exits that filled BELOW the stop: the gap cost, isolated.
    gapped = [t for t in trades
              if t["exit_reason"] == EXIT_STOP and t["exit_price"] < t["stop"]]
    gap_cost = sum((t["stop"] - t["exit_price"]) * t["size"] for t in gapped)

    # Time exits that were in profit when the clock ran out — winners the horizon
    # truncated before they could reach target.
    truncated = [t for t in trades if t["exit_reason"] == EXIT_TIME and t["gross_pnl"] > 0]

    stats = summarize(trades)
    nominal_rr = rules_config.TARGET_ATR_MULTIPLE / rules_config.STOP_ATR_MULTIPLE
    realized_rr = (abs(stats["avg_win"] / stats["avg_loss"])
                   if stats["avg_loss"] else float("inf"))
    return {
        "by_reason": breakdown,
        "gapped_stops": len(gapped),
        "gap_cost": round(gap_cost, 2),
        "truncated_winners": len(truncated),
        "truncated_gross": round(sum(t["gross_pnl"] for t in truncated), 2),
        "nominal_rr": round(nominal_rr, 2),
        "realized_rr": round(realized_rr, 2),
        "cost_per_trade": round(stats["costs"] / stats["trades"], 2) if stats["trades"] else 0.0,
    }


def print_decomposition(label, trades):
    if not trades:
        return
    d = decompose_exits(trades)
    print(f"[backtest] --- {label}: where the money went ---")
    for reason in (EXIT_TARGET, EXIT_STOP, EXIT_TIME):
        row = d["by_reason"].get(reason)
        if not row:
            continue
        print(f"[backtest]   {reason:<7} {row['trades']:>5} ({row['share']:>5.1%})  "
              f"avg {row['avg_pnl']:>8,.0f}  net {row['net']:>11,.0f}  wins {row['wins']:>4}")
    print(f"[backtest]   reward:risk nominal {d['nominal_rr']:.2f} vs realized "
          f"{d['realized_rr']:.2f}   costs {d['cost_per_trade']:,.0f}/trade")
    print(f"[backtest]   {d['gapped_stops']} stop(s) gapped through, costing "
          f"{d['gap_cost']:,.0f} beyond the stop price")
    print(f"[backtest]   {d['truncated_winners']} winner(s) cut short by the "
          f"{MAX_HOLD_BARS}-session time stop, holding {d['truncated_gross']:,.0f} gross")


def _row(label, stats):
    factor = "inf" if stats["profit_factor"] == float("inf") else f"{stats['profit_factor']:.2f}"
    return (f"{label:<24} {stats['trades']:>6} {stats['hit_rate'] * 100:>6.1f}% "
            f"{stats['avg_win']:>9,.0f} {stats['avg_loss']:>9,.0f} {stats['expectancy']:>8,.0f} "
            f"{factor:>7} {stats['net_pnl']:>11,.0f} {stats['max_drawdown']:>11,.0f}")


# --- stage --------------------------------------------------------------------


def run(dry_run=False, symbols=None, as_of=None, **kwargs):
    symbols = tuple(symbols or universe.UNIVERSE)
    conn = get_connection()
    try:
        init_db(conn)
        features.clear_cache()

        bars_by_symbol = {
            symbol: bars
            for symbol, bars in load_universe_bars(conn, symbols, as_of=as_of).items()
            if len(bars) >= features.MIN_BARS + 2
        }
        if not bars_by_symbol:
            raise RuntimeError("no symbol has enough history — run --stage backfill first")

        print(f"[backtest] {len(bars_by_symbol)} symbol(s), hold <= {MAX_HOLD_BARS} sessions")
        print(f"[backtest] rules: {rules_config.assert_consistent()}")
        print(f"[backtest] costs: {costs.describe_example(risk_config.CAPITAL_PER_TRADE)}")

        firings = scan_history(bars_by_symbol)
        print(f"[backtest] {len(firings):,} rule firing(s) across history")

        header = (f"{'strategy':<24} {'trades':>6} {'hit':>7} {'avg win':>9} {'avg loss':>9} "
                  f"{'exp':>8} {'PF':>7} {'net':>11} {'maxDD':>11}")
        print(f"[backtest] {header}")
        print(f"[backtest] {'-' * len(header)}")

        per_rule = {}
        for rule in signals.RULES:
            trades, _ = replay_rule(firings, bars_by_symbol, rule)
            per_rule[rule] = trades
            print(f"[backtest] {_row(rule, summarize(trades))}")

        portfolio_trades, skipped = replay_portfolio(firings, bars_by_symbol)
        stats = summarize(portfolio_trades)
        print(f"[backtest] {'-' * len(header)}")
        print(f"[backtest] {_row('PORTFOLIO (assembled)', stats)}")

        for rule, trades in per_rule.items():
            print_decomposition(rule, trades)
        print_decomposition("PORTFOLIO", portfolio_trades)

        _print_baseline(conn, portfolio_trades, stats)
        _print_survivorship_warning(len(bars_by_symbol), bars_by_symbol)
        if skipped:
            detail = ", ".join(f"{n} {why}" for why, n in sorted(skipped.items(), key=lambda x: -x[1]))
            print(f"[backtest] signals not taken: {detail}")
        return stats
    finally:
        conn.close()


def _print_baseline(conn, trades, stats):
    """The number to beat. A strategy that underperforms buy-and-hold has cost you
    its own transaction costs plus the time spent running it."""
    if not trades:
        return
    start = min(t["entry_date"] for t in trades)
    end = max(t["exit_date"] for t in trades)
    ensure_benchmark(conn)
    index_return = benchmark_return(conn, start, end)

    deployed = risk_config.MAX_TOTAL_CAPITAL
    strategy_return = stats["net_pnl"] / deployed if deployed else 0.0
    print(f"[backtest] period {start} to {end}")
    print(f"[backtest] strategy {stats['net_pnl']:,.0f} on {deployed:,} max deployed "
          f"= {strategy_return:+.1%}")
    if index_return is None:
        print("[backtest] NIFTY baseline unavailable")
        return
    verdict = "BEATS" if strategy_return > index_return else "LOSES TO"
    print(f"[backtest] NIFTY buy-and-hold over the same period = {index_return:+.1%} "
          f"-> strategy {verdict} the index")


def _print_survivorship_warning(tested, bars_by_symbol):
    """Every backtest here runs on TODAY's index membership.

    src/universe.py is a snapshot of the current NIFTY 100, so a name demoted after
    a bad run is simply absent, while one promoted *because* it did well is present
    for its whole history. A systematic upward tilt, not noise — printed with every
    result rather than left in a README nobody rereads.
    """
    starts = [b[0]["date"] for b in bars_by_symbol.values() if b]
    ends = [b[-1]["date"] for b in bars_by_symbol.values() if b]
    years = 0.0
    if starts and ends:
        years = (date.fromisoformat(max(ends)) - date.fromisoformat(min(starts))).days / 365.25
    print(f"[backtest] NOTE: mild survivorship bias — these {tested} symbol(s) are today's "
          f"NIFTY 100 over ~{years:.1f}y.")
    print("[backtest]       Names demoted from the index after poor performance are absent, "
          "so results read better than live trading would have.")
