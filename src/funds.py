"""Stage 6 — mutual-fund NAVs from AMFI into the `fund_navs` table.

AMFI publishes one plain-text dump of every scheme's latest NAV, refreshed each
evening. No key, no per-scheme calls: one request covers everything.

Format is pipe-delimited with scheme-house headers interleaved as bare lines, so
parsing skips anything that does not split into the expected number of fields:

    Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date
    119551;INF209K01YM5;-;Aditya Birla Sun Life Banking & PSU Debt Fund;345.6789;01-Aug-2026

FUND_SCHEME_CODES must list the schemes actually held. Unset, this stage does
nothing: the full dump is ~14k rows *per day* against a database that is committed
back after every run, which is about a megabyte a day of repo growth.
"""

from datetime import datetime

import requests

from src import config
from src.db import get_connection, init_db

EXPECTED_FIELDS = 6
SCHEME_CODE_INDEX = 0
NAV_INDEX = 4
DATE_INDEX = 5

# AMFI writes dates as 01-Aug-2026; everything else in this DB is ISO.
AMFI_DATE_FORMAT = "%d-%b-%Y"


def fetch_nav_text():
    response = requests.get(config.AMFI_NAV_URL, timeout=config.REQUEST_TIMEOUT_SECONDS)
    if response.status_code >= 400:
        raise RuntimeError(f"AMFI NAV dump -> HTTP {response.status_code}")
    return response.text


def parse_navs(text, scheme_codes=None):
    """Rows as (scheme_code, iso_date, nav). Unparseable lines are skipped silently
    — the dump is full of header and blank lines by design, and a scheme suspended
    for the day carries 'N.A.' where the NAV should be."""
    wanted = set(scheme_codes) if scheme_codes else None
    rows = []

    for line in text.splitlines():
        fields = [f.strip() for f in line.split(";")]
        if len(fields) != EXPECTED_FIELDS:
            continue

        code = fields[SCHEME_CODE_INDEX]
        if not code.isdigit():
            continue  # the header row
        if wanted and code not in wanted:
            continue

        try:
            nav = float(fields[NAV_INDEX])
            date = datetime.strptime(fields[DATE_INDEX], AMFI_DATE_FORMAT).date().isoformat()
        except ValueError:
            continue

        rows.append((code, date, nav))

    return rows


def store_navs(conn, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO fund_navs (scheme_code, date, nav) VALUES (?, ?, ?)", rows
    )
    conn.commit()
    return len(rows)


def latest_navs(conn, scheme_codes=None):
    """Newest NAV per scheme, for the delivery message."""
    query = """SELECT scheme_code, date, nav FROM fund_navs
               WHERE (scheme_code, date) IN
                     (SELECT scheme_code, MAX(date) FROM fund_navs GROUP BY scheme_code)"""
    rows = conn.execute(query).fetchall()
    result = [dict(row) for row in rows]
    if scheme_codes:
        wanted = set(scheme_codes)
        result = [row for row in result if row["scheme_code"] in wanted]
    return sorted(result, key=lambda row: row["scheme_code"])


def run(dry_run=False, scheme_codes=None, **kwargs):
    scheme_codes = scheme_codes if scheme_codes is not None else config.FUND_SCHEME_CODES

    # Storing the whole dump is ~14k rows *per day*, which adds roughly a megabyte
    # a day to a database that gets committed back to the repo after every run.
    # That is not a limit you want to discover six months in, so the unconfigured
    # case does nothing rather than quietly accumulating.
    if not scheme_codes:
        print("[funds] FUND_SCHEME_CODES is empty — nothing tracked, skipping")
        return 0

    conn = get_connection()
    try:
        init_db(conn)
        rows = parse_navs(fetch_nav_text(), scheme_codes)

        if not rows:
            raise RuntimeError(
                f"none of the {len(scheme_codes)} configured scheme code(s) appear in the dump"
            )

        if dry_run:
            print(f"[funds] {len(rows)} NAV row(s) parsed (dry run, not stored)")
            return len(rows)

        stored = store_navs(conn, rows)
        print(f"[funds] {stored} NAV row(s) stored across {len(scheme_codes)} tracked scheme(s)")
        return stored
    finally:
        conn.close()
