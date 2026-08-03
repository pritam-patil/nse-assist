"""Walk-forward validation: tune in-sample, judge out-of-sample, report honestly.

    python main.py --stage walkforward            # measure and report
    python main.py --stage walkforward --apply    # also write verdicts to config

A full-sample backtest measures how well a rule describes the past. It cannot
measure whether the rule predicts, because the thresholds were chosen by someone
looking at that same past. This stage separates the two: thresholds are fitted on
a 12-month in-sample window, then judged on the following 6 months that the fitting
never touched, and the window rolls forward.

THE GRID IS SCALE x RATIO, NOT STOP x TARGET

    stop   = scale x ATR
    target = scale x ratio x ATR

Nine cells either way, but these axes are economically separable. `scale` is noise
tolerance: how much ordinary wiggle the position absorbs before being stopped.
`ratio` is payoff geometry: how often you must be right to break even, which is
1/(1+ratio) and nothing else. A raw stop-by-target cross-product mixes the two, so
a result along it cannot be read.

THE HORIZON IS TIED TO GEOMETRY, NOT SWEPT

    horizon = 10 sessions x (target ATR-distance / 2.0)

A fixed horizon would confound the ratio axis: a wider target needs longer to be
reached, so wide-ratio cells would be penalised by truncation rather than judged on
payoff. Scaling the horizon with the target distance keeps the axis clean. It is
derived, not a tenth dimension.

PLATEAUS, NOT PEAKS

One positive cell surrounded by negative neighbours is noise wearing a costume: it
says the fitting window happened to contain a configuration that worked, not that
the configuration works. Selection therefore scores each cell together with its
orthogonal neighbours, so a cell only wins by sitting in a region that holds up.

SURVIVAL

A rule survives only if its out-of-sample expectancy is positive after costs in a
MAJORITY of windows AND its combined out-of-sample return beats NIFTY buy-and-hold
over the same span. Under MIN_TRADES_FOR_EVIDENCE trades the verdict is
"insufficient evidence" regardless of the numbers — six profitable trades in three
years is an anecdote.
"""

import argparse
from datetime import date

from src import backtest, features, signals, universe
from src.db import get_connection, init_db

# scale = noise tolerance, ratio = payoff geometry. See the docstring.
SCALES = (1.0, 1.5, 2.0)
RATIOS = (1.5, 2.0, 2.5)

# The horizon that the reference geometry earns, and the target distance it is
# defined against. horizon = BASE_HORIZON x (target_distance / REFERENCE_TARGET).
BASE_HORIZON_DAYS = 10
REFERENCE_TARGET_ATR = 2.0

IN_SAMPLE_MONTHS = 12
OUT_OF_SAMPLE_MONTHS = 6
STEP_MONTHS = 6

# Below this, a result is an anecdote. Nothing about a hit rate or a profit factor
# is meaningful on a handful of trades, and a grid search over small samples finds
# a winning cell every time by construction.
MIN_TRADES_FOR_EVIDENCE = 30

# A fold whose out-of-sample stretch got truncated by the end of history is not a
# window, it is a fragment. Counting an 18-day tail as one vote in "positive in a
# majority of windows" would let a fortnight outvote six months.
MIN_OOS_COVERAGE = 0.75


def cells():
    """The nine geometries, each with its derived horizon and break-even hit rate."""
    out = []
    for scale in SCALES:
        for ratio in RATIOS:
            target = scale * ratio
            out.append({
                "scale": scale,
                "ratio": ratio,
                "stop_multiple": scale,
                "target_multiple": target,
                "horizon": max(1, round(BASE_HORIZON_DAYS * target / REFERENCE_TARGET_ATR)),
                # Break-even depends only on the ratio: win 1 unit r times, lose 1
                # unit (1-r) times, solve for r.
                "breakeven_hit": 1.0 / (1.0 + ratio),
            })
    return out


def _add_months(day, months):
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    return date(year, month, min(day.day, 28))


def windows(first, last, in_months=IN_SAMPLE_MONTHS, out_months=OUT_OF_SAMPLE_MONTHS,
            step_months=STEP_MONTHS):
    """Rolling (in-sample, out-of-sample) date ranges covering the history."""
    first, last = date.fromisoformat(first), date.fromisoformat(last)
    folds, start = [], first
    while True:
        is_end = _add_months(start, in_months)
        oos_end = _add_months(is_end, out_months)
        if is_end >= last:
            break
        folds.append({
            "is_start": start.isoformat(),
            "is_end": is_end.isoformat(),
            "oos_start": is_end.isoformat(),
            "oos_end": min(oos_end, last).isoformat(),
        })
        if oos_end >= last:
            break
        start = _add_months(start, step_months)

    # Drop a truncated tail fold rather than letting it vote at full weight.
    full = (_add_months(first, out_months) - first).days * MIN_OOS_COVERAGE
    return [
        f for f in folds
        if (date.fromisoformat(f["oos_end"]) - date.fromisoformat(f["oos_start"])).days >= full
    ]


# --- evaluating one cell ------------------------------------------------------


def resize(firing, cell):
    """Re-derive levels for one firing under a grid cell's geometry.

    Rule *entry* thresholds are not swept, so which firings exist never changes —
    only what is done with them. That is what makes nine cells across four windows
    affordable: the feature scan happens once.
    """
    sized = signals.levels(
        firing["_features"], firing["direction"],
        stop_multiple=cell["stop_multiple"], target_multiple=cell["target_multiple"],
    )
    if not sized:
        return None
    return {**firing, **sized}


def replay(firings, bars_by_symbol, rule, cell, start, end):
    """One rule, one cell, one date range. One position per symbol at a time."""
    trades = []
    busy_until = {}
    for firing in firings:
        if firing["rule"] != rule or not (start <= firing["date"] < end):
            continue
        symbol = firing["symbol"]
        if firing["date"] <= busy_until.get(symbol, ""):
            continue
        sized = resize(firing, cell)
        if not sized:
            continue
        trade, _ = backtest.simulate_position(
            bars_by_symbol[symbol], firing["index"], sized, max_hold=cell["horizon"]
        )
        if trade is None:
            continue
        trades.append(trade)
        busy_until[symbol] = trade["exit_date"]
    return trades


def plateau_scores(grid_results):
    """Each cell scored with its orthogonal neighbours.

    A peak is a cell that beats its neighbours by a lot; a plateau is a region that
    is uniformly decent. Only the plateau survives contact with data the fitting
    never saw, because a peak is usually the fitting window's idiosyncrasy. Scoring
    a cell alongside the cells adjacent in scale and in ratio makes the selection
    prefer regions without needing a separate smoothing pass.
    """
    scored = {}
    for (scale, ratio), stats in grid_results.items():
        neighbours = [
            grid_results[(s, r)]["expectancy"]
            for (s, r) in grid_results
            if (s == scale and abs(RATIOS.index(r) - RATIOS.index(ratio)) == 1)
            or (r == ratio and abs(SCALES.index(s) - SCALES.index(scale)) == 1)
        ]
        window = [stats["expectancy"]] + neighbours
        scored[(scale, ratio)] = {
            "own": stats["expectancy"],
            "plateau": round(sum(window) / len(window), 2),
            "neighbours_positive": sum(1 for n in neighbours if n > 0),
            "neighbours": len(neighbours),
            "trades": stats["trades"],
        }
    return scored


def select_cell(grid_results):
    """Pick the in-sample winner: best plateau score among cells that are themselves
    positive and carry enough trades to mean anything."""
    scored = plateau_scores(grid_results)
    eligible = [
        (key, value) for key, value in scored.items()
        if value["own"] > 0 and value["trades"] >= MIN_TRADES_FOR_EVIDENCE
    ]
    if not eligible:
        return None, scored
    key, _ = max(eligible, key=lambda item: (item[1]["plateau"], item[1]["own"]))
    return key, scored


# --- the stage ----------------------------------------------------------------


def evaluate(conn, symbols=None):
    symbols = tuple(symbols or universe.UNIVERSE)
    features.clear_cache()

    bars_by_symbol = {
        symbol: bars
        for symbol, bars in backtest.load_universe_bars(conn, symbols).items()
        if len(bars) >= features.MIN_BARS + 2
    }
    firings = scan_with_features(bars_by_symbol)
    if not firings:
        raise RuntimeError("no rule fired anywhere in history")

    first = min(f["date"] for f in firings)
    last = max(f["date"] for f in firings)
    folds = windows(first, last)

    grid = cells()
    results = {rule: [] for rule in signals.RULES}

    for fold in folds:
        for rule in signals.RULES:
            in_sample = {
                (c["scale"], c["ratio"]): backtest.summarize(
                    replay(firings, bars_by_symbol, rule, c, fold["is_start"], fold["is_end"])
                )
                for c in grid
            }
            chosen, scored = select_cell(in_sample)
            if chosen is None:
                results[rule].append({"fold": fold, "chosen": None, "scored": scored,
                                      "oos": backtest.summarize([]), "oos_trades": []})
                continue
            cell = next(c for c in grid if (c["scale"], c["ratio"]) == chosen)
            oos_trades = replay(firings, bars_by_symbol, rule, cell,
                                fold["oos_start"], fold["oos_end"])
            results[rule].append({
                "fold": fold, "chosen": chosen, "cell": cell, "scored": scored,
                "oos": backtest.summarize(oos_trades), "oos_trades": oos_trades,
            })
    return results, folds, bars_by_symbol


def scan_with_features(bars_by_symbol, warmup=None):
    """Every firing, carrying the feature snapshot its levels are derived from.

    backtest.scan_history() bakes levels in at the config geometry; the grid needs
    to re-derive them per cell, so the features come along for the ride.
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
                firings.append({
                    "symbol": symbol, "rule": rule, "direction": direction,
                    "date": as_of, "index": index,
                    "turnover": signals.rank_key(computed),
                    "_features": computed,
                })
    firings.sort(key=lambda f: (f["date"], -f["turnover"]))
    return firings


def verdicts(conn, results):
    """Survive or not, per rule, with the reason spelled out."""
    out = {}
    for rule, folds in results.items():
        judged = [f for f in folds if f["chosen"] is not None]
        positive = [f for f in judged if f["oos"]["expectancy"] > 0]
        all_trades = [t for f in folds for t in f["oos_trades"]]
        combined = backtest.summarize(all_trades)

        index_return = None
        if all_trades:
            start = min(t["entry_date"] for t in all_trades)
            end = max(t["exit_date"] for t in all_trades)
            backtest.ensure_benchmark(conn)
            index_return = backtest.benchmark_return(conn, start, end)

        from src import risk_config
        strategy_return = combined["net_pnl"] / risk_config.MAX_TOTAL_CAPITAL

        enough = combined["trades"] >= MIN_TRADES_FOR_EVIDENCE
        majority = len(judged) and len(positive) > len(judged) / 2
        beats = index_return is not None and strategy_return > index_return

        if not enough:
            verdict, why = "INSUFFICIENT EVIDENCE", (
                f"{combined['trades']} out-of-sample trades, under the {MIN_TRADES_FOR_EVIDENCE} "
                f"needed for any of these numbers to mean something")
        elif majority and beats:
            verdict, why = "SURVIVES", (
                f"positive in {len(positive)}/{len(judged)} windows and beats the index")
        else:
            reasons = []
            if not majority:
                reasons.append(f"positive in only {len(positive)}/{len(judged)} windows")
            if not beats:
                reasons.append(
                    f"combined {strategy_return:+.1%} vs index "
                    f"{index_return:+.1%}" if index_return is not None else "no index baseline")
            verdict, why = "DISABLE", " and ".join(reasons)

        out[rule] = {
            "verdict": verdict, "why": why, "combined": combined,
            "windows_positive": len(positive), "windows_judged": len(judged),
            "strategy_return": strategy_return, "index_return": index_return,
            "expectancy": combined["expectancy"],
        }
    return out


def report(results, folds, judgements):
    print(f"\n{'=' * 78}\nWALK-FORWARD: tune {IN_SAMPLE_MONTHS}m in-sample, judge "
          f"{OUT_OF_SAMPLE_MONTHS}m out-of-sample, roll {STEP_MONTHS}m\n{'=' * 78}")
    print(f"grid: scale {SCALES} x ratio {RATIOS}  "
          f"(stop = scale x ATR, target = scale x ratio x ATR)")
    print(f"horizon derived: {BASE_HORIZON_DAYS} sessions x target/{REFERENCE_TARGET_ATR} "
          f"— not a grid axis, so truncation cannot confound the ratio\n")

    for rule, entries in results.items():
        print(f"--- {rule} " + "-" * (74 - len(rule)))
        print(f"  {'in-sample window':<26} {'chosen':<16} {'OOS trades':>10} {'OOS exp':>9} "
              f"{'OOS net':>10} {'hit':>7}")
        for entry in entries:
            fold = entry["fold"]
            label = f"{fold['is_start']}..{fold['is_end']}"
            if entry["chosen"] is None:
                print(f"  {label:<26} {'no positive cell':<16} {'-':>10} {'-':>9} {'-':>10} {'-':>7}")
                continue
            scale, ratio = entry["chosen"]
            oos = entry["oos"]
            print(f"  {label:<26} {f'scale {scale} r{ratio}':<16} {oos['trades']:>10} "
                  f"{oos['expectancy']:>9,.0f} {oos['net_pnl']:>10,.0f} "
                  f"{oos['hit_rate'] * 100:>6.1f}%")
        judgement = judgements[rule]
        combined = judgement["combined"]
        print(f"  combined OOS: {combined['trades']} trades, expectancy "
              f"{combined['expectancy']:,.0f}, net {combined['net_pnl']:,.0f}, "
              f"return {judgement['strategy_return']:+.1%}"
              + (f" vs index {judgement['index_return']:+.1%}"
                 if judgement["index_return"] is not None else ""))
        print(f"  VERDICT: {judgement['verdict']} — {judgement['why']}\n")

    print("=" * 78)
    for rule, judgement in judgements.items():
        flag = "" if judgement["combined"]["trades"] >= MIN_TRADES_FOR_EVIDENCE else "  (thin)"
        print(f"  {judgement['verdict']:<22} {rule:<24} "
              f"expectancy {judgement['expectancy']:>8,.0f}{flag}")
    print("=" * 78 + "\n")


def apply_verdicts(judgements):
    """Rewrite the enabled flags and expectancies in rules_config.py.

    Deliberately behind --apply. A sweep that edits the configuration it just
    measured, in the same run, gives you no moment to look at the verdicts before
    they take effect.
    """
    import pathlib
    import re

    path = pathlib.Path(__file__).with_name("rules_config.py")
    text = path.read_text()

    from src import rules_config

    enabled = {rule: judgement["verdict"] == "SURVIVES" for rule, judgement in judgements.items()}

    # A pipeline test lives in RULE_ENABLED, which this function rewrites wholesale
    # from fresh verdicts. That is correct — the test SHOULD end when the rules are
    # re-judged — but it would otherwise end silently, weeks later, with nobody
    # remembering the flag had been set deliberately.
    ending = [r for r in rules_config.active_pipeline_tests() if not enabled.get(r, False)]
    if ending:
        print(f"[walkforward] NOTE: this disables {', '.join(ending)}, which "
              f"{'was' if len(ending) == 1 else 'were'} enabled as a pipeline test "
              f"since {rules_config.PIPELINE_TEST_SINCE}. The test ends here — clear "
              f"PIPELINE_TEST_RULES too if it is finished.")
    expectancy = {rule: round(judgement["expectancy"], 1) for rule, judgement in judgements.items()}
    # Out-of-sample hit rates, persisted for the same reason the expectancies are.
    # Before this they were computed, printed once, and thrown away — leaving the
    # weekly drift column and the frozen gate's criterion 4 comparing live results
    # against a full-sample number the config itself calls "not yet out-of-sample".
    # A frozen criterion judged against an unfrozen, acknowledged-optimistic target
    # is not frozen.
    hit_rate = {rule: round(judgement["combined"]["hit_rate"], 4)
                for rule, judgement in judgements.items()}

    text = re.sub(
        r"RULE_ENABLED = \{[^}]*\}",
        "RULE_ENABLED = {\n" + "".join(
            f'    "{r}": {v},\n' for r, v in enabled.items()) + "}",
        text, count=1)
    text = re.sub(
        r"RULE_EXPECTANCY = \{[^}]*\}",
        "RULE_EXPECTANCY = {\n" + "".join(
            f'    "{r}": {v},\n' for r, v in expectancy.items()) + "}",
        text, count=1)
    text = re.sub(
        r"RULE_BACKTEST_HIT_RATE = \{[^}]*\}",
        "RULE_BACKTEST_HIT_RATE = {\n" + "".join(
            f'    "{r}": {v},\n' for r, v in hit_rate.items()) + "}",
        text, count=1)
    text = re.sub(r'RULE_EXPECTANCY_BASIS = "[^"]*"',
                  'RULE_EXPECTANCY_BASIS = "out-of-sample walk-forward"', text, count=1)
    text = re.sub(r'RULE_BACKTEST_HIT_RATE_BASIS = "[^"]*"',
                  'RULE_BACKTEST_HIT_RATE_BASIS = "out-of-sample walk-forward"', text, count=1)
    path.write_text(text)
    return enabled, expectancy, hit_rate


def run(dry_run=False, symbols=None, apply=False, **kwargs):
    conn = get_connection()
    try:
        init_db(conn)
        results, folds, _ = evaluate(conn, symbols)
        judgements = verdicts(conn, results)
        report(results, folds, judgements)

        if apply:
            enabled, expectancy, hit_rate = apply_verdicts(judgements)
            print(f"[walkforward] rules_config.py updated: enabled={enabled}")
            print(f"[walkforward] RULE_EXPECTANCY={expectancy} (basis: out-of-sample)")
            print(f"[walkforward] RULE_BACKTEST_HIT_RATE={hit_rate} (basis: out-of-sample)")
        else:
            print("[walkforward] nothing written — re-run with --apply to update "
                  "rules_config.py from these verdicts")
        return judgements
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    run(apply=parser.parse_args().apply)
