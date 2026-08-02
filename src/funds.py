"""Stage 6 — mutual-fund NAVs, for the cash parked between trades.

Two sources, deliberately unequal in the trust placed in them.

DAILY (primary, required): AMFI's own NAVAll.txt. Free, no auth, one request for
every scheme in India, republished each evening. This is the industry's system of
record and the only source the stage needs to keep working.

HISTORY (optional, best-effort): mfapi.in, a free community-run JSON API keyed by
AMFI scheme code. It is the only convenient way to get a scheme's back-history, and
it is somebody's side project — no SLA, no support, and it will be down sometimes.
Every call is wrapped so a failure degrades to "no history yet" rather than failing
the run, and the fallback is simply that daily NAVAll pulls accumulate history
forward from today. Slower, but it depends on nothing but AMFI.

    python main.py --stage funds --search "hdfc liquid"   # find scheme codes
    python main.py --stage funds --history                # one-off back-history

CALENDAR: fund NAVs do not share the equity calendar, and they do not share one
with each other. Liquid and overnight funds price on all days including weekends,
because the underlying instruments accrue daily; arbitrage and most duration funds
price only on business days. Measured on 2026-08-02, a Sunday: the liquid schemes
had that day's NAV, the arbitrage and ultra-short ones still showed Friday. So a
missing day is the normal state of affairs here and is never an error — the stage
reports what it stored and says nothing about what it did not.
"""

import time
from datetime import datetime

import requests

from src import config, fund_watchlist
from src.db import get_connection, init_db

EXPECTED_FIELDS = 6
SCHEME_CODE_INDEX = 0
NAME_INDEX = 3
NAV_INDEX = 4
DATE_INDEX = 5

# AMFI writes dates as 01-Aug-2026; everything else in this DB is ISO.
AMFI_DATE_FORMAT = "%d-%b-%Y"

# Category headers and fund-house headers are both bare lines with no delimiter.
# Categories are the ones naming a scheme type.
CATEGORY_MARKER = "Schemes("
HOUSE_SUFFIX = "mutual fund"

MFAPI_URL = "https://api.mfapi.in/mf/{code}"
MFAPI_DATE_FORMAT = "%d-%m-%Y"
MFAPI_TIMEOUT = 20
# A community service, so: one at a time, with a pause, and few retries.
MFAPI_PAUSE_SECONDS = 0.7
MFAPI_RETRIES = 2


# --- AMFI daily dump ----------------------------------------------------------


def fetch_nav_text():
    response = requests.get(config.AMFI_NAV_URL, timeout=config.REQUEST_TIMEOUT_SECONDS)
    if response.status_code >= 400:
        raise RuntimeError(f"AMFI NAV dump -> HTTP {response.status_code}")
    if len(response.text) < 10_000:
        # A block page or a truncated CDN response — better caught here than as
        # "0 schemes parsed" three functions later.
        raise RuntimeError(f"AMFI dump was only {len(response.text)} chars — truncated?")
    return response.text


def parse_dump(text, scheme_codes=None):
    """Every scheme in the dump as a list of dicts, carrying its category header.

    The file interleaves two kinds of bare line: a category ("Open Ended
    Schemes(Debt Scheme - Liquid Fund)") and a fund house ("HDFC Mutual Fund").
    Tracking the former is what makes --search useful, since the category is the
    thing you actually filter on when choosing where to park cash.

    Unparseable lines are skipped in silence: blank separators and headers are most
    of the file by line count, and a suspended scheme carries "N.A." where its NAV
    should be.
    """
    wanted = {str(c) for c in scheme_codes} if scheme_codes else None
    category = house = None
    out = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ";" not in stripped:
            if CATEGORY_MARKER in stripped:
                category = stripped
            elif stripped.lower().endswith(HOUSE_SUFFIX):
                house = stripped
            continue

        fields = [f.strip() for f in stripped.split(";")]
        if len(fields) != EXPECTED_FIELDS:
            continue
        code = fields[SCHEME_CODE_INDEX]
        if not code.isdigit():
            continue  # the header row
        if wanted and code not in wanted:
            continue

        try:
            nav = float(fields[NAV_INDEX])
            day = datetime.strptime(fields[DATE_INDEX], AMFI_DATE_FORMAT).date().isoformat()
        except ValueError:
            continue
        # A wound-up or segregated scheme sits in the file at 0.0000 forever. Storing
        # it would put a fake collapse in the series.
        if nav <= 0:
            continue

        out.append({
            "scheme_code": code,
            "name": fields[NAME_INDEX],
            "category": category,
            "house": house,
            "nav": nav,
            "date": day,
        })
    return out


def search(term, text=None, limit=40):
    """Scheme codes matching a name substring — the watchlist-building helper.

    Case-insensitive, and every space-separated word must appear somewhere in the
    name, so "hdfc liquid direct" narrows instead of returning everything matching
    any one word.
    """
    words = [w for w in term.lower().split() if w]
    if not words:
        raise RuntimeError("search needs a term, e.g. --search 'hdfc liquid'")

    rows = parse_dump(text or fetch_nav_text())
    hits = [r for r in rows if all(w in r["name"].lower() for w in words)]
    hits.sort(key=lambda r: (not fund_watchlist.is_parking_category(r["category"]), r["name"]))
    return hits[:limit], len(hits)


# --- mfapi.in history (third-party, best-effort) -------------------------------


def fetch_history(scheme_code, session=None):
    """Full NAV history for one scheme from mfapi.in, oldest first.

    Raises on failure. Every caller is expected to catch — this is a free community
    API with no availability guarantee, and losing it must never fail the stage.
    """
    session = session or requests
    last_error = None
    for attempt in range(MFAPI_RETRIES):
        if attempt:
            time.sleep(2**attempt)
        try:
            response = session.get(MFAPI_URL.format(code=scheme_code), timeout=MFAPI_TIMEOUT)
        except requests.RequestException as exc:
            last_error = RuntimeError(f"network error: {exc}")
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            last_error = RuntimeError(f"non-JSON response ({exc})")
            continue

        points = payload.get("data") or []
        if not points:
            raise RuntimeError(f"no history returned (status {payload.get('status')})")

        rows = []
        for point in points:
            try:
                nav = float(point["nav"])
                day = datetime.strptime(point["date"], MFAPI_DATE_FORMAT).date().isoformat()
            except (ValueError, KeyError, TypeError):
                continue
            if nav > 0:
                rows.append((str(scheme_code), day, nav))
        if not rows:
            raise RuntimeError("history contained no usable points")
        return sorted(rows)

    raise last_error or RuntimeError("history fetch failed")


# --- storage ------------------------------------------------------------------


def store_navs(conn, rows):
    """Upserts (scheme_code, date, nav) tuples. Returns rows actually written."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR REPLACE INTO fund_navs (scheme_code, date, nav) VALUES (?, ?, ?)", rows
    )
    conn.commit()
    return conn.total_changes - before


def stored_span(conn, scheme_codes):
    """{code: (earliest, latest, count)} — used to decide what needs back-history."""
    if not scheme_codes:
        return {}
    rows = conn.execute(
        "SELECT scheme_code, MIN(date) lo, MAX(date) hi, COUNT(*) n FROM fund_navs "
        f"WHERE scheme_code IN ({','.join('?' * len(scheme_codes))}) GROUP BY scheme_code",
        [str(c) for c in scheme_codes],
    ).fetchall()
    return {r["scheme_code"]: (r["lo"], r["hi"], r["n"]) for r in rows}


def latest_navs(conn, scheme_codes=None):
    """Newest NAV per scheme, for the delivery message."""
    rows = conn.execute(
        """SELECT scheme_code, date, nav FROM fund_navs
           WHERE (scheme_code, date) IN
                 (SELECT scheme_code, MAX(date) FROM fund_navs GROUP BY scheme_code)"""
    ).fetchall()
    result = [dict(r) for r in rows]
    if scheme_codes:
        wanted = {str(c) for c in scheme_codes}
        result = [r for r in result if r["scheme_code"] in wanted]
    return sorted(result, key=lambda r: fund_watchlist.label_for(r["scheme_code"]))


# --- stage --------------------------------------------------------------------


def backfill_history(conn, scheme_codes, dry_run=False):
    """Best-effort back-history from mfapi.in. Never raises.

    Returns (stored, failures). A total outage is reported and shrugged off: daily
    AMFI pulls accumulate history forward regardless, so the only cost of mfapi
    being down is that the series starts today instead of in 2013.
    """
    stored, failures = 0, []
    session = requests.Session()

    for index, code in enumerate(scheme_codes):
        try:
            rows = fetch_history(code, session=session)
        except Exception as exc:
            # Truncated: urllib3 errors run to several hundred characters and
            # bury the one useful line under a stack of connection detail.
            failures.append(f"{code}: {str(exc).split(chr(40))[0][:70]}")
            continue
        if dry_run:
            print(f"[funds] {fund_watchlist.label_for(code)}: {len(rows)} point(s) (dry run)")
        else:
            written = store_navs(conn, rows)
            stored += written
            print(
                f"[funds] {fund_watchlist.label_for(code)}: {written} new of {len(rows)} "
                f"point(s), {rows[0][1]} to {rows[-1][1]}"
            )
        if index < len(scheme_codes) - 1:
            time.sleep(MFAPI_PAUSE_SECONDS)

    if failures:
        print(f"[funds] mfapi.in unavailable for {len(failures)} scheme(s): {'; '.join(failures[:3])}")
        print("[funds] not fatal — daily AMFI pulls accumulate history forward from here")
    return stored, failures


def run(dry_run=False, scheme_codes=None, search_term=None, history=False, **kwargs):
    codes = tuple(scheme_codes if scheme_codes is not None else fund_watchlist.SCHEME_CODES)

    if search_term:
        hits, total = search(search_term)
        print(f"\n[funds] {total} scheme(s) matching {search_term!r}"
              f"{f', showing {len(hits)}' if total > len(hits) else ''}\n")
        print(f"{'code':<9} {'nav':>12} {'date':>12}  scheme")
        print("-" * 100)
        for row in hits:
            mark = "*" if fund_watchlist.is_parking_category(row["category"]) else " "
            print(f"{row['scheme_code']:<9} {row['nav']:>12,.4f} {row['date']:>12} {mark} {row['name'][:58]}")
            print(f"{'':<9} {'':<12} {'':<12}   {(row['category'] or '?')[:76]}")
        print("\n* = a parking category (see src/fund_watchlist.py)\n")
        return len(hits)

    if not codes:
        print("[funds] watchlist is empty — add schemes to src/fund_watchlist.py")
        print("[funds] find codes with: python main.py --stage funds --search 'hdfc liquid'")
        return 0

    conn = get_connection()
    try:
        init_db(conn)

        if history:
            stored, _ = backfill_history(conn, codes, dry_run=dry_run)
            print(f"[funds] history: {stored} NAV point(s) stored")

        rows = parse_dump(fetch_nav_text(), codes)
        found = {r["scheme_code"] for r in rows}
        missing = [c for c in codes if c not in found]

        if not rows:
            raise RuntimeError(
                f"none of the {len(codes)} watchlist scheme(s) appear in today's dump — "
                "check the codes with --search"
            )

        if dry_run:
            print(f"[funds] {len(rows)} NAV row(s) parsed (dry run, not stored)")
            return len(rows)

        written = store_navs(conn, [(r["scheme_code"], r["date"], r["nav"]) for r in rows])
        print(f"[funds] {written} new NAV row(s) from {len(rows)} watchlist scheme(s)")
        for row in sorted(rows, key=lambda r: fund_watchlist.label_for(r["scheme_code"])):
            print(f"[funds]   {fund_watchlist.label_for(row['scheme_code']):<44} "
                  f"{row['nav']:>12,.4f}  {row['date']}")

        # Not a warning. Arbitrage and duration funds price on business days only, so
        # on a weekend most of the watchlist legitimately has nothing new to say.
        if missing:
            labels = ", ".join(fund_watchlist.label_for(c) for c in missing)
            print(f"[funds]   ({len(missing)} scheme(s) not in today's dump — normal for "
                  f"business-day-only funds: {labels})")

        span = stored_span(conn, codes)
        thin = [c for c in codes if span.get(c, ("", "", 0))[2] < 2]
        if thin and not history:
            print(f"[funds] {len(thin)} scheme(s) have almost no history — "
                  f"run `--stage funds --history` to backfill from mfapi.in")
        return written
    finally:
        conn.close()
