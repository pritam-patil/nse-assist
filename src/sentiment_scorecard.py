"""The sentiment layer's shadow scorecard — does the score predict anything?

Appended to the Sunday weekly. It answers one question and refuses to answer it
early: across closed trades that carry a stored sentiment score, is there any
relationship between the score and the outcome?

A SHADOW SCORECARD IS NOT A PERFORMANCE REPORT

Nothing here changed a single trade. Every number is computed on a record the
layer had no hand in producing, which is the only way to measure a predictor
honestly — the moment sentiment filters a candidate, the trades that remain are no
longer a sample of what sentiment would have predicted, they are a sample of what
sentiment selected.

CORRELATION ON A SMALL SAMPLE IS THEATRE, SO IT IS WITHHELD

Pearson's r on eleven trades will produce a confident-looking number that means
nothing. Below MIN_TRADES_FOR_CORRELATION the report says how far it has to go
instead of printing a coefficient — a number withheld is a number nobody
misreads, and this layer's whole risk is being believed too early.

TERCILES, NOT A THRESHOLD

Bucketing by tercile asks "did the most negative third behave differently from the
rest" without anyone choosing a cutoff, which is the same fitting problem the
walk-forward exists to avoid. The graduation criterion is about the negative
cohort specifically, so the terciles are reported in a way that makes that
comparison visible rather than requiring arithmetic.
"""

import math

from src import ledger, rules_config

# Below this, r is noise wearing a decimal point.
MIN_TRADES_FOR_CORRELATION = 20

# Below this, a tercile has too few trades for its hit rate to mean anything.
MIN_TRADES_PER_BUCKET = 5


def annotated_trades(conn, as_of=None):
    """Closed trades that carry a sentiment score, oldest first.

    Filtering to scored trades is the point: a trade with no score is not a zero,
    it is absent from this analysis entirely. Treating unscored trades as neutral
    would swamp the sample with trades the layer never saw.

    Trades come from ledger.closed_trades so the scorecard's population is the same
    population the gate and the weekly judge — a different definition of "closed"
    here would make the two reports describe different ledgers.
    """
    scores = {
        r["signal_id"]: r["score"]
        for r in conn.execute(
            "SELECT signal_id, score FROM news_sentiment WHERE score IS NOT NULL")
    }
    return [
        {**trade, "score": scores[trade["signal_id"]]}
        for trade in ledger.closed_trades(conn, until=as_of)
        if trade["signal_id"] in scores
    ]


def correlation(pairs):
    """Pearson's r between score and P&L, or None when it cannot be computed.

    None when either series has zero variance — every score identical, or every
    trade the same P&L. r is undefined there, and returning 0.0 would read as
    "measured, no relationship" rather than "not measurable".
    """
    if len(pairs) < 2:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(pairs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    numerator = sum(a * b for a, b in zip(dx, dy))
    denominator = math.sqrt(sum(a * a for a in dx) * sum(b * b for b in dy))
    if denominator == 0:
        return None
    return numerator / denominator


def terciles(trades):
    """Split by score into low/mid/high thirds, with each bucket's stats.

    Split by rank rather than by fixed score cuts: most days score 0.0, so a
    fixed-cut split would put the entire sample in one bucket and report two empty
    ones as though they were measured.
    """
    if len(trades) < 3:
        return []
    ordered = sorted(trades, key=lambda t: t["score"])
    size = len(ordered) // 3
    chunks = [ordered[:size], ordered[size:len(ordered) - size], ordered[len(ordered) - size:]]

    out = []
    for name, chunk in zip(("most negative", "middle", "most positive"), chunks):
        if not chunk:
            continue
        wins = sum(1 for t in chunk if (t["pnl"] or 0) > 0)
        net = sum(t["pnl"] or 0 for t in chunk)
        out.append({
            "bucket": name,
            "trades": len(chunk),
            "score_range": (min(t["score"] for t in chunk), max(t["score"] for t in chunk)),
            "hit_rate": wins / len(chunk),
            "net": round(net, 2),
            "expectancy": round(net / len(chunk), 2),
            "enough": len(chunk) >= MIN_TRADES_PER_BUCKET,
        })
    return out


def negative_cohort_gap(trades):
    """Expectancy of the negative-score trades minus everything else.

    This is the number the graduation gate is written against, so it is computed
    directly rather than left to be eyeballed off the tercile table. Negative means
    negative-sentiment trades did worse, which is the direction that would make a
    veto-only filter worth designing.
    """
    negative = [t for t in trades if t["score"] < 0]
    rest = [t for t in trades if t["score"] >= 0]
    if not negative or not rest:
        return None
    neg_exp = sum(t["pnl"] or 0 for t in negative) / len(negative)
    rest_exp = sum(t["pnl"] or 0 for t in rest) / len(rest)
    return {
        "negative_trades": len(negative),
        "rest_trades": len(rest),
        "negative_expectancy": round(neg_exp, 2),
        "rest_expectancy": round(rest_exp, 2),
        "gap": round(neg_exp - rest_exp, 2),
    }


def build_scorecard(conn, as_of=None):
    trades = annotated_trades(conn, as_of)
    cfg = rules_config

    lines = ["SENTIMENT SHADOW SCORECARD — observational, nothing acted on this"]

    if not trades:
        lines.append(
            f"  0 annotated closed trades. Needs {cfg.SENTIMENT_MIN_ANNOTATED_TRADES} "
            "before the question can be asked at all."
        )
        lines.append(
            "  The layer scores assembled candidates each evening and stores what it "
            "saw. With every rule disabled, nothing is being assembled, so this "
            "stays empty until a rule is re-enabled."
        )
        return "\n".join(lines)

    lines.append(f"  {len(trades)} annotated closed trade(s) of "
                 f"{cfg.SENTIMENT_MIN_ANNOTATED_TRADES} needed to judge")

    # Correlation, or the reason it is withheld.
    if len(trades) < MIN_TRADES_FOR_CORRELATION:
        lines.append(
            f"  correlation withheld — needs {MIN_TRADES_FOR_CORRELATION} trades; "
            f"r on {len(trades)} is noise with a decimal point"
        )
    else:
        r = correlation([(t["score"], t["pnl"] or 0) for t in trades])
        lines.append(
            f"  score vs P&L correlation: {r:+.3f}" if r is not None
            else "  correlation undefined — no variance in scores or outcomes"
        )

    buckets = terciles(trades)
    if buckets:
        lines.append(f"\n  {'bucket':<15}{'n':>5}{'score range':>16}{'hit':>8}{'expectancy':>13}")
        for bucket in buckets:
            low, high = bucket["score_range"]
            mark = "" if bucket["enough"] else "   (too few to read)"
            lines.append(
                f"  {bucket['bucket']:<15}{bucket['trades']:>5}"
                f"{f'{low:+.2f} to {high:+.2f}':>16}"
                f"{bucket['hit_rate']:>8.1%}{bucket['expectancy']:>13,.0f}{mark}"
            )

    gap = negative_cohort_gap(trades)
    if gap:
        lines.append(
            f"\n  negative-sentiment trades: {gap['negative_trades']} at "
            f"{gap['negative_expectancy']:,.0f} per trade vs {gap['rest_trades']} others "
            f"at {gap['rest_expectancy']:,.0f} — gap {gap['gap']:+,.0f}"
        )
    else:
        lines.append("\n  no negative-sentiment trades yet, so no cohort to compare")

    lines.append(
        f"\n  Graduation needs {cfg.SENTIMENT_MIN_ANNOTATED_TRADES} annotated closed "
        f"trades AND a visible outcome gap for the negative cohort. Frozen "
        f"{cfg.SENTIMENT_GATE_FROZEN_ON}; see the README."
    )
    lines.append(
        "  Nothing above filtered, sized, ordered or vetoed a trade. If it ever "
        "graduates it enters as a veto-only filter, evaluated in its own right — "
        "never as a signal generator."
    )
    return "\n".join(lines)
