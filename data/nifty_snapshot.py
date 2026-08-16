"""A small, committed stand-in for NIFTY's price history — just its closes.

    python -m data.nifty_snapshot     # run locally, then commit the diff

data/cache/^NSEI.parquet (NIFTY's full OHLCV history, warmed by data.fetch
like any other cached symbol) is gitignored along with the rest of
data/cache/ — regenerable, and the point of this repo is the code and the
verdict, not a private mirror of the exchange. But study_exdate.nifty_closes()
needs NIFTY's daily closes to pair every backtest trade against the index's
return over that SAME trade's own dates, and a bare CI runner has no cache to
read them from. Committing data/grid/ gave the runner the trades; without
this, it still had nothing to compare them against, and notify._survivors()
kept landing on its fail-safe empty answer for a second, separate reason.

WHAT'S IN IT, AND WHY IT NEVER NEEDS TO GROW

Just date and close, one row per session — nothing downstream of
nifty_closes() reads open/high/low/volume/dividend/split. ~2,500 rows for the
full backtest window, tens of KB, nowhere near the 47MB full price cache.

THE COUPLING THAT MATTERS: REFRESH THIS WHENEVER THE GRID IS REGENERATED

data/grid/'s trades and this snapshot must cover the same date range. If the
backtest is ever rerun with a wider window and this snapshot is NOT refreshed
alongside it, with_nifty() does not error — it silently DROPS every trade it
cannot pair against a close. A runner would then compute a verdict from an
incomplete slice of the new grid rather than fail loudly. THREE commits, not
one, every time the backtest is rerun: data/backtest.py's output, this, AND
data/events.parquet (study_grid.with_context() reads that directly for the
yield/liquidity join — found from a real CI run, not local testing, because
an earlier "bare runner" simulation only isolated the price cache and kept
silently reading the real local events.parquet). See docs/notifications.md.
"""

import argparse
from pathlib import Path

import pandas as pd

from data import fetch

SNAPSHOT_PATH = Path(__file__).resolve().parent / "nifty_snapshot.csv"

# Duplicated from study_exdate.NIFTY_SYMBOL rather than imported: this module
# builds the snapshot study_exdate falls back to reading, and importing
# study_exdate here for one string would be a dependency this module does not
# otherwise need. A consistency test asserts the two stay equal.
NIFTY_SYMBOL = "^NSEI"

COLUMNS = ("date", "close")


def build_snapshot():
    """NIFTY's cached daily closes as a plain (date, close) frame, or an
    empty one with the right schema if nothing is cached to read."""
    frame = fetch.read_cache(NIFTY_SYMBOL)
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(COLUMNS))
    tidy = frame[["date", "close"]].sort_values("date").reset_index(drop=True)
    tidy["date"] = tidy["date"].dt.date.astype(str)
    return tidy


def write_snapshot(frame, path=None):
    path = Path(path or SNAPSHOT_PATH)
    frame.to_csv(path, index=False)
    return path


def read_snapshot(path=None):
    path = Path(path or SNAPSHOT_PATH)
    if not path.exists():
        return None
    frame = pd.read_csv(path, parse_dates=["date"])
    return frame if not frame.empty else None


def snapshot_closes(path=None):
    """{Timestamp: close} — the exact shape study_exdate.nifty_closes()
    returns from the live cache, so the fallback is a drop-in, not a special
    case its caller has to know about. {} when there is no snapshot yet."""
    frame = read_snapshot(path)
    if frame is None:
        return {}
    return dict(zip(frame["date"], frame["close"]))


# --- CLI ------------------------------------------------------------------


def run():
    frame = build_snapshot()
    if frame.empty:
        print(f"[nifty-snapshot] no local NIFTY history cached — run "
              f"`python -m data.fetch --symbols {NIFTY_SYMBOL}` first, then this")
        return 1
    path = write_snapshot(frame)
    print(f"[nifty-snapshot] wrote {len(frame)} session(s) to {path} "
          f"({frame['date'].min()} to {frame['date'].max()}) — commit this "
          f"ALONGSIDE data/grid/ whenever the backtest is rerun")
    return 0


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
