# Manual / local fallbacks for the research side. The GitHub workflows are the
# primary path; these targets are what you run by hand when a runner is blocked
# (see docs/notifications.md) or when you just want to preview locally.
#
# PYTHON lets you point at the project venv without activating it:
#   make notify PYTHON=venv/bin/python
PYTHON ?= python

.PHONY: help notify notify-dry notify-check refresh-snapshot refresh-nifty-snapshot test

help:  ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n", $$1, $$2}'

notify-check:  ## Assert this machine can reach NSE (the per-endpoint gate)
	$(PYTHON) -m data.upcoming --assert-access

# Same order as the workflow: assert access, refresh the calendar, alerts BEFORE
# digest (so an urgent filing is surfaced once, as an alert not a digest line).
notify: notify-check  ## Refresh the calendar and send the digest + alerts
	$(PYTHON) -m data.upcoming
	$(PYTHON) -m data.notify alerts
	$(PYTHON) -m data.notify digest

notify-dry:  ## Same as `notify` but print the messages instead of sending
	$(PYTHON) -m data.upcoming
	$(PYTHON) -m data.notify alerts --dry-run
	$(PYTHON) -m data.notify digest --dry-run

refresh-snapshot:  ## Rebuild data/liquidity_snapshot.csv from the local price cache, then commit it
	$(PYTHON) -m data.liquidity_snapshot

refresh-nifty-snapshot:  ## Rebuild data/nifty_snapshot.csv — commit ALONGSIDE data/grid/ if the backtest was rerun
	$(PYTHON) -m data.nifty_snapshot

test:  ## Run the full test suite
	$(PYTHON) -m unittest discover -s tests
