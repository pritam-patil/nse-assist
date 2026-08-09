"""Retrofit — the specials slice, joined onto the stored grid. No re-simulation.

    python -m data.study_specials

Burst 5 left one diagnostic dangling: special dividends showed +4.20% excess
on seven events. This retrofit gives that slice a proper definition (see
events.SPECIAL_*: amount above 3x the symbol's trailing median payout, or
yield above 5%) and reads it back through the EXISTING trade logs — the flag
is joined on by (symbol, ex-date); not one trade is re-simulated, because the
flag changes nothing about what any trade earned.

WHY THE FINGERPRINT GUARD IS DELIBERATELY BYPASSED HERE

Rebuilding events.parquet with the flag column changes its bytes, so the
grid's fingerprint no longer matches — by design, since a future backtest run
must start clean. But refusing THIS analysis would be the guard misfiring:
the addition is a column, not a change to any input the simulation read. The
replacement check is the one that actually protects the join — every trade
must find exactly one event row, and the amounts must agree to the paisa.
Coverage below ~100% means the events table and the logs have genuinely
diverged, and the analysis stops.

THE SLIPPAGE BOUND, BECAUSE THE BATTERY CANNOT RE-RUN

Burst 5's killer was 3x slippage, which needs re-simulation to price exactly
(slippage compounds into STT and taxes). Without re-simulating, the extra
cost of kx slippage is bounded below by (k-1) x 2 x slippage_bps of deployed
capital — the direct price impact alone, ignoring the second-order charge
effects (about a basis point) that would only make it worse. The bound is
applied to the specials excess and labeled an approximation everywhere it
appears.

THE VERDICT-CHANGE RULE, FIXED BEFORE LOOKING

Burst 5's verdict stands unless the specials-only slice of the SELECTED cell
shows (a) at least MIN_TRADES_TO_JUDGE out-of-sample trades — the repo's
long-standing floor below which a number is an anecdote — AND (b) a median
excess over NIFTY that stays positive after the 3x slippage bound. Anything
less is recorded as "verdict unchanged", however pretty the small-n numbers
look. A handful of windfalls is a reason to design a study, never a reason to
reopen a closed one.
"""

from datetime import datetime, timezone

import pandas as pd

from data import backtest, events, frictions, results, study_exdate, study_grid
from data.study_stress import with_nifty

MARK_START = "<!-- study_specials:start -->"
MARK_END = "<!-- study_specials:end -->"

MIN_TRADES_TO_JUDGE = 30
SLIPPAGE_STRESS_MULTIPLIER = 3
JOIN_COVERAGE_FLOOR = 0.999
AMOUNT_TOLERANCE = 0.005
TOP_FLAGGED_SHOWN = 10
SMALL_MINORITY_PCT = 10.0


def slippage_bound(cfg, multiplier=SLIPPAGE_STRESS_MULTIPLIER):
    """Lower bound on the extra cost of multiplied slippage, as a fraction of
    deployed capital. Direct price impact only — the true re-simulated cost is
    slightly worse, so an excess that dies under this bound is dead for sure."""
    return (multiplier - 1) * 2 * cfg.slippage_bps / 10_000


# --- the join -----------------------------------------------------------------


def load_grid_trades():
    """The stored cell logs, WITHOUT the fingerprint guard — see the module
    docstring for why, and join_flags() for the check that replaces it."""
    paths = sorted(backtest.BACKTEST_DIR.glob("trades_*.parquet"))
    if not paths:
        raise RuntimeError("no stored grid — run `python -m data.backtest` first")
    return pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)


def join_flags(trades, table):
    """Trades with the special flag attached, or a refusal with numbers.

    The amounts crossing the join must agree: same symbol, same ex-date, same
    dividend to the paisa. Disagreement means the logs were simulated from a
    different event set than the one supplying the flags, and every conclusion
    drawn from the join would be about a chimera.
    """
    joined = trades.merge(
        table[["symbol", "ex_date", "amount", "special", "yield_pct"]].rename(
            columns={"amount": "event_amount"}),
        on=["symbol", "ex_date"], how="left", validate="m:1")
    coverage = joined["special"].notna().mean()
    if coverage < JOIN_COVERAGE_FLOOR:
        raise RuntimeError(
            f"flag join covers {coverage:.2%} of trades — the events table and "
            f"the stored logs have diverged; re-run data.backtest instead")
    matched = joined.dropna(subset=["event_amount"])
    disagreement = (matched["amount"] - matched["event_amount"]).abs().max()
    if disagreement > AMOUNT_TOLERANCE:
        raise RuntimeError(
            f"amounts disagree across the join (up to {disagreement:.4f}) — the "
            f"logs were simulated from different events; re-run data.backtest")
    return joined.dropna(subset=["special"]).drop(columns=["event_amount"])


# --- sanity -------------------------------------------------------------------


def flagged_share(table):
    return float(table["special"].mean())


def composition(table):
    """How many flags each rule produced, and whether the small-minority sanity
    check holds. The check is part of the retrofit's PRE-COMMITMENT: a flag
    definition that labels a tenth of all dividends "special" is measuring
    something else, and no verdict may change on a mislabeled cohort."""
    flagged = table[table["special"]]
    by_yield = int((flagged["yield_pct"] > events.SPECIAL_YIELD_PCT).sum())
    share = flagged_share(table)
    return {"flagged": len(flagged), "by_yield": by_yield,
            "by_amount": len(flagged) - by_yield, "share": share,
            "sane": share * 100 <= SMALL_MINORITY_PCT}


def top_flagged(table, count=TOP_FLAGGED_SHOWN):
    """The eyeball list: flagged events by yield, with which rule fired. A
    flagged row with yield <= the cut can only have come from the amount rule."""
    flagged = table[table["special"]].copy()
    flagged["rule"] = ["yield" if row.yield_pct == row.yield_pct
                       and row.yield_pct > events.SPECIAL_YIELD_PCT else "amount"
                       for row in flagged.itertuples()]
    return flagged.sort_values("yield_pct", ascending=False).head(count)[
        ["symbol", "ex_date", "amount", "yield_pct", "rule"]]


# --- the slices ---------------------------------------------------------------


def slice_stats(trades, cell, closes, special):
    """Out-of-sample, friction-adjusted, one slice of one cell — with the NIFTY
    pairing, because the verdict's currency is excess, not return."""
    entry, exit_after = cell
    slice_ = trades[(trades["entry_days_before"] == entry)
                    & (trades["exit_days_after"] == exit_after)
                    & (~trades["in_sample"])
                    & (trades["special"] == special)]
    if slice_.empty:
        return {"n": 0, "median_net": None, "hit_rate": None, "median_excess": None}
    paired, _ = with_nifty(slice_, closes)
    return {
        "n": len(slice_),
        "median_net": float(slice_["net_return"].median()),
        "hit_rate": float((slice_["net"] > 0).mean()),
        "median_excess": (float(paired["excess"].median()) if len(paired) else None),
    }


def verdict_change(selected_stats, bound, sanity_ok=True):
    """(changes: bool, reason). The rule from the docstring, mechanically —
    including the gate: a failed small-minority check voids the question."""
    if not sanity_ok:
        return False, (
            "the flag definition failed its own small-minority sanity check — "
            "the amount rule fires on ordinary dividend GROWTH (a trailing "
            "median lags a rising payout for years), so the flagged cohort is "
            "not what 'special' claims, and no verdict can change on a "
            "mislabeled slice")
    n = selected_stats["n"]
    if n < MIN_TRADES_TO_JUDGE:
        return False, (f"{n} out-of-sample special trade(s) in the selected cell "
                       f"— below the {MIN_TRADES_TO_JUDGE}-trade floor, an "
                       f"anecdote however it reads")
    excess = selected_stats["median_excess"]
    if excess is None or excess - bound <= 0:
        shown = f"{excess:+.2%}" if excess is not None else "unmeasurable"
        return False, (f"median excess {shown} does not survive the 3x slippage "
                       f"bound of -{bound:.2%}")
    return True, (f"n={n} with median excess {excess:+.2%} clearing the 3x "
                  f"slippage bound — the pre-committed bar for reopening")


# --- output -------------------------------------------------------------------


def _pct(value, signed=True):
    if value is None:
        return "–"
    return f"{value:+.2%}" if signed else f"{value:.0%}"


def results_markdown(table, trades, survivors, cell, closes, bound):
    stamp = datetime.now(timezone.utc).date().isoformat()
    mix = composition(table)
    regular = slice_stats(trades, cell, closes, special=False)
    special = slice_stats(trades, cell, closes, special=True)
    changes, reason = verdict_change(special, bound, sanity_ok=mix["sane"])

    lines = [
        MARK_START,
        f"## Specials slice — retrofit on the stored grid ({stamp})",
        "",
        f"Special = amount > {events.SPECIAL_AMOUNT_MULTIPLE:g}x the symbol's "
        f"trailing median payout, or yield > {events.SPECIAL_YIELD_PCT:g}% "
        f"(point-in-time, strict). {mix['flagged']:,} of {len(table):,} events "
        f"flagged ({mix['share']:.1%}): {mix['by_yield']} by the yield rule, "
        f"{mix['by_amount']} by the amount rule. Flags joined onto the existing "
        f"trade logs by symbol + ex-date; nothing re-simulated.",
        "",
    ]
    if not mix["sane"]:
        lines += [
            f"**Sanity check FAILED**: {mix['share']:.1%} flagged is not a small "
            f"minority. Eyeballing the amount-rule flags shows the failure "
            f"mode — a trailing median lags a steadily growing payout, so a "
            f"company that ~tripled its dividend over the window has its "
            f"ordinary recent finals flagged for years (UltraCemco's regular "
            f"37/38/38/70/77.5 finals all carry the flag). The slice below is "
            f"therefore closer to \"dividend growers\" than to special "
            f"situations, and is reported as measurement only.",
            "",
        ]
    lines += [
        f"Selected cell (e={cell[0]}, x={cell[1]}), out-of-sample, "
        f"friction-adjusted:",
        "",
        "| slice | n | median net | win rate | median excess vs NIFTY |",
        "|---|---|---|---|---|",
        f"| regular | {regular['n']:,} | {_pct(regular['median_net'])} | "
        f"{_pct(regular['hit_rate'], signed=False)} | {_pct(regular['median_excess'])} |",
        f"| special | {special['n']:,} | {_pct(special['median_net'])} | "
        f"{_pct(special['hit_rate'], signed=False)} | {_pct(special['median_excess'])} |",
        "",
        "All surviving cells, special slice only (out-of-sample):",
        "",
        "| cell | special n | median net | median excess |",
        "|---|---|---|---|",
    ]
    for survivor in survivors:
        row = slice_stats(trades, survivor, closes, special=True)
        lines.append(f"| e={survivor[0]}, x={survivor[1]} | {row['n']:,} | "
                     f"{_pct(row['median_net'])} | {_pct(row['median_excess'])} |")
    lines += [
        "",
        f"Slippage at 3x is approximated here by its lower bound "
        f"(-{bound:.2%} of deployed, direct impact only) — the stored logs "
        f"cannot re-price the compounding exactly, and the true cost is "
        f"slightly worse.",
        "",
    ]
    if changes:
        lines.append(
            f"**The burst 5 verdict acquires a qualification**: {reason}. This "
            f"does not resurrect the gridded strategy — it licenses designing a "
            f"dedicated specials study with its own pre-committed gate, "
            f"starting from announcement data.")
    else:
        lines.append(
            f"**The burst 5 verdict is unchanged**: {reason}. The specials "
            f"slice stays what it was — a diagnostic worth a study of its own "
            f"someday, not evidence against the verdict.")
    lines.append(MARK_END)
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------


def run():
    if not events.EVENTS_PATH.exists():
        print("[specials] no events.parquet — run `python -m data.events` first")
        return 1
    table = pd.read_parquet(events.EVENTS_PATH)
    if "special" not in table.columns:
        print("[specials] events.parquet predates the flag — run "
              "`python -m data.events` to rebuild it")
        return 1

    share = flagged_share(table)
    print(f"[specials] {int(table['special'].sum()):,} of {len(table):,} events "
          f"flagged ({share:.1%})"
          + (" — WARNING: not a small minority, check the rules"
             if share * 100 > SMALL_MINORITY_PCT else ""))
    print(f"[specials] top {TOP_FLAGGED_SHOWN} flagged by yield — eyeball these:")
    for row in top_flagged(table).itertuples():
        print(f"[specials]   {row.symbol:<12} {row.ex_date.date()}  "
              f"amount {row.amount:>8.2f}  yield {row.yield_pct:>6.2f}%  "
              f"({row.rule} rule)")

    trades = join_flags(load_grid_trades(), table)
    cell = study_grid.best_cell(trades)
    survivors, _ = study_grid.survivorship(trades)
    closes = study_exdate.nifty_closes(refresh=False)
    bound = slippage_bound(frictions.Config.from_params())

    section = results_markdown(table, trades, survivors, cell, closes, bound)
    results.update(section, MARK_START, MARK_END)

    special = slice_stats(trades, cell, closes, special=True)
    mix = composition(table)
    changes, reason = verdict_change(special, bound, sanity_ok=mix["sane"])
    print(f"[specials] rules: {mix['by_yield']} by yield, {mix['by_amount']} by amount"
          + ("" if mix["sane"] else " — sanity check FAILED, verdict gate voided"))
    print(f"[specials] selected cell special slice: n={special['n']}, "
          f"median excess {_pct(special['median_excess'])}")
    print(f"[specials] verdict {'CHANGES' if changes else 'unchanged'}: {reason}")
    print(f"[specials] wrote the marked section of {results.RESULTS_PATH}")
    return 0


def main(argv=None):
    import argparse
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
