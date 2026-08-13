"""Burst 1 — the empirical ex-date drop ratio, measured on our own data.

    python -m data.study_exdate
    python -m data.study_exdate --no-refresh    # offline; use cached NIFTY as-is

THE QUESTION, BEFORE ANY STRATEGY EXISTS

Theory says a stock opens ex-dividend lower by the dividend; tax clienteles say
the drop is usually less than that. Every dividend-capture idea lives or dies on
which is true HERE — NSE, our 500 symbols, our decade. So this burst measures it
and nothing else: no entries, no exits, no P&L. The number this produces is the
prior every later burst argues against.

WHAT IS ACTUALLY MEASURED

Close-to-close, market-adjusted, per event:

    stock return   r_s = ex_close / prev_close - 1
    market return  r_m = NIFTY on the same two sessions
    drop_ratio     = -(r_s - r_m) * prev_close / dividend

A ratio of 1.0 means the price gave up exactly the dividend after removing the
market's own move; 0.6 means it gave up 60% of it. Honesty notes, in order of
how much they matter:

  * The denominator is the dividend, so NOISE SCALES AS 1/YIELD: a 0.1%-yield
    event carries ~15x the noise of a 1.5% one, and a mean over all events is
    mostly noise. Buckets and medians are not presentation choices here; they
    are the measurement. The headline number comes from yield >= 1%.
  * Close-to-close includes a full session of drift after the open, not the
    open-gap alone. EOD data cannot do better; burst 3's entry/exit grids use
    the same convention, so at least the bias is shared.
  * Market adjustment assumes beta 1 for every stock. Good enough at the
    median; wrong in the tails.

The study never writes into the cache or the pipeline DB. It writes
data/study_exdate.png and its section of RESULTS.md, and leaves everything else
alone.
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")   # headless: this renders to a file, never to a window
import matplotlib.pyplot as plt
import pandas as pd

from data import events, fetch, results

NIFTY_SYMBOL = "^NSEI"

RESULTS_PATH = results.RESULTS_PATH
PLOT_PATH = Path(__file__).resolve().parent / "study_exdate.png"

# RESULTS.md is a log that later bursts will also write into. Each study owns
# the slice between its own markers and must not touch anything outside them.
MARK_START = "<!-- study_exdate:start -->"
MARK_END = "<!-- study_exdate:end -->"

# Bucket edges chosen where the noise argument changes, not at round numbers for
# their own sake: below 0.25% yield the ratio is ~all noise, 1% is where a
# single event's ratio starts meaning something, 5%+ is special-dividend land.
BUCKETS = ((0.0, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 5.0),
           (5.0, float("inf")))
HEADLINE_MIN_YIELD = 1.0

# dataviz reference palette, light mode: one data hue, neutral ink, recessive
# grid. A single-series research figure needs nothing more.
INK = "#0b0b0b"
INK_SOFT = "#52514e"
BLUE = "#2a78d6"
BLUE_SOFT = "#a8c8ec"
SURFACE = "#fcfcfb"
GRID = "#e7e6e2"


# --- inputs -------------------------------------------------------------------


def nifty_closes(refresh=True):
    """{Timestamp: close} for NIFTY, warmed through the ordinary cache machinery.

    A refresh failure degrades to the cached copy with a note — the study is
    re-runnable offline. A missing LIVE cache (a bare CI runner: data/cache/
    is never checked out there) falls back to the committed snapshot in
    data/nifty_snapshot.py before giving up — see that module for what it is
    and the coupling it has to data/grid/. Only when NEITHER source has
    anything is this fatal: without the index there is no market adjustment,
    and an unadjusted study would be a different study.
    """
    if refresh:
        try:
            fetch.refresh([NIFTY_SYMBOL])
        except Exception as exc:
            print(f"[study] NIFTY refresh failed ({exc}); using the cached series")
    frame = fetch.read_cache(NIFTY_SYMBOL)
    if frame is not None and not frame.empty:
        return dict(zip(frame["date"], frame["close"]))

    from data import nifty_snapshot
    closes = nifty_snapshot.snapshot_closes()
    if closes:
        print(f"[study] no live NIFTY cache — using the committed snapshot "
              f"({len(closes)} session(s); refresh it if the grid was "
              f"regenerated more recently, see docs/notifications.md)")
        return closes

    raise RuntimeError(
        f"no NIFTY history cached and no committed snapshot — run "
        f"`python -m data.fetch --symbols {NIFTY_SYMBOL}` once, or drop --no-refresh")


def load_events():
    """The event table, rebuilt in place if the stored parquet predates a column."""
    if events.EVENTS_PATH.exists():
        table = pd.read_parquet(events.EVENTS_PATH)
        if not set(events.COLUMNS) - set(table.columns):
            return table
        print("[study] events.parquet has an older schema — rebuilding from the cache")
    return events.build_events()


# --- measurement --------------------------------------------------------------


def measure(table, nifty):
    """(measured frame, exclusion counts). One row per event that can be measured.

    Exclusions are counted by reason and reported, never silently dropped: 'we
    measured 4,800 of 4,982' is part of the result.
    """
    exclusions = {"no prior session": 0, "index missing a session": 0}
    rows = []
    for row in table.itertuples():
        if pd.isna(row.prev_close) or pd.isna(row.prev_date):
            exclusions["no prior session"] += 1
            continue
        market_prev = nifty.get(row.prev_date)
        market_ex = nifty.get(row.ex_date)
        if market_prev is None or market_ex is None:
            exclusions["index missing a session"] += 1
            continue
        stock_return = row.ex_close / row.prev_close - 1
        market_return = market_ex / market_prev - 1
        adjusted_drop = -(stock_return - market_return) * row.prev_close
        rows.append({
            "symbol": row.symbol,
            "ex_date": row.ex_date,
            "amount": row.amount,
            "yield_pct": row.yield_pct,
            "adjusted_drop": adjusted_drop,
            "drop_ratio": adjusted_drop / row.amount,
        })
    return pd.DataFrame(rows), exclusions


def bucket_label(yield_pct):
    for low, high in BUCKETS:
        if low <= yield_pct < high:
            if high == float("inf"):
                return f"≥{low:g}%"
            return f"{low:g}–{high:g}%"
    return None


def _stats(ratios):
    quantiles = ratios.quantile([0.25, 0.5, 0.75])
    return {"n": len(ratios), "median": quantiles[0.5],
            "p25": quantiles[0.25], "p75": quantiles[0.75]}


def summarize(measured):
    """Headline subsets and the per-bucket table, medians and IQRs throughout."""
    ratios = measured["drop_ratio"]
    summary = {
        "all": _stats(ratios),
        "headline": _stats(measured[measured["yield_pct"] >= HEADLINE_MIN_YIELD]["drop_ratio"]),
        "high": _stats(measured[measured["yield_pct"] >= 2.0]["drop_ratio"]),
        "buckets": [],
    }
    for low, high in BUCKETS:
        inside = measured[(measured["yield_pct"] >= low) & (measured["yield_pct"] < high)]
        if len(inside):
            label = bucket_label(low if low else 0.0)
            summary["buckets"].append({"label": label, **_stats(inside["drop_ratio"])})
    return summary


# --- outputs ------------------------------------------------------------------


def render_plot(measured, summary, path=None):
    """Two panels: the ratio by yield bucket, and its distribution where it is
    measurable. Median dots with IQR whiskers on the left — with noise scaling as
    1/yield, means and full ranges would draw the noise, not the effect."""
    path = Path(path or PLOT_PATH)
    figure, (left, right) = plt.subplots(
        1, 2, figsize=(10.0, 4.6), facecolor=SURFACE, layout="constrained",
        gridspec_kw={"width_ratios": [1.15, 1.0], "wspace": 0.06})

    for axes in (left, right):
        axes.set_facecolor(SURFACE)
        for side in ("top", "right", "left"):
            axes.spines[side].set_visible(False)
        axes.spines["bottom"].set_color(GRID)
        axes.tick_params(colors=INK_SOFT, labelsize=9)
        axes.yaxis.grid(True, color=GRID, linewidth=0.8)
        axes.set_axisbelow(True)

    # Left: median + IQR per bucket.
    buckets = summary["buckets"]
    positions = range(len(buckets))
    left.axhline(1.0, color=INK_SOFT, linewidth=1.0, linestyle=(0, (4, 3)))
    # Top-right corner, clear of every whisker and value label — anchoring this
    # to the line itself collides with whichever bucket's median sits near 1.0.
    left.text(0.99, 0.96, "dashed line = full drop (1.0)",
              transform=left.transAxes, ha="right", va="top",
              fontsize=8.5, color=INK_SOFT)
    left.axhline(0.0, color=GRID, linewidth=1.0)
    for position, bucket in zip(positions, buckets):
        left.plot([position, position], [bucket["p25"], bucket["p75"]],
                  color=BLUE_SOFT, linewidth=3.5, solid_capstyle="round", zorder=2)
        left.plot(position, bucket["median"], "o", color=BLUE, markersize=8, zorder=3)
        # The last bucket labels leftward so the value never leaves the axes.
        rightward = position < len(buckets) - 1
        left.annotate(f"{bucket['median']:.2f}", (position, bucket["median"]),
                      textcoords="offset points",
                      xytext=(8, -3) if rightward else (-8, -3),
                      ha="left" if rightward else "right",
                      fontsize=9, color=INK,
                      bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1})
    left.set_xticks(list(positions))
    left.set_xticklabels([f"{b['label']}\nn={b['n']:,}" for b in buckets])
    left.set_ylim(-3.5, 3.5)
    left.set_title("Ex-date drop ratio by dividend yield — median and IQR",
                   loc="left", fontsize=10.5, color=INK, pad=10)
    left.set_xlabel("dividend yield bucket", fontsize=9, color=INK_SOFT)

    # Right: the distribution where the ratio is measurable at all.
    subset = measured[measured["yield_pct"] >= HEADLINE_MIN_YIELD]["drop_ratio"]
    clipped = subset.clip(-2, 4)
    outside = int((subset < -2).sum() + (subset > 4).sum())
    right.hist(clipped, bins=48, color=BLUE, edgecolor=SURFACE, linewidth=0.6)
    median = summary["headline"]["median"]
    right.axvline(median, color=INK, linewidth=1.2)
    right.annotate(f"median {median:.2f}", (median, right.get_ylim()[1]),
                   textcoords="offset points", xytext=(-8, -12), ha="right",
                   fontsize=9, color=INK,
                   bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1})
    right.axvline(1.0, color=INK_SOFT, linewidth=1.0, linestyle=(0, (4, 3)))
    right.set_title(f"Distribution, yield ≥ {HEADLINE_MIN_YIELD:g}% "
                    f"(n={summary['headline']['n']:,})",
                    loc="left", fontsize=10.5, color=INK, pad=10)
    right.set_xlabel(f"drop ratio (view clipped to [-2, 4]; {outside} outside)",
                     fontsize=9, color=INK_SOFT)

    figure.suptitle("How much of the dividend does the price actually give up?",
                    x=0.01, ha="left", fontsize=12, color=INK, fontweight="bold")
    figure.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(figure)
    return path


def results_markdown(summary, exclusions, total_events, window):
    """The section between the markers — regenerated whole on every run."""
    stamp = datetime.now(timezone.utc).date().isoformat()
    measured = summary["all"]["n"]
    dropped = ", ".join(f"{count} {reason}" for reason, count in exclusions.items()
                        if count) or "none"

    def line(label, stats):
        return (f"| {label} | {stats['n']:,} | {stats['median']:.2f} | "
                f"{stats['p25']:.2f} to {stats['p75']:.2f} |")

    rows = [line(f"yield ≥ 2%", summary["high"]),
            line(f"yield ≥ {HEADLINE_MIN_YIELD:g}% (headline)", summary["headline"]),
            line("all measurable events", summary["all"])]
    bucket_rows = [line(bucket["label"], bucket) for bucket in summary["buckets"]]

    return "\n".join([
        MARK_START,
        f"## Burst 1 — ex-date drop ratio ({stamp})",
        "",
        f"How much of the dividend the price gives up on ex-date, close-to-close,",
        f"adjusted for NIFTY's same-day move (beta 1), as a fraction of the dividend.",
        f"1.0 = the full dividend. Measured on {measured:,} of {total_events:,} events,",
        f"{window}. Excluded: {dropped}.",
        "",
        "| subset | n | median ratio | IQR |",
        "|---|---|---|---|",
        *rows,
        "",
        "By yield bucket (noise scales as 1/yield — read the low buckets as noise",
        "floors, not findings):",
        "",
        "| yield bucket | n | median ratio | IQR |",
        "|---|---|---|---|",
        *bucket_rows,
        "",
        f"![ex-date drop ratio](data/{PLOT_PATH.name})",
        "",
        "Descriptive statistics on history — no strategy is implied, and a ratio",
        "below 1.0 is a pre-tax, pre-cost observation about averages, not an edge.",
        "Regenerate with `python -m data.study_exdate`.",
        MARK_END,
    ])


def write_results(section, path=None):
    """This study's slice of RESULTS.md; the surgery lives in data/results.py."""
    return results.update(section, MARK_START, MARK_END,
                          path=path or RESULTS_PATH)


# --- CLI ----------------------------------------------------------------------


def run(refresh=True):
    table = load_events()
    if table.empty:
        print("[study] no events — run `python -m data.events` first")
        return 1
    measured, exclusions = measure(table, nifty_closes(refresh=refresh))
    if measured.empty:
        print("[study] nothing measurable — is the NIFTY series aligned with the cache?")
        return 1

    summary = summarize(measured)
    window = (f"ex-dates {table['ex_date'].min().date()} to "
              f"{table['ex_date'].max().date()}")
    plot = render_plot(measured, summary)
    results = write_results(results_markdown(summary, exclusions, len(table), window))

    print(f"[study] measured {summary['all']['n']:,} of {len(table):,} events; "
          + "; ".join(f"{v} excluded ({k})" for k, v in exclusions.items() if v))
    print(f"[study] drop ratio, yield >= {HEADLINE_MIN_YIELD:g}%: "
          f"median {summary['headline']['median']:.2f} "
          f"(IQR {summary['headline']['p25']:.2f} to {summary['headline']['p75']:.2f}, "
          f"n={summary['headline']['n']:,})")
    print(f"[study] drop ratio, yield >= 2%: median {summary['high']['median']:.2f} "
          f"(n={summary['high']['n']:,})")
    print(f"[study] wrote {plot} and the marked section of {results}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure the ex-date drop ratio across the cached events and "
                    "write the findings to RESULTS.md.")
    parser.add_argument("--no-refresh", action="store_true",
                        help="do not refresh the NIFTY series first (offline)")
    args = parser.parse_args(argv)
    return run(refresh=not args.no_refresh)


if __name__ == "__main__":
    raise SystemExit(main())
