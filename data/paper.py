"""Paper tracking — every signal logged at birth, graded by what then happened.

    python -m data.paper log       # after data.signal: record this run's signals
    python -m data.paper fill      # after data.fetch: capture actual prices
    python -m data.paper report    # the monthly report and the real-money gate

THE RULE, ENCODED IN EVERY REPORT HEADER

No real money before three months of paper tracking within expected
dispersion. Operationally, all three simultaneously:

  1. at least three calendar months since the first logged run,
  2. at least MIN_CLOSED_TRADES closed paper trades (the repo's long-standing
     floor below which a median is an anecdote),
  3. the realized median return inside the backtest's out-of-sample IQR — the
     band frozen into each signal row AT LOG TIME, so later re-runs of the
     backtest cannot quietly re-grade old paper.

Today the signal module emits nothing (no cell survives the burst-7 bar), so
`log` records runs with zero candidates. That is not a waste: the run log IS
the evidence the clock demands — a system that was live and honest about
having nothing to say.

WHY THE LEDGER IS COMMITTED CSV, NOT GITIGNORED PARQUET

The ledger is state, not derived data: losing it resets the three-month clock
and orphans every filled price. CSV keeps the diffs reviewable — a paper trade
appearing in history is a one-line change a human can read, the same reasoning
that keeps the pipeline's SQLite committed.

Prices are captured from the cache on `fill`, at the same session offsets the
backtest used (entry e sessions before the ex-date bar, exit x after, closes),
and the realized net goes through the SAME friction model. The comparison
against expectations is therefore convention-for-convention: any gap between
paper and backtest is the world disagreeing, not the bookkeeping.
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data import backtest, fetch, frictions, signal, upcoming

PAPER_DIR = Path(__file__).resolve().parent / "paper"
SIGNALS_NAME = "signals.csv"
RUNS_NAME = "runs.csv"

MIN_CLOSED_TRADES = 30
TRACKING_MONTHS = 3

STATUS_PENDING = "pending"
STATUS_CLOSED = "closed"
STATUS_SKIPPED = "skipped"

SIGNAL_COLUMNS = [
    "logged_at", "entry_days_before", "exit_days_after", "symbol", "ex_date",
    "expected_median_return", "expected_p25", "expected_p75",
    "status", "entry_date", "entry_close", "exit_date", "exit_close",
    "dividend", "realized_net", "realized_return",
]


def signals_path():
    return PAPER_DIR / SIGNALS_NAME


def runs_path():
    return PAPER_DIR / RUNS_NAME


def _now():
    return pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)


def read_signals():
    path = signals_path()
    if not path.exists():
        return pd.DataFrame(columns=SIGNAL_COLUMNS)
    return pd.read_csv(path, parse_dates=["logged_at", "ex_date", "entry_date",
                                          "exit_date"])


def write_signals(frame):
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    ordered = frame.sort_values(["ex_date", "symbol", "entry_days_before"])
    ordered.to_csv(signals_path(), index=False)


def read_runs():
    path = runs_path()
    if not path.exists():
        return pd.DataFrame(columns=["ran_at", "survivors", "candidates_logged"])
    return pd.read_csv(path, parse_dates=["ran_at"])


# --- log ----------------------------------------------------------------------


def log(today=None):
    """One signal-generation run, recorded: the surviving cells derived fresh,
    their eligible candidates appended (deduplicated — a signal is one
    (cell, symbol, ex-date), however many runs see it)."""
    trades = signal.study_grid.with_context(signal.study_specials.load_grid_trades())
    closes = signal.study_exdate.nifty_closes(refresh=False)
    survivors = signal.surviving_cells(trades, closes)

    logged = 0
    if survivors:
        if not upcoming.OUT_PATH.exists():
            print("[paper] no calendar snapshot — run `python -m data.upcoming` first")
            return 1
        table = pd.read_parquet(upcoming.OUT_PATH)
        existing = read_signals()
        seen = {(row.entry_days_before, row.symbol, row.ex_date)
                for row in existing.itertuples()}
        fresh = []
        for survivor in survivors:
            entry, exit_after = survivor["cell"]
            for row in signal.eligible(table, entry, today).itertuples():
                key = (entry, row.symbol, row.ex_date)
                if key in seen:
                    continue
                seen.add(key)
                fresh.append({
                    "logged_at": _now(), "entry_days_before": entry,
                    "exit_days_after": exit_after, "symbol": row.symbol,
                    "ex_date": row.ex_date,
                    # Frozen now: the band this signal will be graded against.
                    "expected_median_return": survivor["median_return"],
                    "expected_p25": survivor["p25"],
                    "expected_p75": survivor["p75"],
                    "status": STATUS_PENDING,
                    "entry_date": pd.NaT, "entry_close": None,
                    "exit_date": pd.NaT, "exit_close": None,
                    "dividend": None, "realized_net": None,
                    "realized_return": None,
                })
        if fresh:
            write_signals(pd.concat([existing, pd.DataFrame(fresh)],
                                    ignore_index=True))
            logged = len(fresh)

    runs = pd.concat([read_runs(), pd.DataFrame([{
        "ran_at": _now(), "survivors": len(survivors),
        "candidates_logged": logged,
    }])], ignore_index=True)
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    runs.to_csv(runs_path(), index=False)
    print(f"[paper] run logged: {len(survivors)} surviving cell(s), "
          f"{logged} new signal(s)")
    return 0


def record(cell, symbol, ex_date, expected, logged_at=None):
    """Appends ONE signal to the ledger, deduplicated; returns True when new.

    This is the hook notify.py calls at send time, so an alert and its paper
    row are the same act — the two records cannot diverge because there is
    only one write. `expected` carries median_return/p25/p75 frozen now.
    """
    entry, exit_after = cell
    existing = read_signals()
    key = (entry, symbol, pd.Timestamp(ex_date))
    seen = {(row.entry_days_before, row.symbol, row.ex_date)
            for row in existing.itertuples()}
    if key in seen:
        return False
    row = pd.DataFrame([{
        "logged_at": logged_at or _now(), "entry_days_before": entry,
        "exit_days_after": exit_after, "symbol": symbol,
        "ex_date": pd.Timestamp(ex_date),
        "expected_median_return": expected["median_return"],
        "expected_p25": expected["p25"], "expected_p75": expected["p75"],
        "status": STATUS_PENDING, "entry_date": pd.NaT, "entry_close": None,
        "exit_date": pd.NaT, "exit_close": None, "dividend": None,
        "realized_net": None, "realized_return": None,
    }])
    write_signals(pd.concat([existing, row], ignore_index=True))
    return True


# --- fill ---------------------------------------------------------------------


def fill(cfg=None, notional=None):
    """Captures actual closes for pending signals whose sessions now exist in
    the cache, and settles them through the friction model. A signal whose
    dates have not arrived stays pending — silence, not a guess."""
    ledger = read_signals()
    pending = ledger[ledger["status"] == STATUS_PENDING]
    if pending.empty:
        print("[paper] nothing pending")
        return 0
    cfg = cfg or frictions.Config.from_params()
    notional = notional or backtest.load_backtest_params()["notional"]

    filled = 0
    for index, row in pending.iterrows():
        frame = fetch.read_cache(row["symbol"])
        if frame is None or frame.empty:
            continue
        frame = frame.sort_values("date").reset_index(drop=True)
        positions = {stamp: i for i, stamp in enumerate(frame["date"])}
        ex_index = positions.get(row["ex_date"])
        if ex_index is None:
            continue
        entry = int(row["entry_days_before"])
        entry_index = ex_index - entry if entry >= 1 else ex_index
        exit_index = ex_index + int(row["exit_days_after"])
        if entry_index < 0 or exit_index >= len(frame):
            continue

        entry_close = float(frame["close"].iloc[entry_index])
        quantity = int(notional // entry_close)
        if quantity < 1:
            ledger.loc[index, "status"] = STATUS_SKIPPED
            continue
        dividend = float(frame["dividend"].iloc[ex_index]) if entry >= 1 else 0.0
        entry_date = frame["date"].iloc[entry_index]
        exit_date = frame["date"].iloc[exit_index]
        result = frictions.trade(
            cfg, quantity=quantity, buy_price=entry_close,
            sell_price=float(frame["close"].iloc[exit_index]),
            buy_date=entry_date.date(), sell_date=exit_date.date(),
            dividend_per_share=dividend,
            record_date=row["ex_date"].date() if entry >= 1 else None)
        deployed = result["buy_exec"] * quantity
        ledger.loc[index, ["status", "entry_date", "entry_close", "exit_date",
                           "exit_close", "dividend", "realized_net",
                           "realized_return"]] = [
            STATUS_CLOSED, entry_date, entry_close, exit_date,
            float(frame["close"].iloc[exit_index]), dividend, result["net"],
            round(result["net"] / deployed, 6)]
        filled += 1

    write_signals(ledger)
    remaining = int((ledger["status"] == STATUS_PENDING).sum())
    print(f"[paper] {filled} signal(s) settled, {remaining} still pending")
    return 0


# --- report -------------------------------------------------------------------


def gate(ledger, runs, today=None):
    """(cleared: bool, reasons: [str]). The header rule, mechanically."""
    today = (pd.Timestamp(today) if today is not None else _now()).date()
    reasons = []
    if runs.empty:
        return False, ["the clock has not started — no runs logged yet"]
    first = runs["ran_at"].min().date()
    deadline = frictions.add_months(first, TRACKING_MONTHS)
    if today < deadline:
        reasons.append(f"only tracking since {first} — three months complete "
                       f"on {deadline}")
    closed = ledger[ledger["status"] == STATUS_CLOSED]
    if len(closed) < MIN_CLOSED_TRADES:
        reasons.append(f"{len(closed)} closed paper trade(s) of "
                       f"{MIN_CLOSED_TRADES} required")
    if len(closed):
        realized = float(closed["realized_return"].median())
        low = float(closed["expected_p25"].median())
        high = float(closed["expected_p75"].median())
        if not low <= realized <= high:
            reasons.append(
                f"realized median {realized:+.2%} sits outside the expected "
                f"band {low:+.2%} to {high:+.2%} frozen at log time")
    return (not reasons), reasons


def monthly(ledger):
    """One row per calendar month of closed trades."""
    closed = ledger[ledger["status"] == STATUS_CLOSED]
    if closed.empty:
        return pd.DataFrame(columns=["month", "closed", "median_return",
                                     "total_net"])
    grouped = closed.groupby(closed["exit_date"].dt.strftime("%Y-%m"))
    return pd.DataFrame([{
        "month": month,
        "closed": len(group),
        "median_return": float(group["realized_return"].median()),
        "total_net": float(group["realized_net"].sum()),
    } for month, group in grouped]).sort_values("month").reset_index(drop=True)


def report(today=None):
    ledger = read_signals()
    runs = read_runs()
    cleared, reasons = gate(ledger, runs, today)

    lines = [
        "[paper] RULE — no real money before 3 months of paper tracking within "
        "expected dispersion:",
        f"[paper]   three calendar months since the first logged run, at least "
        f"{MIN_CLOSED_TRADES} closed paper trades, and the realized median "
        f"inside the out-of-sample IQR frozen at log time. All three, "
        f"simultaneously.",
        f"[paper] GATE: {'CLEARED' if cleared else 'NOT CLEARED'}"
        + ("" if cleared else " — " + "; ".join(reasons)),
    ]
    if not runs.empty:
        lines.append(f"[paper] runs logged: {len(runs)}, first "
                     f"{runs['ran_at'].min().date()}, latest "
                     f"{runs['ran_at'].max().date()}; signals ever: "
                     f"{len(ledger)} ({int((ledger['status'] == STATUS_CLOSED).sum())} "
                     f"closed, {int((ledger['status'] == STATUS_PENDING).sum())} pending)")
    table = monthly(ledger)
    if len(table):
        lines.append("[paper] month      closed   median      total net")
        for row in table.itertuples():
            lines.append(f"[paper] {row.month}    {row.closed:>5}   "
                         f"{row.median_return:+.2%}   {row.total_net:>12,.0f}")
        closed = ledger[ledger["status"] == STATUS_CLOSED]
        lines.append(
            f"[paper] realized median {closed['realized_return'].median():+.2%} "
            f"vs expected {closed['expected_median_return'].median():+.2%} "
            f"(band {closed['expected_p25'].median():+.2%} to "
            f"{closed['expected_p75'].median():+.2%}, frozen at log time)")
    else:
        lines.append("[paper] no closed paper trades yet — the comparison "
                     "starts when the first signal settles")
    for line in lines:
        print(line)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Log signals, capture fills from the cache, and report "
                    "paper results against backtest expectations.")
    parser.add_argument("command", choices=["log", "fill", "report"])
    args = parser.parse_args(argv)
    return {"log": log, "fill": fill, "report": report}[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
