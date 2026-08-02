"""Stage — how the parking schemes have behaved, ranked per category.

    python main.py --stage fund-digest    # standalone, for a manual look

Appended to the Sunday weekly rather than sent as a second message: two Telegram
messages minutes apart on the same evening is how both stop being read. Building
and sending are separate for exactly that reason — weekly.py calls build_digest()
and glues the text on, and this stage exists for when you want it on its own. It
needs only deliver.py's send helpers.

RANKS ARE NOT RECOMMENDATIONS

No buy, sell, hold, switch or "top pick" appears in this output and none should.
A ranking of past behaviour is a description. Treating it as advice is a decision
for the reader to make with the numbers in front of them, which is why the raw
metrics table sits beside the rank rather than behind it — when the composite
disagrees with your judgement, the table shows which input caused it.

THE COMPOSITE FAVOURS STABILITY, DELIBERATELY

Money parked between trades has one job: be there, in full, when you want it. A
scheme returning 7% with a bad month of -2% is worse at that job than one returning
6.5% that never dips, even though the first wins on the number most tables sort by.
So consistency and low volatility outweigh raw return, and the worst single month
is shown as its own column rather than buried — over a short horizon the tail is
what you actually meet, because you meet it exactly when you need the money back.

The weights are printed in the message. A composite whose recipe is hidden is an
opinion with a decimal point.
"""

from src import deliver, fund_watchlist, funds
from src.db import get_connection, init_db
from src.runlog import today

# Stability outweighs return, and the two downside terms together outweigh
# everything else. Return is deliberately smallest: across these schemes the spread
# is a few tens of basis points over a parking horizon, while a bad month is felt
# in full and immediately.
COMPOSITE_WEIGHTS = {
    "consistency": 0.35,   # share of rolling quarters that ended positive
    "stability": 0.30,     # inverse of 1-year volatility
    "downside": 0.25,      # worst single month, less bad is better
    "return": 0.10,        # 1-year return
}

# Below this a rank says nothing: with one scheme per category it is a list of
# length one wearing a number.
MIN_SCHEMES_TO_RANK = 2


def _normalise(values, higher_is_better=True):
    """Min-max within a category, with ties and singletons landing neutral.

    Neutral rather than 1.0 on purpose: best-of-one has demonstrated nothing, and a
    full score would let an unrankable category produce a confident-looking
    composite.
    """
    present = [v for v in values if v is not None]
    if len(present) < 2 or max(present) == min(present):
        return [None if v is None else 0.5 for v in values]
    low, high = min(present), max(present)
    scaled = [None if v is None else (v - low) / (high - low) for v in values]
    return scaled if higher_is_better else [None if v is None else 1 - v for v in scaled]


def composite_scores(rows):
    """Weighted score per scheme, within one category."""
    axes = {
        "consistency": _normalise([r.get("consistency_3m") for r in rows]),
        "stability": _normalise([r.get("vol_1y") for r in rows], higher_is_better=False),
        "downside": _normalise([r.get("worst_month_1y") for r in rows]),
        "return": _normalise([r.get("return_1y") for r in rows]),
    }
    scores = []
    for index in range(len(rows)):
        # A missing metric contributes neutrally rather than zero, so a scheme is not
        # ranked last for having short history.
        total = sum(
            weight * (0.5 if axes[name][index] is None else axes[name][index])
            for name, weight in COMPOSITE_WEIGHTS.items()
        )
        scores.append(round(total, 3))
    return scores


def by_category(conn, scheme_codes=None):
    grouped = {}
    for code in (scheme_codes or fund_watchlist.SCHEME_CODES):
        metrics = funds.metrics_for(conn, code)
        if not metrics:
            continue
        row = dict(metrics)
        row["scheme_code"] = code
        row["label"] = fund_watchlist.label_for(code)
        grouped.setdefault(fund_watchlist.CATEGORIES.get(code, "Uncategorised"), []).append(row)
    return grouped


def _pct(value, width=8, places=2):
    return f"{value:>{width}.{places}%}" if value is not None else f"{'n/a':>{width}}"


def build_digest(conn, scheme_codes=None):
    grouped = by_category(conn, scheme_codes)
    lines = ["PARKED CASH — how each scheme has behaved"]

    if not grouped:
        lines.append("  No fund metrics available. Run --stage funds first.")
        return "\n".join(lines)

    unrankable = []
    for category in sorted(grouped):
        rows = grouped[category]
        ordered = sorted(zip(rows, composite_scores(rows)), key=lambda pair: -pair[1])

        lines.append(f"\n  {category.upper()}")
        if len(rows) < MIN_SCHEMES_TO_RANK:
            unrankable.append(category)

        # Rank, label and composite on one line, then the raw metrics beneath it —
        # rather than a rank list followed by a separate table, which prints every
        # label twice. Two short lines per scheme instead of one wide row because
        # this is read on a phone: a seven-column table wraps into fragments whose
        # numbers no longer sit under their headings, which is worse than not
        # tabulating at all.
        for rank, (row, score) in enumerate(ordered, start=1):
            lines.append(f"    {rank}. {row['label']}   composite {score:.2f}")
            lines.append(
                f"       1m {_pct(row.get('return_1m'), 0)}   "
                f"3m {_pct(row.get('return_3m'), 0)}   "
                f"1y {_pct(row.get('return_1y'), 0)}   "
                f"vol {_pct(row.get('vol_1y'), 0)}"
            )
            # Worst month first on its line: over a parking horizon the tail is what
            # you meet, and you meet it exactly when you want the money back.
            lines.append(
                f"       worst month {_pct(row.get('worst_month_1y'), 0)}   "
                f"max drawdown {_pct(row.get('max_drawdown_1y'), 0)}   "
                f"consistency {_pct(row.get('consistency_3m'), 0, 0)}"
            )
        withheld = [r["label"] for r, _ in ordered if r.get("restatements")]
        if withheld:
            lines.append(
                f"    Some figures withheld for {', '.join(withheld)}: the NAV series "
                "changes unit face value part-way through, so a window crossing it "
                "would measure a restatement rather than a return."
            )

    if unrankable:
        lines.append(
            f"\n  Only one scheme in: {', '.join(unrankable)}. A rank of 1 of 1 "
            "describes nothing — add more to the watchlist to compare."
        )

    weights = ", ".join(f"{name} {weight:.0%}" for name, weight in COMPOSITE_WEIGHTS.items())
    lines.append(f"\n  Composite weights: {weights}.")
    lines.append(
        "  Weighted toward stability rather than return: parked money is judged on "
        "being available in full when wanted, so the worst month matters more than "
        "the average."
    )
    lines.append(
        "\n  These ranks describe how each scheme has behaved. They are not a "
        "forecast, and past consistency does not carry forward."
    )
    lines.append(
        "  Two things change what you keep and are not in the numbers above. Some "
        "schemes charge an exit load if you redeem within a short window. And tax "
        "differs by category: gains on debt-oriented schemes — liquid, money market, "
        "ultra-short — are added to income and taxed at your slab rate whatever the "
        "holding period, while arbitrage schemes are taxed on the equity basis "
        "instead. Rates change; check the current position for your own situation."
    )
    lines.append(
        "  No scheme above is a recommendation to buy, sell or switch. Ranks and "
        "numbers only."
    )
    return "\n".join(lines)


def run(dry_run=False, scheme_codes=None, **kwargs):
    conn = get_connection()
    try:
        init_db(conn)
        message = f"nse-assist fund digest — {today()}\n\n{build_digest(conn, scheme_codes)}"
        deliver.send_message(message, dry_run=dry_run, parse_mode=None)
        print(f"[fund-digest] sent ({len(message)} chars)" if not dry_run
              else "[fund-digest] dry run")
        return message
    finally:
        conn.close()
