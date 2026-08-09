"""Forthcoming dividends for the universe — probed from NSE, from this machine.

    python -m data.upcoming

Sources, in order:

  1. The corporate-actions API: structured rows with exDate, recDate and the
     amount in the subject line ("Dividend - Rs 14 Per Share"). When this
     works it is strictly better than anything parsed from prose.
  2. The announcements API, as FALLBACK only: dividend declarations whose
     ex-dates are typically not yet fixed. Rows from here carry the amount
     when the subject text yields one and TBA where it does not.

Both endpoints are probed on every run and their status printed, because NSE
access is location-sensitive: the same requests that work from a residential
connection return empty from cloud egress (the Workers spike), so this module
documents what THIS machine sees, not what NSE promises.

ACCESS QUIRKS, MEASURED 2026-08-09 FROM HERE

  * The NSE homepage now returns 403 even with full browser headers; priming
    on the corporate-filings SECTION PAGE is what grants the nsit cookie the
    api host requires. Same cookie dance as the bhavcopy in src/ingest.py,
    different door.
  * The api host answers brotli-compressed when Accept-Encoding offers br,
    and requests cannot decode brotli without an extra package — the response
    then LOOKS like a 200 with binary garbage. API calls therefore send
    Accept-Encoding: gzip, deflate explicitly.
  * An unprimed session gets 200 with an EMPTY body, not an error status.
    The probe treats an empty body as a failure and retries once.

The output is a table — symbol, ex-date, record date, amount, estimated yield
(amount over the latest cached close), liquidity flag (terciles of 60-session
average turnover from the cache) — filtered to the cached universe, printed,
and written to data/upcoming.parquet. Forward-looking calendar data, not a
signal: nothing here says whether acting on any row is a good idea, and the
verdict in RESULTS.md says it is not.
"""

import argparse
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from data import events, fetch
from src import config
from src.ingest import BROWSER_HEADERS

PRIME_URL = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions"
ACTIONS_REFERER = PRIME_URL
ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
ANNOUNCEMENTS_REFERER = (
    "https://www.nseindia.com/companies-listing/corporate-filings-announcements")

LOOKAHEAD_DAYS = 90
DECLARATION_LOOKBACK_DAYS = 21
DATE_PARAM_FORMAT = "%d-%m-%Y"

OUT_PATH = Path(__file__).resolve().parent / "upcoming.parquet"

# "Dividend - Rs 14 Per Share", "Rs.5.50", "Re 0.50", "₹12".
AMOUNT_PATTERN = re.compile(r"(?:R[se]\.?|₹)\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)

LIQUIDITY_SESSIONS = 60


def parse_amount(text):
    match = AMOUNT_PATTERN.search(str(text or ""))
    return float(match.group(1)) if match else None


def parse_nse_date(text):
    """'10-Aug-2026' -> Timestamp; anything else -> None, never a guess."""
    try:
        return pd.Timestamp(datetime.strptime(str(text).strip(), "%d-%b-%Y"))
    except (ValueError, AttributeError):
        return None


# --- retrieval ----------------------------------------------------------------


def nse_session():
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    session.get(PRIME_URL, timeout=config.REQUEST_TIMEOUT_SECONDS)
    return session


def probe(session, name, url, params, referer):
    """{name, status, rows, error}. Empty-body 200s count as failures and get
    one retry — that is NSE's way of saying the cookies did not take."""
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip, deflate",
               "Referer": referer}
    outcome = {"name": name, "status": None, "rows": None, "error": None}
    for attempt in (1, 2):
        try:
            response = session.get(url, params=params, headers=headers,
                                   timeout=config.REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            outcome["error"] = str(exc)
            continue
        outcome["status"] = response.status_code
        if not response.ok or not response.text.strip():
            outcome["error"] = (f"HTTP {response.status_code}"
                                if not response.ok else "empty body")
            continue
        try:
            payload = response.json()
        except ValueError as exc:
            outcome["error"] = f"not JSON ({exc})"
            continue
        outcome["rows"] = payload if isinstance(payload, list) else payload.get("data", [])
        outcome["error"] = None
        break
    return outcome


def fetch_corporate_actions(session, today=None):
    today = today or date.today()
    params = {
        "index": "equities",
        "from_date": today.strftime(DATE_PARAM_FORMAT),
        "to_date": (today + timedelta(days=LOOKAHEAD_DAYS)).strftime(DATE_PARAM_FORMAT),
    }
    return probe(session, "corporate-actions", ACTIONS_URL, params, ACTIONS_REFERER)


def fetch_announcements(session, today=None):
    today = today or date.today()
    params = {
        "index": "equities",
        "from_date": (today - timedelta(days=DECLARATION_LOOKBACK_DAYS)).strftime(DATE_PARAM_FORMAT),
        "to_date": today.strftime(DATE_PARAM_FORMAT),
    }
    return probe(session, "announcements", ANNOUNCEMENTS_URL, params,
                 ANNOUNCEMENTS_REFERER)


# --- normalization ------------------------------------------------------------


def dividend_actions(rows):
    """Corporate-action rows -> the table's shape. Only dividend subjects; the
    amount comes from the subject line and the dates are NSE's own fields."""
    out = []
    for row in rows or []:
        subject = str(row.get("subject", ""))
        if "dividend" not in subject.lower():
            continue
        out.append({
            "symbol": str(row.get("symbol", "")).strip(),
            "ex_date": parse_nse_date(row.get("exDate")),
            "record_date": parse_nse_date(row.get("recDate")),
            "amount": parse_amount(subject),
            # Corporate-actions rows carry no seq_id; the seen-ledger falls back
            # to symbol|ex-date for these. Only the announcements feed has one.
            "seq_id": None,
            "source": "corporate-actions",
        })
    return out


def dividend_declarations(rows):
    """Announcement rows -> declared-but-not-scheduled dividends, one per
    symbol (the latest). Ex-dates are usually TBA at this stage; a row without
    one is still a forthcoming dividend worth knowing about."""
    latest = {}
    for row in rows or []:
        text = f"{row.get('desc', '')} {row.get('attchmntText', '')}"
        if "dividend" not in text.lower():
            continue
        symbol = str(row.get("symbol", "")).strip()
        stamp = str(row.get("an_dt", ""))
        if symbol and stamp >= latest.get(symbol, {}).get("_stamp", ""):
            latest[symbol] = {
                "symbol": symbol, "ex_date": None, "record_date": None,
                "amount": parse_amount(text),
                # NSE's own unique id for the filing — the natural key for the
                # seen-ledger, so a second daily run surfaces only new filings.
                "seq_id": row.get("seq_id"), "source": "announcements",
                "_stamp": stamp,
            }
    return [{k: v for k, v in row.items() if k != "_stamp"}
            for row in latest.values()]


# --- context from the cache ---------------------------------------------------


def cache_context(symbols):
    """{symbol: (latest close, 60-session avg turnover)} for yield estimates
    and liquidity flags. Reads only symbols the table needs."""
    context = {}
    for symbol in symbols:
        frame = fetch.read_cache(symbol)
        if frame is None or frame.empty:
            continue
        tail = frame.sort_values("date").tail(LIQUIDITY_SESSIONS)
        context[symbol] = (float(tail["close"].iloc[-1]),
                          float((tail["close"] * tail["volume"]).mean()))
    return context


def liquidity_cutoffs(context):
    turnovers = pd.Series([turnover for _, turnover in context.values()])
    if turnovers.empty:
        return None
    return float(turnovers.quantile(1 / 3)), float(turnovers.quantile(2 / 3))


def build_table(rows, universe, context, cutoffs):
    """The output table, universe-filtered, soonest ex-date first, TBA last."""
    members = set(universe)
    kept = []
    for row in rows:
        if row["symbol"] not in members:
            continue
        close, turnover = context.get(row["symbol"], (None, None))
        est_yield = (row["amount"] / close * 100
                     if row["amount"] and close else None)
        if cutoffs is None or turnover is None:
            liquidity = "unknown"
        elif turnover < cutoffs[0]:
            liquidity = "low"
        else:
            liquidity = "mid" if turnover < cutoffs[1] else "high"
        kept.append({**row, "est_yield_pct": est_yield, "liquidity": liquidity})
    frame = pd.DataFrame(kept, columns=["symbol", "ex_date", "record_date",
                                        "amount", "est_yield_pct", "liquidity",
                                        "seq_id", "source"])
    return frame.sort_values(["ex_date", "symbol"],
                             na_position="last").reset_index(drop=True)


# --- CLI ----------------------------------------------------------------------


def _print_table(table):
    print(f"[upcoming] {'symbol':<12} {'ex-date':<12} {'record':<12} "
          f"{'amount':>8} {'est yield':>10} {'liquidity':>9}  source")
    for row in table.itertuples():
        ex_date = row.ex_date.date().isoformat() if pd.notna(row.ex_date) else "TBA"
        record = (row.record_date.date().isoformat()
                  if pd.notna(row.record_date) else "TBA")
        # pd.notna, not truthiness: a missing amount is NaN by the time it has
        # been through the DataFrame, and NaN is truthy.
        amount = f"{row.amount:.2f}" if pd.notna(row.amount) else "–"
        est = f"{row.est_yield_pct:.2f}%" if pd.notna(row.est_yield_pct) else "–"
        print(f"[upcoming] {row.symbol:<12} {ex_date:<12} {record:<12} "
              f"{amount:>8} {est:>10} {row.liquidity:>9}  {row.source}")


def run():
    session = nse_session()
    actions = fetch_corporate_actions(session)
    announcements = fetch_announcements(session)
    for outcome in (actions, announcements):
        state = (f"{len(outcome['rows'])} row(s)" if outcome["rows"] is not None
                 else f"FAILED ({outcome['error']})")
        print(f"[upcoming] probe {outcome['name']}: "
              f"HTTP {outcome['status']} — {state}")

    if actions["rows"] is not None:
        rows = dividend_actions(actions["rows"])
    elif announcements["rows"] is not None:
        print("[upcoming] corporate-actions unavailable — falling back to "
              "declarations parsed from announcements (ex-dates mostly TBA)")
        rows = dividend_declarations(announcements["rows"])
    else:
        print("[upcoming] both endpoints failed from this machine — nothing to show")
        return 1

    # backtested_universe(), not cached_symbols() directly: a bare runner has
    # no cache (data/cache/ is gitignored), and cached_symbols() there is
    # always [] — which used to make every row filter out, table.empty stay
    # True, and this function return 0 WITHOUT ever writing OUT_PATH. notify.py
    # then found no calendar snapshot and failed. backtested_universe() falls
    # back to the committed constituent list precisely for this case.
    universe = events.backtested_universe()
    context = cache_context({row["symbol"] for row in rows})
    table = build_table(rows, universe, context, liquidity_cutoffs(context))
    if table.empty:
        print(f"[upcoming] {len(rows)} dividend row(s) fetched, none in the "
              f"backtested universe — nothing forthcoming for our symbols")
        return 0

    _print_table(table)
    table.to_parquet(OUT_PATH, index=False)
    print(f"[upcoming] {len(table)} row(s) written to {OUT_PATH}")
    print("[upcoming] calendar data, not a signal — RESULTS.md's verdict stands")
    return 0


def check_access(session=None):
    """Probe the announcements endpoint and return its outcome dict.

    This is the per-endpoint gate the notify workflow runs FIRST. A GitHub
    runner is an untested network — the Workers spike showed cloud egress gets
    empty bodies where a residential connection gets data — so access is
    asserted, never assumed. announcements is the probe target because it is
    the same host and cookie dance as corporate-actions; if it answers, the
    calendar fetch will too.
    """
    return fetch_announcements(session or nse_session())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--assert-access", action="store_true",
        help="probe the announcements endpoint and exit 0 (reachable) or 1 "
             "(not) — the runner network gate; fetches nothing else")
    args = parser.parse_args(argv)
    if args.assert_access:
        outcome = check_access()
        if outcome["rows"] is None:
            print(f"[upcoming] NSE announcements UNREACHABLE from here: "
                  f"HTTP {outcome['status']} — {outcome['error']}")
            return 1
        print(f"[upcoming] NSE announcements reachable: "
              f"{len(outcome['rows'])} row(s)")
        return 0
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
