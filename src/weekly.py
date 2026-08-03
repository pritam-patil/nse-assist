"""Stage — the Sunday weekly: what the paper ledger did, and whether to believe it.

    python main.py --stage weekly

Four questions, in the order they are worth asking:

  1. What happened this week, and cumulatively, per rule?
  2. Is the live hit rate drifting from the backtest's? A large gap means one of
     them is wrong about the market, and the trade count says which.
  3. Does multi-rule agreement predict anything? signals.py records the rules that
     fired and lost the dedupe precisely so this can be answered rather than
     assumed.
  4. Would the same capital have done better sitting in the index?

Same tone rules as the brief: no emojis, no superlatives, no urgency, no advice.
A weekly report that celebrates a good week trains you to dread a bad one, and both
reactions are noise on a sample this small.

WHY BOTH WINDOWS

This week alone is almost never evidence — a handful of trades, and the same rule
can look transformed by two lucky exits. It is shown because you will look for it,
and showing it beside the cumulative number is what keeps it in proportion.
"""

from datetime import date, timedelta

from src import backtest, deliver, fund_digest, gate, health, ledger, risk_config, rules_config
from src import sentiment_scorecard, signals
from src.db import get_connection, init_db
from src.runlog import today

WEEK_DAYS = 7


def week_bounds(day=None):
    end = date.fromisoformat(day or today())
    return (end - timedelta(days=WEEK_DAYS)).isoformat(), end.isoformat()


def _rule_stats(conn, since=None):
    """Closed trades grouped by originating rule, optionally since a date."""
    return {
        rule: {
            "trades": stats["trades"], "wins": stats["wins"],
            "hit_rate": stats["hit_rate"] if stats["trades"] else None,
            "net": stats["net_pnl"], "costs": stats["costs"],
        }
        for rule, stats in ledger.by_rule(conn, since=since).items()
    }


def cohort_stats(conn):
    """Confirmed trades versus solo ones.

    Whether multi-rule agreement predicts anything is a real question with an
    answer in the data, and this is the only place it gets asked. The cohort rule
    itself lives in ledger.by_cohort so the gate and the weekly cannot disagree
    about what "confirmed" means.
    """
    return {
        cohort: {
            "trades": stats["trades"], "wins": stats["wins"],
            "hit_rate": stats["hit_rate"] if stats["trades"] else None,
            "net": stats["net_pnl"], "expectancy": stats["expectancy"],
        }
        for cohort, stats in ledger.by_cohort(conn).items()
    }


def benchmark_comparison(conn):
    """Cumulative paper P&L against the same capital held in the index.

    The comparison the strategy has to win. Buy-and-hold costs one round trip and no
    attention; anything that underperforms it has charged you its transaction costs
    and your evenings for the privilege.
    """
    span = conn.execute(
        "SELECT MIN(entry_date), MAX(COALESCE(exit_date, entry_date)) FROM paper_trades"
    ).fetchone()
    if not span or not span[0]:
        return None

    start, end = span[0], span[1]
    backtest.ensure_benchmark(conn)
    index_return = backtest.benchmark_return(conn, start, end)

    net = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status = 'closed'"
    ).fetchone()[0]
    capital = risk_config.MAX_TOTAL_CAPITAL
    return {
        "start": start, "end": end,
        "net": round(net, 2),
        "strategy_return": net / capital if capital else 0.0,
        "index_return": index_return,
        "index_pnl": round(index_return * capital, 2) if index_return is not None else None,
        "capital": capital,
    }


def _hit(value):
    return f"{value:.1%}" if value is not None else "n/a"


def build_weekly(conn, day=None):
    since, end = week_bounds(day)
    week = _rule_stats(conn, since=since)
    total = _rule_stats(conn)
    cohorts = cohort_stats(conn)
    versus = benchmark_comparison(conn)

    lines = [f"nse-assist weekly — {end}"]

    banner = rules_config.pipeline_test_banner()
    if banner:
        lines.append(f"\n{banner}")

    if not total:
        lines.append("\nNo closed paper trades yet.")
        if not signals.ENABLED_RULES:
            lines.append(
                f"All {len(signals.RULES)} rules are disabled by walk-forward "
                "validation, so nothing is being proposed and nothing will fill. "
                "The ledger starts when a rule is re-enabled."
            )
    else:
        lines.append(f"\nPER RULE (week of {since} to {end}, then cumulative)")
        lines.append(f"  {'rule':<24}{'wk n':>6}{'wk net':>10}"
                     f"{'cum n':>7}{'cum hit':>9}{'backtest':>10}{'drift':>8}{'cum net':>11}")
        for rule in sorted(total):
            this_week = week.get(rule, {})
            overall = total[rule]
            expected = rules_config.RULE_BACKTEST_HIT_RATE.get(rule)
            drift = None
            if overall["hit_rate"] is not None and expected is not None:
                drift = overall["hit_rate"] - expected
            flag = ""
            if drift is not None and abs(drift) > rules_config.HIT_RATE_DRIFT_FLAG:
                flag = "  <- drift"
            lines.append(
                f"  {rule:<24}{this_week.get('trades', 0):>6}"
                f"{this_week.get('net', 0):>10,.0f}"
                f"{overall['trades']:>7}{_hit(overall['hit_rate']):>9}"
                f"{_hit(expected):>10}"
                f"{(f'{drift:+.1%}' if drift is not None else 'n/a'):>8}"
                f"{overall['net']:>11,.0f}{flag}"
            )
        lines.append(
            f"  Drift beyond {rules_config.HIT_RATE_DRIFT_FLAG:.0%} means the live rate and the "
            f"backtest disagree about the market. On a large sample that is the "
            f"backtest being wrong; on a small one it is nothing yet."
        )
        lines.append(f"  Backtest rates are {rules_config.RULE_BACKTEST_HIT_RATE_BASIS}.")

        lines.append("\nCONFIRMED VERSUS SOLO")
        if not cohorts:
            lines.append("  No closed trades to split.")
        else:
            lines.append(f"  {'cohort':<12}{'n':>6}{'hit':>9}{'expectancy':>12}{'net':>11}")
            for name in ("confirmed", "solo"):
                stats = cohorts.get(name)
                if not stats:
                    lines.append(f"  {name:<12}{0:>6}{'n/a':>9}{'n/a':>12}{'n/a':>11}")
                    continue
                lines.append(f"  {name:<12}{stats['trades']:>6}{_hit(stats['hit_rate']):>9}"
                             f"{stats['expectancy']:>12,.0f}{stats['net']:>11,.0f}")
            lines.append("  Confirmed means more than one rule fired on that symbol that day.")

    if versus and versus["index_return"] is not None:
        lines.append("\nAGAINST THE INDEX")
        lines.append(f"  period {versus['start']} to {versus['end']}")
        lines.append(f"  paper ledger  {versus['net']:>12,.0f}  "
                     f"({versus['strategy_return']:+.2%} on {versus['capital']:,} deployed)")
        lines.append(f"  NIFTY held    {versus['index_pnl']:>12,.0f}  "
                     f"({versus['index_return']:+.2%} over the same days)")
        difference = versus["net"] - (versus["index_pnl"] or 0)
        lines.append(f"  difference    {difference:>12,.0f}")

    lines.append("\n" + gate.build_gate(conn, day))

    # Shadow only: nothing in this block changed a trade, which is what makes
    # it a measurement rather than a performance report.
    lines.append("\n" + sentiment_scorecard.build_scorecard(conn, day))

    # Appended rather than sent as its own message: two Telegram messages minutes
    # apart on a Sunday evening is how both stop being read.
    lines.append("\n" + fund_digest.build_digest(conn))

    footer = health.footer(conn)
    if footer:
        lines.append(f"\n{footer}")

    lines.append("\nDescribes past behaviour, not future returns. Paper trades. "
                 "Not investment advice.")
    return "\n".join(lines)


def run(dry_run=False, date=None, **kwargs):
    conn = get_connection()
    try:
        init_db(conn)
        message = build_weekly(conn, date)
        deliver.send_message(message, dry_run=dry_run, parse_mode=None)
        print(f"[weekly] sent ({len(message)} chars)" if not dry_run else "[weekly] dry run")
        return message
    finally:
        conn.close()
