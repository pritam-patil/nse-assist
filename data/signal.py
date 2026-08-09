"""Personal signal report — the surviving grid cells applied to the calendar.

    python -m data.signal

Applies ONLY the parameter cells that survive the burst-7 bar (positive
out-of-sample excess over NIFTY at baseline AND under 3x slippage — the same
pre-committed rule study_stress.py wrote the verdict with) to the forthcoming
dividends table from data/upcoming.py, ranks the candidates, and displays the
expected NET return per event after the full friction model.

THE SURVIVING SET IS DERIVED, NEVER DECLARED

No cell name appears in this file. The survivors are recomputed from the
stored trade logs every run, because the honest answer changes with the
evidence and a hard-coded list is how a dead strategy keeps trading. As of the
verdict in RESULTS.md the set is EMPTY — zero of twelve cells clear 3x
slippage — and this module's correct output is exactly that sentence. It will
say something else only when the stored evidence does.

WHAT "EXPECTED" MEANS HERE, PRECISELY

The expectation shown is the cell's out-of-sample MEDIAN net return, at the
backtest's notional, with the friction model already inside it. The caveat
beside it is the same distribution's interquartile range: half the validation
trades landed inside that band, a quarter below it. A median is an
expectation only in that weak sense, and the report prints the band beside
every number so the weak sense is the one you read.

Eligibility is approximate on purpose: a cell entering e sessions before the
ex-date needs the ex-date to still be at least e trading sessions away, and
sessions are estimated from calendar days at 5 per 7. The report is planning
aid, not an order ticket.

PERSONAL USE. Decision support on end-of-day data, based on the backtest
period stated in every report. Not advice, not a recommendation, and the
standing verdict in RESULTS.md is that this strategy does not beat the index
after realistic execution costs.
"""

import argparse
import math

import pandas as pd

from data import backtest, study_exdate, study_grid, study_specials, study_stress, upcoming

SESSIONS_PER_WEEK = 5
CALENDAR_DAYS_PER_SESSION = 7 / SESSIONS_PER_WEEK

# The burst-7 bar, restated: OOS paired excess must be positive at baseline
# and remain positive under the harshest slippage stress in the battery.
STRESS_MULTIPLIER = study_stress.SLIPPAGE_MULTIPLIERS[-1]


def _oos(trades, cell):
    return study_stress.cell_slice(trades, cell, in_sample=False)


def surviving_cells(trades=None, closes=None):
    """[{cell, stats}] for every tune-positive cell clearing the burst-7 bar.

    Recomputed from the stored grid: baseline excess from the logs, stressed
    excess from a true 3x re-simulation. Empty is a first-class result.
    """
    if trades is None:
        trades = study_grid.with_context(study_specials.load_grid_trades())
    if closes is None:
        closes = study_exdate.nifty_closes(refresh=False)
    tune_positive, _ = study_grid.survivorship(trades)

    survivors = []
    stressed_all = (study_stress.resimulate_cells(tune_positive, STRESS_MULTIPLIER)
                    if tune_positive else pd.DataFrame())
    for cell in tune_positive:
        base, _ = study_stress.with_nifty(_oos(trades, cell), closes)
        if base.empty or base["excess"].median() <= 0:
            continue
        merged = _oos(stressed_all, cell).merge(
            base[["symbol", "ex_date", "nifty_return"]],
            on=["symbol", "ex_date"], how="inner", validate="1:1")
        if merged.empty:
            continue
        stressed_excess = (merged["net_return"] - merged["nifty_return"]).median()
        if stressed_excess <= 0:
            continue
        returns = base["net_return"]
        survivors.append({
            "cell": cell,
            "n": len(base),
            "median_return": float(returns.median()),
            "p25": float(returns.quantile(0.25)),
            "p75": float(returns.quantile(0.75)),
            "hit_rate": float((base["net"] > 0).mean()),
            "median_excess": float(base["excess"].median()),
            "stressed_excess": float(stressed_excess),
        })
    return survivors


def periods(trades):
    """(tune span, validate span) so every report names what it stands on."""
    tune = trades[trades["in_sample"]]["ex_date"]
    validate = trades[~trades["in_sample"]]["ex_date"]
    def span(series):
        return (f"{series.min().date()} to {series.max().date()}"
                if len(series) else "empty")
    return span(tune), span(validate)


def eligible(table, entry_sessions, today=None):
    """Calendar rows this cell can still enter: a dated ex-date at least
    entry_sessions trading sessions away (approximated from calendar days).
    TBA rows cannot be scheduled and are excluded."""
    today = pd.Timestamp(today or pd.Timestamp.now().normalize())
    needed_days = math.ceil(entry_sessions * CALENDAR_DAYS_PER_SESSION)
    dated = table.dropna(subset=["ex_date"])
    return dated[dated["ex_date"] >= today + pd.Timedelta(days=needed_days)]


def rank(candidates):
    """Highest estimated yield first — within one cell every candidate carries
    the same cell-level expectation, so yield is the only per-event ordering
    the evidence supports."""
    return candidates.sort_values(
        ["est_yield_pct", "symbol"], ascending=[False, True],
        na_position="last").reset_index(drop=True)


# --- report -------------------------------------------------------------------


def _no_survivors_text(trades):
    tune_span, validate_span = periods(trades)
    return (
        "[signal] No parameter cell survives the burst-7 bar (positive "
        "out-of-sample excess over NIFTY at baseline AND under 3x slippage).\n"
        "[signal] By the study's own pre-committed rule there is NOTHING TO "
        "SIGNAL — the verdict in RESULTS.md stands: the strategy does not "
        "beat parking the same capital in the index after realistic "
        "execution costs.\n"
        f"[signal] Evidence base: tuned on {tune_span}, validated on "
        f"{validate_span}. What would change this output is measured (not "
        "assumed) execution costs, per docs/dividend-study.md — not a rerun.")


def report(survivors, trades, table, notional, today=None):
    """The printed report as a list of lines (tested as text, printed by run)."""
    tune_span, validate_span = periods(trades)
    lines = [
        "[signal] PERSONAL USE — decision support, not advice.",
        f"[signal] Basis: grid tuned on {tune_span}; expectations and "
        f"dispersion from validation on {validate_span}; notional "
        f"{notional:,.0f} per event; full friction model inside every number.",
    ]
    for survivor in survivors:
        entry, exit_after = survivor["cell"]
        lines.append(
            f"[signal] cell e={entry} x={exit_after}: OOS median "
            f"{survivor['median_return']:+.2%} per event "
            f"(n={survivor['n']:,}, hit {survivor['hit_rate']:.0%}, excess vs "
            f"NIFTY {survivor['median_excess']:+.2%}, still "
            f"{survivor['stressed_excess']:+.2%} at 3x slippage)")
        lines.append(
            f"[signal]   confidence: half the validation trades landed "
            f"between {survivor['p25']:+.2%} and {survivor['p75']:+.2%}; a "
            f"quarter did worse than the low end. The median is the "
            f"expectation only in that weak sense.")
        candidates = rank(eligible(table, entry, today))
        if candidates.empty:
            lines.append("[signal]   no eligible candidates on the calendar "
                         f"(need an ex-date ≥ {entry} sessions away)")
            continue
        expected_net = survivor["median_return"] * notional
        for row in candidates.itertuples():
            est = (f"{row.est_yield_pct:.2f}%" if pd.notna(row.est_yield_pct)
                   else "–")
            amount = f"{row.amount:.2f}" if pd.notna(row.amount) else "–"
            lines.append(
                f"[signal]   {row.symbol:<12} ex {row.ex_date.date()}  "
                f"amount {amount:>8}  est yield {est:>7}  "
                f"liquidity {row.liquidity:<8} expected net "
                f"~{expected_net:+,.0f} (cell median, see band above)")
    return lines


def run(today=None):
    trades = study_grid.with_context(study_specials.load_grid_trades())
    closes = study_exdate.nifty_closes(refresh=False)
    survivors = surviving_cells(trades, closes)

    if not survivors:
        print(_no_survivors_text(trades))
        return 0

    if not upcoming.OUT_PATH.exists():
        print("[signal] no calendar snapshot — run `python -m data.upcoming` first")
        return 1
    table = pd.read_parquet(upcoming.OUT_PATH)
    notional = backtest.load_backtest_params()["notional"]
    for line in report(survivors, trades, table, notional, today):
        print(line)
    return 0


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
