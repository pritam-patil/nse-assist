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

import bisect
import math
import time
from datetime import date, datetime, timedelta

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


# --- point-in-time metrics ----------------------------------------------------
#
# Everything below takes an as-of date and reads only NAVs dated at or before it,
# the same discipline features.py applies to prices and for the same reason: a
# digest that quietly used next week's NAV to rank last week's funds would look
# fine and be wrong.
#
# NOTHING IS FORWARD-FILLED.
#
# Schemes do not share a calendar. Measured on the current watchlist, the liquid
# fund posts 325 NAVs a year and the arbitrage and duration funds post ~240 —
# liquid paper accrues on weekends, equity-linked paper does not. Forward-filling
# the business-day schemes to a daily grid would manufacture a run of zero-change
# days, which drags volatility down by roughly the square root of the padding
# ratio and invents flat stretches that never happened. Instead every calculation
# runs on the observations that exist, and annualisation uses each scheme's own
# observed frequency rather than an assumed 252 or 365.

PERIOD_DAYS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365}
CONSISTENCY_WINDOW = "3m"

# A single-observation rise past this is not a return, it is a restatement: the
# scheme changed its unit face value and every NAV before the change is quoted on a
# different basis. Both HDFC schemes on the current watchlist do exactly this on
# 2015-08-30, x100.04 — a 10-rupee face value becoming 1000.
#
# Only POSITIVE jumps count. A debt fund can genuinely fall 20% or more in a day
# when a holding is written down or a portfolio is segregated; that is a real loss
# and belongs in the numbers. Nothing a parking fund holds can genuinely gain 50%
# overnight, so the asymmetry is the signal.
#
# Windows spanning a restatement return None rather than a number. The alternative
# is dividing by the factor, which means guessing it — the observed 100.04 is 100
# plus a day of accrual, and a wrong guess silently rewrites years of history.
RESTATEMENT_THRESHOLD = 0.5


def _anchor_tolerance(days):
    """How stale a lookback anchor may be before the window stops meaning what it says.

    A "1-month return" measured from an observation 45 days back is a 45-day return
    wearing the wrong label. Rather than silently widening the window, a period with
    no observation close enough to its start returns None.
    """
    return max(5, round(days * 0.1))


def load_navs(conn, scheme_code, as_of=None):
    """(date, nav) pairs for one scheme, oldest first, up to and including as_of."""
    if as_of is None:
        rows = conn.execute(
            "SELECT date, nav FROM fund_navs WHERE scheme_code = ? ORDER BY date",
            (str(scheme_code),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT date, nav FROM fund_navs WHERE scheme_code = ? AND date <= ? ORDER BY date",
            (str(scheme_code), _iso(as_of)),
        ).fetchall()
    return [(r["date"], r["nav"]) for r in rows if r["nav"]]


def _iso(day):
    return day if isinstance(day, str) else day.isoformat()


def nav_at_or_before(navs, target):
    """The most recent observation at or before `target`, or None.

    Resolving backwards is what makes gaps harmless: a Sunday query on a
    business-day scheme answers with Friday rather than inventing a Sunday.

    `target=None` means the latest observation, matching what as_of=None means
    everywhere else in the codebase. Without that the convention held in
    compute_metrics() and broke in every function that called through to here.
    """
    if not navs:
        return None
    if target is None:
        return navs[-1]
    target = _iso(target)
    dates = [d for d, _ in navs]
    index = bisect.bisect_right(dates, target) - 1
    return navs[index] if index >= 0 else None


def period_return(navs, as_of, days, restatements=None):
    """Simple return over the trailing `days`.

    None when no anchor is close enough, or when the window spans a restatement —
    the two NAVs would be quoted on different bases and their ratio would be a
    face-value change wearing the costume of a return.
    """
    end = nav_at_or_before(navs, as_of)
    if not end or not end[1]:
        return None
    target = date.fromisoformat(end[0]) - timedelta(days=days)
    start = nav_at_or_before(navs, target)
    if not start or not start[1]:
        return None
    drift = (target - date.fromisoformat(start[0])).days
    if drift > _anchor_tolerance(days):
        return None
    if restatements is None:
        restatements = find_restatements(navs)
    if _spans_restatement(restatements, start[0], end[0]):
        return None
    return end[1] / start[1] - 1.0


def find_restatements(navs, threshold=RESTATEMENT_THRESHOLD):
    """Dates where the NAV series changes basis rather than value."""
    return [
        navs[i][0]
        for i in range(1, len(navs))
        if navs[i - 1][1] and navs[i][1] / navs[i - 1][1] - 1.0 >= threshold
    ]


def _spans_restatement(restatements, start, end):
    return any(start < day <= end for day in restatements)


def observations_per_year(navs, as_of=None):
    """How many NAVs this scheme actually published in the trailing year.

    Measured, not assumed. The median gap between observations is 1.0 day for every
    scheme on the watchlist — including the business-day ones, because Monday to
    Friday dominates the median and the weekend gap hides in the tail. Only the
    count separates a 325-a-year liquid fund from a 240-a-year arbitrage fund, and
    getting it wrong misstates annualised volatility by about 16%.
    """
    if not navs:
        return 0
    end = _iso(as_of) if as_of else navs[-1][0]
    cutoff = (date.fromisoformat(end) - timedelta(days=365)).isoformat()
    return sum(1 for d, _ in navs if cutoff < d <= end)


def observation_changes(navs, since=None):
    """Return between each pair of CONSECUTIVE OBSERVATIONS — never across a fill.

    A Friday-to-Monday pair is one change, not three. Treating it as three would
    require inventing two of them.
    """
    window = [(d, v) for d, v in navs if since is None or d >= since]
    return [
        window[i][1] / window[i - 1][1] - 1.0
        for i in range(1, len(window))
        if window[i - 1][1]
    ]


def volatility(navs, as_of, days, obs_per_year=None):
    """Annualised standard deviation of observation-to-observation returns."""
    end = nav_at_or_before(navs, as_of)
    if not end:
        return None
    since = (date.fromisoformat(end[0]) - timedelta(days=days)).isoformat()
    visible = [n for n in navs if n[0] <= end[0]]
    if _spans_restatement(find_restatements(visible), since, end[0]):
        return None
    changes = observation_changes(visible, since=since)
    if len(changes) < 3:
        return None
    mean = sum(changes) / len(changes)
    variance = sum((c - mean) ** 2 for c in changes) / (len(changes) - 1)
    periods = obs_per_year or observations_per_year(navs, end[0])
    if not periods:
        return None
    return math.sqrt(variance) * math.sqrt(periods)


def max_drawdown(navs, as_of, days=365):
    """Deepest peak-to-trough fall in the window, as a negative fraction."""
    end = nav_at_or_before(navs, as_of)
    if not end:
        return None
    since = (date.fromisoformat(end[0]) - timedelta(days=days)).isoformat()
    if _spans_restatement(find_restatements(navs), since, end[0]):
        return None
    window = [v for d, v in navs if since <= d <= end[0] and v]
    if len(window) < 2:
        return None
    peak, worst = window[0], 0.0
    for value in window:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def rolling_period_returns(navs, as_of, days, lookback=365):
    """Every trailing-`days` return ending at an observation in the last `lookback`."""
    end = nav_at_or_before(navs, as_of)
    if not end:
        return []
    since = (date.fromisoformat(end[0]) - timedelta(days=lookback)).isoformat()
    visible = [n for n in navs if n[0] <= end[0]]
    restatements = find_restatements(visible)
    out = []
    for day, _ in visible:
        if day < since:
            continue
        value = period_return(visible, day, days, restatements)
        if value is not None:
            out.append(value)
    return out


def worst_month(navs, as_of):
    """The worst trailing-month return of the past year: how bad a bad month gets."""
    returns = rolling_period_returns(navs, as_of, PERIOD_DAYS["1m"])
    return min(returns) if returns else None


def consistency(navs, as_of, window=CONSISTENCY_WINDOW):
    """Share of rolling windows in the past year that ended positive.

    A parking fund should be boring: a high average with a third of its quarters
    negative is a different instrument from a lower average that never dips, and the
    mean alone cannot tell them apart.
    """
    returns = rolling_period_returns(navs, as_of, PERIOD_DAYS[window])
    if not returns:
        return None
    return sum(1 for r in returns if r > 0) / len(returns)


def compute_metrics(navs, as_of=None):
    """Every metric for one scheme as of a date, or None if there is nothing to see."""
    end = nav_at_or_before(navs, as_of) if as_of else (navs[-1] if navs else None)
    if not end:
        return None
    as_of = end[0]
    visible = [n for n in navs if n[0] <= as_of]
    per_year = observations_per_year(visible, as_of)

    restatements = find_restatements(visible)

    # CAGR spans the whole stored history, so it is the metric most likely to cross
    # a restatement — and the one where crossing it is least obvious. HDFC Liquid
    # reads 49.9% a year across its 2015 face-value change, which is roughly seven
    # times the truth and looks merely surprising rather than broken.
    first_day, first_nav = visible[0]
    years = (date.fromisoformat(as_of) - date.fromisoformat(first_day)).days / 365.25
    annualised = None
    if years >= 1 and first_nav and not _spans_restatement(restatements, first_day, as_of):
        annualised = (end[1] / first_nav) ** (1 / years) - 1.0

    return {
        "date": as_of,
        "return_1m": period_return(visible, as_of, PERIOD_DAYS["1m"], restatements),
        "return_3m": period_return(visible, as_of, PERIOD_DAYS["3m"], restatements),
        "return_6m": period_return(visible, as_of, PERIOD_DAYS["6m"], restatements),
        "return_1y": period_return(visible, as_of, PERIOD_DAYS["1y"], restatements),
        # CAGR over the whole stored span, not an annualised short window —
        # annualising a one-month number multiplies its noise by twelve.
        "return_annualized": annualised,
        "vol_3m": volatility(visible, as_of, PERIOD_DAYS["3m"], per_year),
        "vol_1y": volatility(visible, as_of, PERIOD_DAYS["1y"], per_year),
        "max_drawdown_1y": max_drawdown(visible, as_of),
        "worst_month_1y": worst_month(visible, as_of),
        "consistency_3m": consistency(visible, as_of),
        "observations": len(visible),
        "obs_per_year": per_year,
        "restatements": restatements,
    }


def metrics_for(conn, scheme_code, as_of=None):
    return compute_metrics(load_navs(conn, scheme_code, as_of=as_of), as_of)


def store_metrics(conn, scheme_code, metrics):
    """Cache one scheme-date row. Inputs for a past date never change, so a stored
    row stays valid — which is the point of storing it."""
    if not metrics:
        return 0
    before = conn.total_changes
    conn.execute(
        """INSERT OR REPLACE INTO fund_metrics
           (scheme_code, date, return_1m, return_3m, return_6m, return_1y,
            return_annualized, vol_3m, vol_1y, max_drawdown_1y, worst_month_1y,
            consistency_3m, observations, obs_per_year, computed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (str(scheme_code), metrics["date"], metrics["return_1m"], metrics["return_3m"],
         metrics["return_6m"], metrics["return_1y"], metrics["return_annualized"],
         metrics["vol_3m"], metrics["vol_1y"], metrics["max_drawdown_1y"],
         metrics["worst_month_1y"], metrics["consistency_3m"], metrics["observations"],
         metrics["obs_per_year"], datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return conn.total_changes - before


def cached_metrics(conn, scheme_code, as_of):
    row = conn.execute(
        "SELECT * FROM fund_metrics WHERE scheme_code = ? AND date = ?",
        (str(scheme_code), _iso(as_of)),
    ).fetchone()
    return dict(row) if row else None


def refresh_metrics(conn, scheme_codes, as_of=None, recompute=False):
    """Compute and cache metrics for each scheme as of its latest visible NAV."""
    written = []
    for code in scheme_codes:
        navs = load_navs(conn, code, as_of=as_of)
        if not navs:
            continue
        latest = nav_at_or_before(navs, as_of)[0] if as_of else navs[-1][0]
        if not recompute and cached_metrics(conn, code, latest):
            continue
        metrics = compute_metrics(navs, latest)
        if metrics:
            store_metrics(conn, code, metrics)
            written.append((code, metrics))
    return written


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

        refreshed = refresh_metrics(conn, codes)
        if refreshed:
            print(f"[funds] metrics computed for {len(refreshed)} scheme(s):")
            header = (f"{'scheme':<44} {'1m':>7} {'3m':>7} {'1y':>7} {'CAGR':>7} "
                      f"{'vol1y':>7} {'maxDD':>7} {'worst m':>8} {'consist':>8}")
            print(f"[funds]   {header}")
            for code, m in refreshed:
                def pct(value, width=7):
                    return f"{value:>{width}.2%}" if value is not None else f"{'-':>{width}}"
                print(f"[funds]   {fund_watchlist.label_for(code):<44} "
                      f"{pct(m['return_1m'])} {pct(m['return_3m'])} {pct(m['return_1y'])} "
                      f"{pct(m['return_annualized'])} {pct(m['vol_1y'])} "
                      f"{pct(m['max_drawdown_1y'])} {pct(m['worst_month_1y'], 8)} "
                      f"{pct(m['consistency_3m'], 8)}")

        span = stored_span(conn, codes)
        thin = [c for c in codes if span.get(c, ("", "", 0))[2] < 2]
        if thin and not history:
            print(f"[funds] {len(thin)} scheme(s) have almost no history — "
                  f"run `--stage funds --history` to backfill from mfapi.in")
        return written
    finally:
        conn.close()
