"""CLI entry point for nse-assist."""

import argparse
import sys
import traceback

from src import backtest, deliver, doctor, features, funds, ingest, journal, runlog, signals
from src import db

STAGES = {
    "ingest": ingest,
    "features": features,
    "signals": signals,
    "scan": signals,  # alias: "run the scan" is what the signals stage does
    "backtest": backtest,
    "journal": journal,
    "funds": funds,
    "deliver": deliver,
    "doctor": doctor,
}

# Canonical, alias-free daily order for --stage all (STAGES.values() would run
# signals.run() twice, once per alias, if used directly here).
#
# 'backtest' is deliberately excluded: it replays years of history across the
# whole universe, which takes minutes, and it answers a research question rather
# than a daily one. Invoke it explicitly (--stage backtest). 'doctor' is likewise
# a health check, not a pipeline step.
ALL_STAGES = (ingest, features, signals, journal, funds, deliver)


def parse_args():
    parser = argparse.ArgumentParser(description="Run stages of the nse-assist pipeline.")
    parser.add_argument(
        "--stage",
        choices=list(STAGES.keys()) + ["all"],
        default="all",
        help="Which stage to run (default: all). 'scan' is an alias for 'signals'.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without writing to the database or sending to Telegram.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Ingest only: re-request the full lookback for every symbol instead of "
        "just the days since its newest stored bar. Use after raising INGEST_LOOKBACK_DAYS.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    db.init_db()

    stages = ALL_STAGES if args.stage == "all" else [STAGES[args.stage]]
    failures = []
    for stage in stages:
        name = stage.__name__.split(".")[-1]
        runlog.log(name, "start")
        try:
            stage.run(dry_run=args.dry_run, backfill=args.backfill)
            runlog.log(name, "ok")
        except Exception as exc:
            # One stage failing must not block the others: a dead price feed should
            # still let funds and deliver run. Log it, keep going, and fail the
            # process at the end so CI still alerts.
            runlog.log(name, "fail", exc)
            failures.append(name)
            traceback.print_exc()
            print(f"[main] Stage {name} FAILED ({exc}) — continuing with remaining stages")

    if failures:
        print(f"[main] {len(failures)} stage(s) failed: {', '.join(failures)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
