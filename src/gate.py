"""Stage — the evaluation gate: does the paper record justify anything?

    python main.py --stage gate        # standalone
    (folded into the Sunday weekly automatically)

Five pre-committed criteria, frozen in rules_config.py on 2026-08-02 before any
paper trade existed. All five must hold simultaneously for a PASS.

WHY THE THRESHOLDS ARE FROZEN AND WHY THAT MATTERS MORE THAN THE THRESHOLDS

The failure this stage exists to prevent is not a bad rule. It is the entirely
reasonable-sounding conversation you have with yourself in week seven, looking at
an expectancy of −₹40 on 28 trades, in which you notice that 30 was always a bit
arbitrary and the drift limit was maybe tight for a sample this small. Every step
of that reasoning is defensible. The conclusion is fitted to the result.

So the numbers were set at the only moment it was possible to set them honestly —
before there was anything to look at — and tests/test_gate.py pins every one of
them. Moving a goalpost now costs a second commit that says, in the diff, that a
goalpost was moved.

A FAIL IS A SUCCESS OF THE SYSTEM

Worth repeating in the module that computes it, because the week it prints FAIL is
the week nobody wants to read this. A fail means the gate worked: the rules go back
for another walk-forward cycle, or the project stays paper-only forever. Both are
fine outcomes. The bad outcome is a system that says PASS because it was asked
nicely, and the money that follows it.

EVERY CRITERION REPORTS THREE STATES, NOT TWO

pass / fail / insufficient-data. The third is not a soft fail — it is the honest
answer for most of the window, and collapsing it into "fail" would make an
early-and-fine week look identical to a late-and-broken one. It also keeps the
overall verdict at IN PROGRESS rather than FAIL until there is enough evidence to
say anything at all.

TRENDS ARE COMPUTED, NOT REMEMBERED

Each criterion is evaluated twice — as of today and as of a week ago — using the
same point-in-time discipline features.py applies to prices. Nothing is stored, so
a trend cannot drift out of sync with the number it describes.
"""

from datetime import date, timedelta

from src import backtest, deliver, rules_config
from src.db import get_connection, init_db
from src.runlog import today

PASS = "pass"
FAIL = "fail"
INSUFFICIENT = "insufficient"

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_IN_PROGRESS = "IN PROGRESS"

TREND_DAYS = 7

# Printed marks. Words rather than symbols: this message is read on a phone, where
# a green tick and a red cross are four pixels apart and colour-blind readers get
# neither.
MARKS = {PASS: "PASS", FAIL: "FAIL", INSUFFICIENT: "  --"}


def _closed(conn, as_of=None):
    """Closed paper trades at or before `as_of`, oldest first."""
    if as_of:
        rows = conn.execute(
            "SELECT t.pnl, t.exit_date, t.entry_date, s.rule "
            "FROM paper_trades t JOIN signals s ON s.id = t.signal_id "
            "WHERE t.status = 'closed' AND t.exit_date <= ? ORDER BY t.exit_date",
            (as_of,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT t.pnl, t.exit_date, t.entry_date, s.rule "
            "FROM paper_trades t JOIN signals s ON s.id = t.signal_id "
            "WHERE t.status = 'closed' ORDER BY t.exit_date"
        ).fetchall()
    return [dict(r) for r in rows]


def _first_entry(conn, as_of=None):
    if as_of:
        row = conn.execute(
            "SELECT MIN(entry_date) FROM paper_trades WHERE entry_date <= ?", (as_of,)
        ).fetchone()
    else:
        row = conn.execute("SELECT MIN(entry_date) FROM paper_trades").fetchone()
    return row[0] if row else None


def snapshot(conn, as_of=None):
    """Every raw number the criteria read, as of one date.

    One function so today's numbers and last week's come from identical code — a
    trend computed against a differently-derived baseline measures the difference
    between two implementations, not a change in the strategy.
    """
    as_of = as_of or today()
    trades = _closed(conn, as_of)
    first = _first_entry(conn, as_of)

    days = (date.fromisoformat(as_of) - date.fromisoformat(first)).days if first else 0
    net = sum(t["pnl"] or 0 for t in trades)
    expectancy = (net / len(trades)) if trades else None

    # Drift is measured per rule and the worst one binds. Averaging would let a
    # rule that fired twice and matched perfectly cancel one that is badly off.
    drifts = {}
    by_rule = {}
    for trade in trades:
        by_rule.setdefault(trade["rule"], []).append(trade)
    for rule, rows in by_rule.items():
        expected = rules_config.RULE_BACKTEST_HIT_RATE.get(rule)
        if expected is None or not rows:
            continue
        live = sum(1 for r in rows if (r["pnl"] or 0) > 0) / len(rows)
        drifts[rule] = live - expected
    worst_drift = max(drifts.values(), key=abs) if drifts else None

    index_return = index_pnl = None
    if trades:
        start, end = min(t["entry_date"] for t in trades), max(t["exit_date"] for t in trades)
        try:
            backtest.ensure_benchmark(conn)
            index_return = backtest.benchmark_return(conn, start, end)
        except Exception:
            # A missing baseline weakens the report; it does not invalidate a trade.
            index_return = None
        if index_return is not None:
            from src import risk_config

            index_pnl = index_return * risk_config.MAX_TOTAL_CAPITAL

    return {
        "as_of": as_of,
        "first_entry": first,
        "days": days,
        "trades": len(trades),
        "net": round(net, 2),
        "expectancy": round(expectancy, 2) if expectancy is not None else None,
        "drift": worst_drift,
        "drift_by_rule": drifts,
        "index_return": index_return,
        "index_pnl": round(index_pnl, 2) if index_pnl is not None else None,
    }


def _trend(now, before, higher_is_better=True):
    """A word describing the direction of travel, or None when there is no baseline."""
    if now is None or before is None:
        return None
    delta = now - before
    if abs(delta) < 1e-9:
        return "flat"
    improving = delta > 0 if higher_is_better else delta < 0
    return "improving" if improving else "worsening"


def criteria(conn, as_of=None):
    """The five criteria, each with a status, the numbers behind it, and a trend."""
    as_of = as_of or today()
    prior_day = (date.fromisoformat(as_of) - timedelta(days=TREND_DAYS)).isoformat()
    now = snapshot(conn, as_of)
    before = snapshot(conn, prior_day)

    cfg = rules_config
    out = []

    # 1. Sample — both thresholds, whichever comes later.
    days_met = now["days"] >= cfg.EVALUATION_DAYS_REQUIRED
    trades_met = now["trades"] >= cfg.EVALUATION_MIN_TRADES
    if days_met and trades_met:
        sample_detail = "met"
    else:
        days_part = "days met" if days_met else f"{cfg.EVALUATION_DAYS_REQUIRED - now['days']}d short"
        trades_part = (
            "trades met" if trades_met
            else f"{cfg.EVALUATION_MIN_TRADES - now['trades']} trades short"
        )
        sample_detail = f"{days_part}, {trades_part}"
    out.append({
        "key": "sample",
        "label": f"{cfg.EVALUATION_WEEKS_REQUIRED} weeks AND {cfg.EVALUATION_MIN_TRADES} closed trades",
        # Never FAIL. Being early is not a failure, and marking it as one would
        # make the first five weeks look like five weeks of bad news.
        "status": PASS if (days_met and trades_met) else INSUFFICIENT,
        "value": f"{now['days']}d, {now['trades']} trades",
        "threshold": f"{cfg.EVALUATION_DAYS_REQUIRED}d, {cfg.EVALUATION_MIN_TRADES} trades",
        "trend": _trend(now["trades"], before["trades"]),
        "detail": sample_detail,
    })

    # 2. Cumulative P&L, after costs.
    out.append({
        "key": "cumulative_pnl",
        "label": "cumulative P&L positive (after costs)",
        "status": (INSUFFICIENT if not now["trades"]
                   else PASS if now["net"] > cfg.GATE_MIN_CUMULATIVE_PNL else FAIL),
        "value": f"{now['net']:,.0f}" if now["trades"] else "no closed trades",
        "threshold": f"> {cfg.GATE_MIN_CUMULATIVE_PNL:,.0f}",
        "trend": _trend(now["net"], before["net"] if before["trades"] else None),
        "detail": "net of all transaction costs and slippage",
    })

    # 3. Expectancy per trade.
    out.append({
        "key": "expectancy",
        "label": "expectancy per trade positive",
        "status": (INSUFFICIENT if now["expectancy"] is None
                   else PASS if now["expectancy"] > cfg.GATE_MIN_EXPECTANCY else FAIL),
        "value": f"{now['expectancy']:,.0f}" if now["expectancy"] is not None else "no closed trades",
        "threshold": f"> {cfg.GATE_MIN_EXPECTANCY:,.0f}",
        "trend": _trend(now["expectancy"], before["expectancy"]),
        "detail": "separate from cumulative — one large winner can carry a losing process",
    })

    # 4. Hit-rate drift, worst rule binds.
    drift = now["drift"]
    out.append({
        "key": "drift",
        "label": f"live-vs-backtest hit-rate drift under {cfg.GATE_MAX_HIT_RATE_DRIFT:.0%}",
        "status": (INSUFFICIENT if drift is None
                   else PASS if abs(drift) < cfg.GATE_MAX_HIT_RATE_DRIFT else FAIL),
        "value": f"{drift:+.1%} worst rule" if drift is not None else "no closed trades",
        "threshold": f"< {cfg.GATE_MAX_HIT_RATE_DRIFT:.0%}",
        # Smaller absolute drift is better, so the comparison is on |drift|.
        "trend": _trend(
            abs(drift) if drift is not None else None,
            abs(before["drift"]) if before["drift"] is not None else None,
            higher_is_better=False,
        ),
        "detail": f"backtest rates are {cfg.RULE_BACKTEST_HIT_RATE_BASIS}",
    })

    # 5. Against the index.
    beat = None
    if now["index_pnl"] is not None and now["trades"]:
        beat = now["net"] >= now["index_pnl"]
    out.append({
        "key": "benchmark",
        "label": "paper P&L at least the NIFTY over the same days",
        "status": INSUFFICIENT if beat is None else (PASS if beat else FAIL),
        "value": (f"{now['net']:,.0f} vs {now['index_pnl']:,.0f}"
                  if now["index_pnl"] is not None else "no index data"),
        "threshold": "paper >= index",
        "trend": _trend(
            (now["net"] - now["index_pnl"]) if now["index_pnl"] is not None else None,
            (before["net"] - before["index_pnl"]) if before["index_pnl"] is not None else None,
        ),
        "detail": "buy-and-hold costs one round trip and no attention",
    })

    return out, now, before


def verdict(rows):
    """PASS only when all five pass. FAIL the moment the sample is complete and any
    criterion has failed. IN PROGRESS while evidence is still accumulating.

    The ordering matters: a failing criterion inside the window is not yet a FAIL,
    because there are trades still to come that could move it. Once the sample
    criterion is met, the window is closed and the verdict is final.
    """
    statuses = {row["key"]: row["status"] for row in rows}
    if all(status == PASS for status in statuses.values()):
        return VERDICT_PASS
    if statuses.get("sample") == PASS:
        return VERDICT_FAIL
    return VERDICT_IN_PROGRESS


def build_gate(conn, as_of=None):
    rows, now, _ = criteria(conn, as_of)
    result = verdict(rows)
    cfg = rules_config

    lines = [
        "EVALUATION GATE — five pre-committed criteria, all must hold",
        f"  frozen {cfg.GATE_FROZEN_ON}, {cfg.GATE_BASIS}",
        "",
    ]

    for index, row in enumerate(rows, start=1):
        trend = f"   ({row['trend']})" if row["trend"] else ""
        lines.append(f"  {MARKS[row['status']]}  {index}. {row['label']}")
        lines.append(f"          {row['value']}   target {row['threshold']}{trend}")

    lines.append("")
    if result == VERDICT_PASS:
        lines.append("  VERDICT: PASS — all five criteria hold.")
        lines.append(
            "  This is the paper record clearing a bar set before it existed. It is "
            "not a recommendation to trade real money, and nothing here has "
            "accounted for the difference between a simulated fill and a real one."
        )
    elif result == VERDICT_FAIL:
        failed = [r["label"] for r in rows if r["status"] == FAIL]
        lines.append(f"  VERDICT: FAIL — {len(failed)} criterion(s) not met.")
        lines.append(f"  Failing: {'; '.join(failed)}")
        lines.append(
            "  This is the gate working, not the project failing. The rules go back "
            "for another walk-forward cycle, or the system stays paper-only. The "
            "outcome it prevented was finding this out with real money."
        )
    else:
        pending = [r["label"] for r in rows if r["status"] == INSUFFICIENT]
        lines.append(f"  VERDICT: IN PROGRESS — {len(pending)} criterion(s) lack data.")
        lines.append(
            f"  {now['trades']} of {cfg.EVALUATION_MIN_TRADES} trades, "
            f"{now['days']} of {cfg.EVALUATION_DAYS_REQUIRED} days. A criterion "
            "failing today is not a verdict; the window is still open."
        )

    lines.append(
        "\n  Thresholds are frozen and pinned by tests/test_gate.py. Changing one "
        "fails the suite until the test is changed too — moving a goalpost after "
        "seeing results is the failure this gate exists to prevent."
    )
    return "\n".join(lines)


def run(dry_run=False, date=None, **kwargs):
    conn = get_connection()
    try:
        init_db(conn)
        message = f"nse-assist evaluation gate — {date or today()}\n\n{build_gate(conn, date)}"
        deliver.send_message(message, dry_run=dry_run, parse_mode=None)
        print(f"[gate] sent ({len(message)} chars)" if not dry_run else "[gate] dry run")
        return message
    finally:
        conn.close()
