"""Burst 4 — the split verdict: tune through 2022, validate on 2023 onward.

    python -m data.study_grid

Reads the trade logs data/backtest.py wrote, splits them at train_until, and
answers the only question that matters: after the cell is CHOSEN on the tuning
period, does its friction-adjusted edge survive the years it never saw?

WHAT "NAIVE" MEANS, AND WHY IT IS SHOWN AT ALL

The naive return is the same trade with every friction deleted: no slippage, no
charges, no dividend tax, no 94(7) — price move plus gross dividend over
capital. It is not a tradable number; it is the wedge's other jaw. Showing the
naive and friction-adjusted heatmaps side by side displays exactly how much of
the apparent opportunity the real world keeps, which is the finding a
dividend-capture study exists to publish.

SELECTION DISCIPLINE

The best cell is chosen on the TUNING period's friction-adjusted median, full
stop. Its validation-period row is then read once, as the unbiased estimate.
The validation heatmaps are printed for completeness, and the survivorship
count (how many tune-positive cells stay positive) is a robustness check on
the selected set — but picking a different cell because the validation panel
looks better there would be the exact snooping the split exists to prevent,
and RESULTS.md states the verdict for the selected cell only.

THE DRIFT CAVEAT TRAVELS WITH THE VERDICT

Long entries hold a stock for up to a month. A month of long-only exposure in
a rising market is positive with no dividend anywhere in sight, so a positive
long-entry cell is "beta plus maybe something", not an edge over the index.
The verdict sentence carries this explicitly; burst 5 subtracts the index
before anyone is allowed to get excited.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
import pandas as pd

from data import backtest, events, results

MARK_START = "<!-- study_grid:start -->"
MARK_END = "<!-- study_grid:end -->"
PLOT_PATH = Path(__file__).resolve().parent / "study_grid.png"

# Coarser than study_exdate's buckets: win rates need enough trades per row of
# the table to mean anything within one cell.
YIELD_BUCKETS = ((0.0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, float("inf")))

# dataviz palette: diverging = orange pole, neutral midpoint, blue pole.
INK = "#0b0b0b"
INK_SOFT = "#52514e"
SURFACE = "#fcfcfb"
DIVERGING = LinearSegmentedColormap.from_list(
    "grid_div", ["#eb6834", "#f2f0ec", "#2a78d6"])


# --- inputs -------------------------------------------------------------------


def load_trades():
    """Every cell log, with the fingerprint guard: a grid computed under other
    params or other events must be recomputed, not analyzed."""
    meta_path = backtest.BACKTEST_DIR / backtest.META_PATH_NAME
    if not meta_path.exists():
        raise RuntimeError("no grid found — run `python -m data.backtest` first")
    stored = json.loads(meta_path.read_text()).get("fingerprint")
    if stored != backtest.fingerprint():
        raise RuntimeError(
            "the stored grid was computed under different params or events — "
            "run `python -m data.backtest` to refresh it first")
    frames = [pd.read_parquet(path)
              for path in sorted(backtest.BACKTEST_DIR.glob("trades_*.parquet"))]
    if not frames:
        raise RuntimeError("grid directory has no trade logs — run data.backtest")
    return pd.concat(frames, ignore_index=True)


def with_context(trades):
    """Joins yield and liquidity context from the events table, and derives the
    naive return. Liquidity is 60-session average turnover (price x volume) —
    the number that decides whether the notional would even fill unnoticed."""
    table = pd.read_parquet(events.EVENTS_PATH)[
        ["symbol", "ex_date", "yield_pct", "avg_volume_60d", "avg_price_60d"]]
    table["turnover_60d"] = table["avg_volume_60d"] * table["avg_price_60d"]
    joined = trades.merge(table[["symbol", "ex_date", "yield_pct", "turnover_60d"]],
                          on=["symbol", "ex_date"], how="left", validate="m:1")
    capital = joined["entry_close"] * joined["quantity"]
    joined["naive_return"] = (
        (joined["exit_close"] - joined["entry_close"]) * joined["quantity"]
        + joined["dividend_gross"]) / capital
    return joined


# --- the grid views -----------------------------------------------------------


def pivot(trades, value, in_sample, entries, exits):
    """entries (desc) x exits matrix of per-cell medians for one period."""
    slice_ = trades[trades["in_sample"] == in_sample]
    grouped = slice_.groupby(["entry_days_before", "exit_days_after"])[value].median()
    frame = grouped.unstack("exit_days_after")
    frame = frame.reindex(index=sorted(entries, reverse=True), columns=sorted(exits))
    return frame


def best_cell(trades):
    """The tune-period winner: highest friction-adjusted median, required
    positive. None when nothing survives frictions even in-sample."""
    tune = trades[trades["in_sample"]]
    medians = tune.groupby(["entry_days_before", "exit_days_after"])["net_return"].median()
    if medians.empty or medians.max() <= 0:
        return None
    entry, exit_after = medians.idxmax()
    return int(entry), int(exit_after)


def cell_stats(trades, cell, in_sample):
    entry, exit_after = cell
    slice_ = trades[(trades["entry_days_before"] == entry)
                    & (trades["exit_days_after"] == exit_after)
                    & (trades["in_sample"] == in_sample)]
    if slice_.empty:
        return None
    return {
        "n": len(slice_),
        "median_return": float(slice_["net_return"].median()),
        "mean_return": float(slice_["net_return"].mean()),
        "hit_rate": float((slice_["net"] > 0).mean()),
        "total_net": float(slice_["net"].sum()),
    }


def survivorship(trades):
    """(tune-positive cells, how many stay positive in validation)."""
    tune = trades[trades["in_sample"]]
    validate = trades[~trades["in_sample"]]
    tune_medians = tune.groupby(["entry_days_before", "exit_days_after"])["net_return"].median()
    validate_medians = validate.groupby(
        ["entry_days_before", "exit_days_after"])["net_return"].median()
    positive = [cell for cell, value in tune_medians.items() if value > 0]
    held = [cell for cell in positive if validate_medians.get(cell, 0) > 0]
    return positive, held


# --- buckets ------------------------------------------------------------------


def yield_bucket(value):
    for low, high in YIELD_BUCKETS:
        if low <= value < high:
            return f"≥{low:g}%" if high == float("inf") else f"{low:g}–{high:g}%"
    return None


def liquidity_cutoffs(trades):
    """Tercile edges from TUNE-period turnover only — cutting on the full
    sample would leak validation-period information into the buckets."""
    tune = trades[trades["in_sample"]]["turnover_60d"].dropna()
    return float(tune.quantile(1 / 3)), float(tune.quantile(2 / 3))


def liquidity_bucket(value, cutoffs):
    if value != value:
        return None
    low, high = cutoffs
    if value < low:
        return "low"
    return "mid" if value < high else "high"


def bucket_win_rates(trades, cell, bucket_of, column_title):
    """[{bucket, tune n, tune win, validate n, validate win}] for one cell."""
    entry, exit_after = cell
    slice_ = trades[(trades["entry_days_before"] == entry)
                    & (trades["exit_days_after"] == exit_after)].copy()
    slice_["bucket"] = slice_.apply(bucket_of, axis=1)
    rows = []
    for bucket, group in slice_.groupby("bucket", sort=False):
        tune = group[group["in_sample"]]
        validate = group[~group["in_sample"]]
        rows.append({
            column_title: bucket,
            "tune_n": len(tune),
            "tune_win": float((tune["net"] > 0).mean()) if len(tune) else None,
            "validate_n": len(validate),
            "validate_win": float((validate["net"] > 0).mean()) if len(validate) else None,
        })
    order = {label: index for index, label in enumerate(
        [f"{low:g}–{high:g}%" if high != float("inf") else f"≥{low:g}%"
         for low, high in YIELD_BUCKETS] + ["low", "mid", "high"])}
    rows.sort(key=lambda row: order.get(row[column_title], 99))
    return rows


# --- outputs ------------------------------------------------------------------


def render_heatmaps(pivots, path=None):
    """Four panels, one shared symmetric scale centered on zero — the whole
    point is comparing panels, which a per-panel scale would quietly break."""
    path = Path(path or PLOT_PATH)
    limit = max(abs(frame.to_numpy()).max() for frame in pivots.values())
    norm = TwoSlopeNorm(vmin=-limit, vcenter=0.0, vmax=limit)

    figure, axes_grid = plt.subplots(2, 2, figsize=(11.0, 8.6),
                                     facecolor=SURFACE, layout="constrained")
    order = [("tune", "naive"), ("tune", "net"),
             ("validate", "naive"), ("validate", "net")]
    titles = {("tune", "naive"): "Tune (≤ train_until) — naive, frictionless",
              ("tune", "net"): "Tune — friction-adjusted",
              ("validate", "naive"): "Validate (after train_until) — naive",
              ("validate", "net"): "Validate — friction-adjusted"}

    image = None
    for axes, key in zip(axes_grid.flat, order):
        frame = pivots[key]
        image = axes.imshow(frame.to_numpy(), cmap=DIVERGING, norm=norm,
                            aspect="auto")
        axes.set_facecolor(SURFACE)
        axes.set_title(titles[key], loc="left", fontsize=10.5, color=INK, pad=8)
        axes.set_xticks(range(len(frame.columns)),
                        [f"x={c}" for c in frame.columns], fontsize=8.5)
        axes.set_yticks(range(len(frame.index)),
                        [f"e={r}" for r in frame.index], fontsize=8.5)
        axes.tick_params(colors=INK_SOFT, length=0)
        for spine in axes.spines.values():
            spine.set_visible(False)
        for row_index, entry in enumerate(frame.index):
            for col_index, exit_after in enumerate(frame.columns):
                value = frame.loc[entry, exit_after]
                if value != value:
                    continue
                strong = abs(value) > 0.6 * limit
                axes.text(col_index, row_index, f"{value:+.2%}",
                          ha="center", va="center", fontsize=7.5,
                          color=SURFACE if strong else INK)

    bar = figure.colorbar(image, ax=axes_grid, shrink=0.55, pad=0.02,
                          format=lambda v, _: f"{v:+.1%}")
    bar.set_label("median net return per event", fontsize=9, color=INK_SOFT)
    bar.outline.set_visible(False)
    figure.suptitle("Dividend-capture grid: entry sessions before ex-date (rows) "
                    "× exit sessions after (cols)",
                    x=0.01, ha="left", fontsize=12, color=INK, fontweight="bold")
    figure.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(figure)
    return path


def _win_table(rows, column_title):
    def pct(value):
        return f"{value:.0%}" if value is not None else "–"
    lines = [f"| {column_title} | tune n | tune win | validate n | validate win |",
             "|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row[column_title]} | {row['tune_n']:,} | "
                     f"{pct(row['tune_win'])} | {row['validate_n']:,} | "
                     f"{pct(row['validate_win'])} |")
    return lines


def verdict_sentence(cell, tune, validate):
    entry, exit_after = cell
    if validate is None or validate["n"] == 0:
        return ("**Verdict: no out-of-sample evidence either way** — the "
                "validation period contains no trades for the selected cell.")
    if validate["median_return"] > 0:
        return (
            f"**Verdict: a friction-adjusted edge survives out-of-sample in the "
            f"selected cell** — e={entry}, x={exit_after} keeps a median "
            f"{validate['median_return']:+.2%} per event ({validate['hit_rate']:.0%} "
            f"win rate, n={validate['n']:,}) on years it never saw. The required "
            f"caveat: a {entry}-session hold embeds market drift, so this is "
            f"long-only beta plus perhaps something — an edge over CASH, not yet "
            f"over the index. Burst 5 must subtract NIFTY over the same windows "
            f"before this cell is called an anomaly.")
    return (
        f"**Verdict: no friction-adjusted edge survives out-of-sample.** The "
        f"cell chosen on the tuning period (e={entry}, x={exit_after}, tuned "
        f"median {tune['median_return']:+.2%}) returns a median "
        f"{validate['median_return']:+.2%} per event out-of-sample "
        f"({validate['hit_rate']:.0%} win rate, n={validate['n']:,}). After real "
        f"frictions and taxes, the strategy as gridded does not clear zero on "
        f"unseen years — which is the system working, not failing.")


def results_markdown(trades, cell, split_date):
    stamp = datetime.now(timezone.utc).date().isoformat()
    tune_n = int(trades["in_sample"].sum())
    validate_n = int((~trades["in_sample"]).sum())
    positive, held = survivorship(trades)

    lines = [
        MARK_START,
        f"## Burst 4 — tune/validate split ({stamp})",
        "",
        f"Trades through {split_date} tune the grid ({tune_n:,} trades); "
        f"{validate_n:,} trades from later years validate it. Naive = no "
        f"frictions, no taxes; friction-adjusted = the full cost model.",
        "",
        f"![grid heatmaps](data/{PLOT_PATH.name})",
        "",
        f"{len(positive)} of 35 cells are friction-positive on the tuning period; "
        f"{len(held)} of those stay positive in validation.",
        "",
    ]
    if cell is None:
        lines += ["No cell survives frictions even on the tuning period — there "
                  "is nothing to validate.", MARK_END]
        return "\n".join(lines)

    entry, exit_after = cell
    tune = cell_stats(trades, cell, in_sample=True)
    validate = cell_stats(trades, cell, in_sample=False)
    lines += [
        f"### Selected cell: enter {entry} sessions before ex-date, "
        f"exit {exit_after} after",
        "",
        "| period | n | median return | mean return | win rate |",
        "|---|---|---|---|---|",
        f"| tune | {tune['n']:,} | {tune['median_return']:+.2%} | "
        f"{tune['mean_return']:+.2%} | {tune['hit_rate']:.0%} |",
        f"| validate | {validate['n']:,} | {validate['median_return']:+.2%} | "
        f"{validate['mean_return']:+.2%} | {validate['hit_rate']:.0%} |",
        "",
        "Win rates within the selected cell (friction-adjusted):",
        "",
    ]
    cutoffs = liquidity_cutoffs(trades)
    lines += _win_table(
        bucket_win_rates(trades, cell,
                         lambda row: yield_bucket(row["yield_pct"]), "yield"),
        "yield")
    lines.append("")
    lines += _win_table(
        bucket_win_rates(trades, cell,
                         lambda row: liquidity_bucket(row["turnover_60d"], cutoffs),
                         "liquidity"),
        "liquidity")
    lines += [
        "",
        "Liquidity buckets are terciles of 60-session average turnover with "
        "cutoffs fixed on the tuning period.",
        "",
        verdict_sentence(cell, tune, validate),
        MARK_END,
    ]
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------


def run():
    trades = with_context(load_trades())
    grid = backtest.load_backtest_params()
    entries, exits = grid["entries"], grid["exits"]

    pivots = {(period, value): pivot(trades, column, in_sample, entries, exits)
              for period, in_sample in (("tune", True), ("validate", False))
              for value, column in (("naive", "naive_return"), ("net", "net_return"))}
    plot = render_heatmaps(pivots)

    cell = best_cell(trades)
    section = results_markdown(trades, cell, grid["train_until"])
    results.update(section, MARK_START, MARK_END)

    if cell:
        tune = cell_stats(trades, cell, in_sample=True)
        validate = cell_stats(trades, cell, in_sample=False)
        print(f"[grid-study] selected on tune: e={cell[0]} x={cell[1]} "
              f"(median {tune['median_return']:+.2%}, n={tune['n']:,})")
        print(f"[grid-study] validation: median {validate['median_return']:+.2%}, "
              f"win {validate['hit_rate']:.0%}, n={validate['n']:,}")
    else:
        print("[grid-study] no cell survives frictions on the tuning period")
    positive, held = survivorship(trades)
    print(f"[grid-study] {len(positive)} tune-positive cell(s); {len(held)} hold up "
          f"in validation")
    print(f"[grid-study] wrote {plot} and the marked section of {results.RESULTS_PATH}")
    return 0


def main(argv=None):
    import argparse
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
