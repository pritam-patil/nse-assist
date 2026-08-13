"""A small, committed stand-in for the price cache — just what the digest needs.

    python -m data.liquidity_snapshot     # run locally, then commit the diff

data/cache/ (the ten-year price history data.fetch warms, ~47MB) is gitignored
on purpose: regenerable, and too large for a repo whose point is the code and
the verdict, not a mirror of Yahoo. But upcoming.py's calendar table needs a
LATEST CLOSE and a LIQUIDITY TERCILE per symbol to estimate a dividend's yield
and flag how liquid the name is — and a bare CI runner has none of the cache
to compute either from. Committing data/grid/ fixed whether the model has an
opinion at all; it did nothing for this, a separate gap with a separate cause.

This module extracts exactly the two numbers cache_context() actually needs —
nothing else from the ten years of history — into a few KB, committed. A
runner without the full cache then has real numbers instead of "unknown" on
every single row.

REFRESH BY RERUNNING AND COMMITTING, THERE IS NO AUTOMATION HERE

Deliberately not wired into any GitHub workflow: refreshing it needs the full
local price cache, which no runner has — that is the entire premise this
module exists to work around. Weekly is plenty; a dividend yield estimate is
amount-over-price, and a stale price only matters if it moved enough to cross
a yield bucket boundary (1%, 5%) in the meantime, which a week rarely does.
Run this locally after `data.fetch`, commit the small diff — a few hundred
rows, most numbers barely moving — the same rhythm as any other committed
snapshot in this repo.

STALENESS IS LABELED, NOT HIDDEN

cache_context() prints how old the snapshot is whenever it falls back to it,
so a digest built from a stale close says so rather than reading like today's
number. See its docstring in upcoming.py.
"""

import argparse
from pathlib import Path

import pandas as pd

from data import events, fetch

SNAPSHOT_PATH = Path(__file__).resolve().parent / "liquidity_snapshot.csv"

# Matches upcoming.LIQUIDITY_SESSIONS — not imported from there to avoid a
# circular import (upcoming.py imports this module for the fallback). Tests
# assert the two stay equal.
LIQUIDITY_SESSIONS = 60

COLUMNS = ("symbol", "asof_date", "close", "avg_turnover_60d")


def build_snapshot(symbols=None):
    """One row per symbol with cached history: its latest close and the same
    60-session average turnover (price x volume) study_grid.py uses for its
    liquidity terciles — so a runner's tercile split is computed the same way
    a local run's would be, just against slightly older numbers."""
    rows = []
    for symbol in sorted(symbols or events.cached_symbols()):
        frame = fetch.read_cache(symbol)
        if frame is None or frame.empty:
            continue
        tail = frame.sort_values("date").tail(LIQUIDITY_SESSIONS)
        rows.append({
            "symbol": symbol,
            "asof_date": tail["date"].iloc[-1].date().isoformat(),
            "close": float(tail["close"].iloc[-1]),
            "avg_turnover_60d": float((tail["close"] * tail["volume"]).mean()),
        })
    return pd.DataFrame(rows, columns=list(COLUMNS))


def write_snapshot(frame, path=None):
    path = Path(path or SNAPSHOT_PATH)
    frame.sort_values("symbol").to_csv(path, index=False)
    return path


def read_snapshot(path=None):
    path = Path(path or SNAPSHOT_PATH)
    if not path.exists():
        return None
    frame = pd.read_csv(path, parse_dates=["asof_date"])
    return frame if not frame.empty else None


def snapshot_context(path=None):
    """({symbol: (close, avg_turnover_60d)}, as-of date) from the committed
    file. Empty dict and None date when it doesn't exist yet — a first run
    with no snapshot ever written is not an error, just nothing to fall back
    on, exactly like an empty live cache."""
    frame = read_snapshot(path)
    if frame is None:
        return {}, None
    context = {row.symbol: (row.close, row.avg_turnover_60d)
              for row in frame.itertuples()}
    return context, frame["asof_date"].max()


# --- CLI ------------------------------------------------------------------


def run():
    symbols = events.cached_symbols()
    if not symbols:
        print("[liquidity-snapshot] no local price cache — run "
              "`python -m data.fetch` first, then this")
        return 1
    frame = build_snapshot(symbols)
    path = write_snapshot(frame)
    print(f"[liquidity-snapshot] wrote {len(frame)} symbol(s) to {path} "
          f"(as of {frame['asof_date'].max()}) — commit this")
    return 0


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
