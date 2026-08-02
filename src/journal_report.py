"""Stage shim — `--stage journal-report` runs journal.report().

Its own module because main.py dispatches stages by importing a module and calling
run(); a bare function in journal.py has no place in that table. The reporting
lives with the ledger it reports on.
"""

from src import journal


def run(dry_run=False, **kwargs):
    return journal.report(dry_run=dry_run, **kwargs)
