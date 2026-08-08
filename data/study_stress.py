"""Burst 5 — the stress battery and the benchmark: the final verdict.

    python -m data.study_stress

Burst 4 left one caveat standing: the surviving cells hold stocks for weeks, so
their positive out-of-sample medians could be market drift wearing a dividend
costume. This burst settles it. Every trade in the selected cell is paired with
NIFTY over the IDENTICAL two dates — the same capital parked in the index, then
the battery: slippage at 2x and 3x, the bottom liquidity tercile excluded,
special dividends separated from regular ones, and the top five winning events
removed. All rows are validation-period only; the tuning years already had
their say.

THE DECISION RULE, FIXED BEFORE THE NUMBERS ARE SEEN

The edge SURVIVES only if the paired median excess over NIFTY is positive in
the validation period at baseline AND stays positive under every required
stress: 3x slippage, the liquidity exclusion, regular-dividends-only, and
outliers-removed. One failure and the verdict is DIES, naming the stress that
killed it. The special-dividends-only row is diagnostic (specials are too few
to demand significance from) but regular-only is required — an "edge" that
lives only in special situations is a different, rarer strategy than the one
gridded here.

The benchmark is frictionless: parking in an index fund costs a few basis
points that this comparison charges to the strategy's side entirely. Every
choice in this file leans against the edge on purpose — a verdict that
survives adversarial accounting is worth writing down; one that needs
favourable accounting is already dead.

Special = yield >= 5%, the study_exdate bucket where special payouts live. A
proxy, stated as one: NSE announcements distinguish special dividends, but
that classification is not in the cache, and the yield cut is reproducible
from data already validated.
"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data import backtest, events, frictions, results, study_exdate, study_grid

MARK_START = "<!-- study_stress:start -->"
MARK_END = "<!-- study_stress:end -->"

SLIPPAGE_MULTIPLIERS = (2, 3)
SPECIAL_YIELD_PCT = 5.0
OUTLIERS_REMOVED = 5

# The rows that must ALL keep positive paired excess for the edge to survive.
REQUIRED_STRESSES = ("slippage 3x", "ex bottom-liquidity tercile",
                     "regular dividends only", "top 5 winners removed")


# --- benchmark pairing --------------------------------------------------------


def with_nifty(trades, closes):
    """Adds nifty_return (same entry/exit dates) and excess; trades the index
    cannot date are dropped and counted — a benchmark hole must not silently
    become a zero."""
    nifty = []
    for row in trades.itertuples():
        entry = closes.get(row.entry_date)
        exit_ = closes.get(row.exit_date)
        nifty.append(exit_ / entry - 1 if entry and exit_ else None)
    out = trades.copy()
    out["nifty_return"] = nifty
    missing = int(out["nifty_return"].isna().sum())
    out = out.dropna(subset=["nifty_return"]).copy()
    out["excess"] = out["net_return"] - out["nifty_return"]
    return out, missing


def stress_stats(name, slice_):
    """One table row: paired medians, and how often the strategy beat the index."""
    if slice_.empty:
        return {"stress": name, "n": 0, "median_net": None, "median_nifty": None,
                "median_excess": None, "beat_nifty": None}
    return {
        "stress": name, "n": len(slice_),
        "median_net": float(slice_["net_return"].median()),
        "median_nifty": float(slice_["nifty_return"].median()),
        "median_excess": float(slice_["excess"].median()),
        "beat_nifty": float((slice_["excess"] > 0).mean()),
    }


# --- the battery --------------------------------------------------------------


def resimulate_cells(cells, slippage_multiplier):
    """The surviving cells re-run through the friction model with slippage
    scaled — a real re-simulation, because slippage compounds into turnover
    charges and taxes; scaling the old nets would understate the damage."""
    cfg = frictions.Config.from_params()
    cfg = replace(cfg, slippage_bps=cfg.slippage_bps * slippage_multiplier)
    grid = backtest.load_backtest_params()
    table = pd.read_parquet(events.EVENTS_PATH)
    series = backtest.sessions_by_symbol(sorted(table["symbol"].unique()))
    located, _ = backtest.locate_events(table, series)
    frames = [backtest.simulate_cell(entry, exit_after, located, series, cfg,
                                     grid["notional"], grid["train_until"])[0]
              for entry, exit_after in cells]
    return pd.concat(frames, ignore_index=True)


def cell_slice(trades, cell, in_sample=False):
    entry, exit_after = cell
    return trades[(trades["entry_days_before"] == entry)
                  & (trades["exit_days_after"] == exit_after)
                  & (trades["in_sample"] == in_sample)]


def battery(baseline, cell, closes, cutoffs):
    """Every stress row for the selected cell, validation period only."""
    base, missing = with_nifty(cell_slice(baseline, cell), closes)
    rows = [stress_stats("baseline", base)]

    for multiplier in SLIPPAGE_MULTIPLIERS:
        stressed = resimulate_cells([cell], multiplier)
        merged = cell_slice(stressed, cell).merge(
            base[["symbol", "ex_date", "nifty_return"]],
            on=["symbol", "ex_date"], how="inner", validate="1:1")
        merged["excess"] = merged["net_return"] - merged["nifty_return"]
        rows.append(stress_stats(f"slippage {multiplier}x", merged))

    low_cut = cutoffs[0]
    rows.append(stress_stats("ex bottom-liquidity tercile",
                             base[base["turnover_60d"] >= low_cut]))
    rows.append(stress_stats("regular dividends only",
                             base[base["yield_pct"] < SPECIAL_YIELD_PCT]))
    rows.append(stress_stats("special dividends only",
                             base[base["yield_pct"] >= SPECIAL_YIELD_PCT]))
    trimmed = base.drop(base.nlargest(OUTLIERS_REMOVED, "net_return").index)
    rows.append(stress_stats("top 5 winners removed", trimmed))
    return rows, missing


def verdict(rows):
    """(survives: bool, killer or None, baseline row). The rule from the module
    docstring, applied mechanically — no judgment call happens here."""
    by_name = {row["stress"]: row for row in rows}
    base = by_name["baseline"]
    if base["median_excess"] is None or base["median_excess"] <= 0:
        return False, "baseline", base
    for name in REQUIRED_STRESSES:
        row = by_name.get(name)
        if row is None or row["median_excess"] is None or row["median_excess"] <= 0:
            return False, name, base
    return True, None, base


# --- output -------------------------------------------------------------------


def _pct(value, signed=True):
    if value is None:
        return "–"
    return f"{value:+.2%}" if signed else f"{value:.0%}"


def results_markdown(cell, rows, survivor_counts, missing, verdict_result):
    stamp = datetime.now(timezone.utc).date().isoformat()
    survives, killer, base = verdict_result
    entry, exit_after = cell

    lines = [
        MARK_START,
        f"## Burst 5 — stress battery and the benchmark ({stamp})",
        "",
        f"Selected cell (e={entry}, x={exit_after}), validation period only, "
        f"every trade paired with NIFTY over the identical dates (benchmark "
        f"frictionless — the accounting leans against the strategy throughout)."
        + (f" {missing} trade(s) had no NIFTY bar and were dropped." if missing else ""),
        "",
        "| stress | n | median net | median NIFTY | median excess | beat NIFTY |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row['stress']} | {row['n']:,} | {_pct(row['median_net'])} | "
                     f"{_pct(row['median_nifty'])} | {_pct(row['median_excess'])} | "
                     f"{_pct(row['beat_nifty'], signed=False)} |")

    held_base, held_stressed, total = survivor_counts
    lines += [
        "",
        f"Across all {total} tune-positive cells: {held_base} keep positive "
        f"out-of-sample excess over NIFTY at baseline; {held_stressed} still do "
        f"at 3x slippage.",
        "",
    ]

    if survives:
        lines.append(
            f"**FINAL VERDICT: the edge survives.** The two numbers that decide "
            f"it: the strategy's validation median of {_pct(base['median_net'])} "
            f"per event after all frictions and taxes, against NIFTY's "
            f"{_pct(base['median_nifty'])} over the identical windows — a paired "
            f"excess of {_pct(base['median_excess'])} that stays positive under "
            f"every required stress (3x slippage, liquidity exclusion, regular "
            f"dividends only, outliers removed). This clears the pre-committed "
            f"bar; what it earns is a live paper test, not capital.")
    else:
        lines.append(
            f"**FINAL VERDICT: the edge dies** — killed by the {killer} row. The "
            f"two numbers that decide it: the strategy's validation median of "
            f"{_pct(base['median_net'])} per event against NIFTY's "
            f"{_pct(base['median_nifty'])} over the identical windows. Whatever "
            f"the grid found does not beat parking the same capital in the "
            f"index once the required stresses are applied. The verdict is the "
            f"system working: a strategy rejected before it cost anything.")
    lines.append(MARK_END)
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------


def run():
    baseline = study_grid.with_context(study_grid.load_trades())
    cell = study_grid.best_cell(baseline)
    if cell is None:
        print("[stress] no cell survived frictions in-sample — nothing to stress")
        return 1
    survivors, _ = study_grid.survivorship(baseline)
    closes = study_exdate.nifty_closes(refresh=False)
    cutoffs = study_grid.liquidity_cutoffs(baseline)

    rows, missing = battery(baseline, cell, closes, cutoffs)

    stressed_all = resimulate_cells(survivors, SLIPPAGE_MULTIPLIERS[-1])
    held_base, held_stressed = 0, 0
    for survivor in survivors:
        base_slice, _ = with_nifty(cell_slice(baseline, survivor), closes)
        if base_slice.empty:
            continue
        if base_slice["excess"].median() > 0:
            held_base += 1
        merged = cell_slice(stressed_all, survivor).merge(
            base_slice[["symbol", "ex_date", "nifty_return"]],
            on=["symbol", "ex_date"], how="inner", validate="1:1")
        if len(merged) and (merged["net_return"] - merged["nifty_return"]).median() > 0:
            held_stressed += 1

    verdict_result = verdict(rows)
    section = results_markdown(cell, rows, (held_base, held_stressed, len(survivors)),
                               missing, verdict_result)
    results.update(section, MARK_START, MARK_END)

    for row in rows:
        print(f"[stress] {row['stress']:<28} n={row['n']:<6,} "
              f"excess {_pct(row['median_excess'])}  beat {_pct(row['beat_nifty'], signed=False)}")
    survives, killer, _ = verdict_result
    print(f"[stress] survivors: {held_base}/{len(survivors)} positive excess at "
          f"baseline, {held_stressed}/{len(survivors)} at 3x slippage")
    print(f"[stress] VERDICT: {'edge survives' if survives else f'edge dies ({killer})'}")
    print(f"[stress] wrote the marked section of {results.RESULTS_PATH}")
    return 0


def main(argv=None):
    import argparse
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
