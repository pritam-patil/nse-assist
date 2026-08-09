"""Telegram notifications for the dividend research side — digest and alerts.

    python -m data.notify digest [--dry-run]    # once daily, market-wide
    python -m data.notify alerts [--dry-run]    # urgent eligible declarations

Sends through a dedicated personal bot (NOTIFY_TELEGRAM_BOT_TOKEN /
NOTIFY_TELEGRAM_CHAT_ID in .env, never committed), falling back to the
pipeline bot's credentials when blank — safe, because this module only SENDS
and never touches getUpdates, so the two bots cannot conflict.

ELIGIBILITY IS THE SURVIVING PROFILE FROM RESULTS.MD, CODIFIED

A declaration is model-eligible only when every leg holds:

  universe    the symbol is in the backtested universe (the cache) — the
              digest LISTENS market-wide, but the model has never seen
              anything outside the 500 and may not pretend otherwise.
  liquidity   above the bottom tercile — the stress battery's liquidity
              exclusion, approximated by the calendar table's tercile flag;
              "unknown" fails CLOSED, because unverifiable is not a pass.
  yield       inside the surviving buckets: >= 1%, where the validation win
              rate ran 59-64% and the burst-1 measurement itself is reliable;
              the sub-1% buckets sat at coin-flip and are out.
  specials    EXCLUDED, per the slice verdict: the specials cohort is n=7
              diagnostic, never validated, and the amount-rule definition
              failed its sanity check — so the cut here is the yield rule
              alone (> 5%), the only leg of that definition that survived
              eyeballing.
  expectation the surviving cell's friction-adjusted OOS median is positive —
              which, today, is where everything stops: NO cell survives the
              burst-7 bar, the model scope is EMPTY, and the digest says so
              in those words. Ineligible-but-notable declarations still get
              their one FYI line, clearly fenced as outside model scope.

ALERTS AND PAPER ARE ONE WRITE

Every signal alert calls paper.record() BEFORE the send: the alert and the
paper ledger row are the same act, so they cannot diverge. If Telegram then
fails after every backoff retry, the ledger keeps the row — a paper record
with no alert is conservative; an alert with no paper record would be the
strategy grading itself from memory.

Run ORDER is alerts-before-digest (the workflow and `make notify` both do it
this way). An urgent eligible filing is surfaced ONCE — as an alert if it is
time-critical, otherwise as a digest line — because alerts run first and mark
the filing seen, so the digest lists only the rest. The second daily run finds
run 1's filings already seen and alerts only genuinely-new late ones.

Caps: one digest per day, at most MAX_ALERTS_PER_DAY signal alerts per day
ranked by expected net return, exponential backoff on Telegram errors. State
(the seq_id seen-ledger, last-digest date, today's alert count) lives in
notify_state.json, COMMITTED to the repo by the workflow — a GitHub runner is
a fresh checkout each run, so without persistence the second run would forget
the first and the daily cap would reset. The paper ledger (also committed) is
the harder guarantee for alert dedup.

NO ORDER PLACEMENT EXISTS ANYWHERE IN THIS CODEBASE. This module composes
text messages to one private chat; the only side effects are one JSON state
file and appends to the paper ledger.
"""

import argparse
import json
import math
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from data import events, paper, signal, upcoming
from src import config

STATE_PATH = Path(__file__).resolve().parent / "notify_state.json"

MAX_ALERTS_PER_DAY = 5
SURVIVING_YIELD_MIN_PCT = 1.0
SPECIAL_YIELD_PCT = events.SPECIAL_YIELD_PCT
NOTABLE_YIELD_PCT = 2.0
FYI_CAP = 5

# Entry must be scheduled by ex-date minus the cell's entry sessions; an
# eligible declaration is URGENT when that day is this close.
URGENT_WINDOW_DAYS = 2

TELEGRAM_TIMEOUT = 15
MAX_SEND_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0


def credentials():
    """(token, chat_id) — the dedicated bot, else the pipeline's, else None."""
    token = os.environ.get("NOTIFY_TELEGRAM_BOT_TOKEN") or config.TELEGRAM_BOT_TOKEN
    chat = os.environ.get("NOTIFY_TELEGRAM_CHAT_ID") or config.TELEGRAM_CHAT_ID
    return (token, chat) if token and chat else (None, None)


def send(text, dry_run=False):
    """One message, exponential backoff on failure. True when delivered."""
    if dry_run:
        print(f"[notify] DRY RUN — would send {len(text)} chars:\n{text}")
        return True
    token, chat = credentials()
    if not token:
        print("[notify] no bot credentials — set NOTIFY_TELEGRAM_BOT_TOKEN / "
              "NOTIFY_TELEGRAM_CHAT_ID in .env (see .env.example)")
        return False
    for attempt in range(MAX_SEND_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text},
                timeout=TELEGRAM_TIMEOUT)
            if response.ok:
                return True
            print(f"[notify] send failed (HTTP {response.status_code}), "
                  f"attempt {attempt + 1}/{MAX_SEND_ATTEMPTS}")
        except requests.RequestException as exc:
            print(f"[notify] send failed ({exc}), attempt "
                  f"{attempt + 1}/{MAX_SEND_ATTEMPTS}")
    return False


# --- state --------------------------------------------------------------------


def read_state():
    if not STATE_PATH.exists():
        return {"last_digest": None, "seen": [], "alerts": {}}
    return json.loads(STATE_PATH.read_text())


def write_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=1))


def _key(row):
    """Stable id for the seen-ledger. NSE's seq_id when the row came from the
    announcements feed (its own unique key for a filing); symbol|ex-date
    otherwise, since corporate-actions rows carry none. Committed across runs so
    the second daily run surfaces only filings the first did not."""
    seq_id = row.get("seq_id")
    if seq_id is not None and seq_id == seq_id and str(seq_id).strip():
        return f"seq:{seq_id}"
    ex_date = row["ex_date"]
    stamp = ex_date.date().isoformat() if pd.notna(ex_date) else "TBA"
    return f"{row['symbol']}|{stamp}"


# --- eligibility --------------------------------------------------------------


def eligibility(row, universe, survivors):
    """(eligible, reasons). Every leg of the surviving profile, checked in the
    order the docstring states it; reasons name the first failures, plural,
    because "why not" is the useful half of a filter."""
    reasons = []
    if row["symbol"] not in universe:
        reasons.append("outside backtested universe")
    if row.get("liquidity") not in ("mid", "high"):
        reasons.append("bottom liquidity tercile or unverifiable")
    est = row.get("est_yield_pct")
    if est is None or est != est:
        reasons.append("yield not estimable")
    else:
        if est < SURVIVING_YIELD_MIN_PCT:
            reasons.append(f"yield below the surviving {SURVIVING_YIELD_MIN_PCT:g}% floor")
        if est > SPECIAL_YIELD_PCT:
            reasons.append("special (yield rule) — excluded per the slice verdict")
    if not survivors:
        reasons.append("model scope empty — no cell survives burst 7")
    elif all(s["median_return"] <= 0 for s in survivors):
        reasons.append("no surviving cell has positive expected net")
    return (not reasons), reasons


def expected_for(survivors):
    """The best surviving cell's expectation, for ranking and display."""
    best = max(survivors, key=lambda s: s["median_return"])
    return best


def urgent(row, entry_sessions, today):
    """True when the entry window is about to close: the last schedulable
    entry day (ex-date minus the cell's sessions, calendar-approximated) is
    within URGENT_WINDOW_DAYS of today."""
    if pd.isna(row["ex_date"]):
        return False
    last_entry = row["ex_date"] - pd.Timedelta(
        days=math.ceil(entry_sessions * signal.CALENDAR_DAYS_PER_SESSION))
    gap = (last_entry - pd.Timestamp(today)).days
    return 0 <= gap <= URGENT_WINDOW_DAYS


# --- digest -------------------------------------------------------------------


def build_digest(rows, universe, survivors, notional, today):
    """The daily text: model-eligible ranked by expected net, then the fenced
    FYI section. Every number carries its provenance."""
    lines = [f"NSE-ASSIST · DIVIDEND DIGEST\n{today} · research side",
             f"{len(rows)} new declaration(s) since the last digest."]

    eligible_rows, fyi = [], []
    for row in rows:
        ok, reasons = eligibility(row, universe, survivors)
        if ok:
            eligible_rows.append(row)
        else:
            est = row.get("est_yield_pct")
            notable = (est == est and est is not None
                       and est >= NOTABLE_YIELD_PCT)
            if notable:
                fyi.append((row, reasons))

    if not survivors:
        lines.append(
            "\nMODEL SCOPE IS EMPTY: no parameter cell survives the burst-7 "
            "bar (RESULTS.md verdict: the edge dies). Nothing below is a "
            "signal.")
    elif eligible_rows:
        best = expected_for(survivors)
        expected_net = best["median_return"] * notional
        eligible_rows.sort(key=lambda r: -(r.get("est_yield_pct") or 0))
        lines.append(f"\nMODEL-ELIGIBLE ({len(eligible_rows)}):")
        for row in eligible_rows:
            lines.append(
                f"  {row['symbol']}  ex {row['ex_date'].date()}  "
                f"amount {row['amount']:.2f}  est yield {row['est_yield_pct']:.2f}%  "
                f"expected net ~{expected_net:+,.0f}")
        lines.append(
            f"  Dispersion: half the validation trades landed between "
            f"{best['p25']:+.2%} and {best['p75']:+.2%}; a quarter did worse "
            f"than the low end.")
    else:
        lines.append("\nNo model-eligible declarations today.")

    if fyi:
        lines.append(f"\nFYI, OUTSIDE MODEL SCOPE ({min(len(fyi), FYI_CAP)} of "
                     f"{len(fyi)}):")
        fyi.sort(key=lambda pair: -(pair[0].get("est_yield_pct") or 0))
        for row, reasons in fyi[:FYI_CAP]:
            ex_date = (row["ex_date"].date().isoformat()
                       if pd.notna(row["ex_date"]) else "TBA")
            lines.append(f"  {row['symbol']}  ex {ex_date}  "
                         f"yield ~{row['est_yield_pct']:.2f}% — {reasons[0]}")

    lines.append("\nResearch side. Paper only; the real-money gate is not "
                 "cleared. Not advice.")
    return "\n".join(lines)


def digest(dry_run=False, today=None):
    today = today or date.today().isoformat()
    state = read_state()
    if state.get("last_digest") == today and not dry_run:
        print(f"[notify] digest already sent {today}")
        return 0
    if not upcoming.OUT_PATH.exists():
        print("[notify] no calendar snapshot — run `python -m data.upcoming` first")
        return 1

    table = pd.read_parquet(upcoming.OUT_PATH)
    rows = [row for _, row in table.iterrows()
            if _key(row) not in set(state.get("seen", []))]
    if not rows:
        print("[notify] nothing new since the last digest — not sending")
        return 0

    universe = _backtested_universe()
    survivors = _survivors()
    notional = _notional()
    text = build_digest(rows, universe, survivors, notional, today)
    if not send(text, dry_run=dry_run):
        return 1
    if not dry_run:
        state["last_digest"] = today
        state["seen"] = sorted(set(state.get("seen", []))
                               | {_key(row) for row in rows})
        write_state(state)
    print(f"[notify] digest {'previewed' if dry_run else 'sent'}: "
          f"{len(rows)} declaration(s)")
    return 0


# --- alerts -------------------------------------------------------------------


def alerts(dry_run=False, today=None):
    """Urgent eligible declarations as signal alerts — paper-logged at send
    time, capped per day, ranked by expected net."""
    today = today or date.today().isoformat()
    survivors = _survivors()
    if not survivors:
        print("[notify] no surviving cells — no alerts can exist "
              "(the verdict stands)")
        return 0
    if not upcoming.OUT_PATH.exists():
        print("[notify] no calendar snapshot — run `python -m data.upcoming` first")
        return 1

    state = read_state()
    sent_today = (state.get("alerts", {}).get("count", 0)
                  if state.get("alerts", {}).get("date") == today else 0)
    budget = MAX_ALERTS_PER_DAY - sent_today
    if budget <= 0:
        print(f"[notify] alert cap ({MAX_ALERTS_PER_DAY}/day) already reached")
        return 0

    table = pd.read_parquet(upcoming.OUT_PATH)
    universe = _backtested_universe()
    notional = _notional()
    best = expected_for(survivors)
    entry, exit_after = best["cell"]

    # The seen-ledger gate: a filing surfaced by an earlier run (this one
    # ran alerts-before-digest, so run 1's urgent items are already seen) is
    # not surfaced again. This is what makes the second daily run alert only
    # UNSEEN late filings. paper.record() below is the harder guarantee — the
    # atomic one-write that ties an alert to its ledger row — and the two agree.
    seen = set(state.get("seen", []))
    candidates = []
    for _, row in table.iterrows():
        if _key(row) in seen:
            continue
        ok, _ = eligibility(row, universe, survivors)
        if ok and urgent(row, entry, today):
            candidates.append(row)
    candidates.sort(key=lambda r: -(r.get("est_yield_pct") or 0))

    delivered = 0
    for row in candidates[:budget]:
        # Paper first, then Telegram: one write, two readers. See docstring.
        new = paper.record((entry, exit_after), row["symbol"], row["ex_date"], best)
        seen.add(_key(row))   # surfaced now — reconciles even if paper already had it
        if not new:
            continue
        expected_net = best["median_return"] * notional
        text = (f"NSE-ASSIST · SIGNAL ALERT (paper)\n"
                f"{row['symbol']}  ex-date {row['ex_date'].date()}\n"
                f"cell e={entry} x={exit_after}  expected NET "
                f"~{expected_net:+,.0f} on {notional:,.0f}\n"
                f"dispersion: {best['p25']:+.2%} to {best['p75']:+.2%} "
                f"(OOS IQR; a quarter did worse)\n"
                f"Logged to paper. Paper only — the real-money gate is not "
                f"cleared. Not advice.")
        send(text, dry_run=dry_run)
        delivered += 1

    if not dry_run:
        if delivered:
            state["alerts"] = {"date": today, "count": sent_today + delivered}
        state["seen"] = sorted(set(state.get("seen", [])) | seen)
        write_state(state)
    print(f"[notify] {delivered} alert(s) {'previewed' if dry_run else 'sent'} "
          f"({len(candidates)} urgent eligible candidate(s))")
    return 0


def _survivors():
    """The surviving cells, or [] when the grid or cache is not loadable here.

    Degrading to empty is the FAIL-SAFE direction and it is deliberate: a
    GitHub runner has neither the gitignored grid nor the price cache, so the
    evidence cannot be recomputed there. With no evidence loadable we must not
    claim any cell survives — no signal is emitted rather than a false one,
    which is also the true current state (nothing survives burst 7). Loud,
    never silent, so a genuinely-lost grid is visible in the logs."""
    try:
        trades = signal.study_grid.with_context(
            signal.study_specials.load_grid_trades())
        closes = signal.study_exdate.nifty_closes(refresh=False)
        return signal.surviving_cells(trades, closes)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"[notify] survivor evidence not loadable here ({exc}) — model "
              f"scope EMPTY (fail-safe: no evidence means no signal, never a "
              f"false one)")
        return []


def _backtested_universe():
    """Thin wrapper — the fallback logic lives once, in events.py, because
    upcoming.py needs the identical cache-or-committed-list behaviour to
    filter the calendar table and must not grow its own copy. See there."""
    return events.backtested_universe()


def _notional():
    from data import backtest
    return backtest.load_backtest_params()["notional"]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Telegram digest and signal alerts for the dividend side.")
    parser.add_argument("command", choices=["digest", "alerts"])
    parser.add_argument("--dry-run", action="store_true",
                        help="print the message instead of sending it")
    args = parser.parse_args(argv)
    handler = {"digest": digest, "alerts": alerts}[args.command]
    return handler(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
