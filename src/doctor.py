"""Health check: environment, data sources, committed constants, table row counts.

    python main.py --stage doctor

Read-only and cheap — nothing is ingested, stored, or sent. Each check is
independent, so one failure never hides the others. Exits non-zero when any hard
check fails, which makes it usable as a CI gate or as the first thing to run in a
new environment.
"""

import requests

from src import config, risk_config, universe
from src.db import TABLES, get_connection, init_db, table_counts

CHECK_TIMEOUT = 15


def _check(name, fn):
    try:
        return (name, "OK", fn() or "ok")
    except Exception as exc:
        return (name, "FAIL", str(exc)[:120])


def _skip(name, why):
    return (name, "SKIP", why)


def check_env():
    """Both Telegram values, since delivery is the only output channel."""
    config.require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    return "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID set"


def check_db():
    conn = get_connection()
    try:
        init_db(conn)
        return f"{len(TABLES)} tables at {config.DB_PATH}"
    finally:
        conn.close()


def check_universe():
    return universe.assert_consistent()


def check_risk():
    values = risk_config.as_dict()
    if values["max_daily_loss"] <= 0 or values["daily_profit_target"] <= 0:
        raise RuntimeError("max_daily_loss and daily_profit_target must both be positive")
    if values["capital_per_trade"] <= 0:
        raise RuntimeError("capital_per_trade must be positive")
    coherence = risk_config.assert_coherent()
    return (
        f"{values['capital_per_trade']:,}/trade · -{values['max_daily_loss']:,} loss cap · "
        f"+{values['daily_profit_target']:,} target · max {values['max_open_positions']} open "
        f"({coherence})"
    )


def check_sizing_coverage():
    """How much of the universe can actually be sized at the current capital.

    Not a pass/fail on its own — a deliberately small capital_per_trade legitimately
    excludes the expensive names. It fails only when most of the universe is gone,
    because at that point the paper record is a sample of cheap stocks rather than of
    the index, and no amount of it will tell you whether the rules work.
    """
    from src import features

    conn = get_connection()
    try:
        init_db(conn)
        quotes = []
        for symbol in universe.UNIVERSE:
            ind = features.compute_for(conn, symbol)
            if ind and ind["atr"]:
                quotes.append((symbol, ind["close"], ind["atr"] * risk_config.ATR_STOP_MULTIPLE))

        if not quotes:
            raise RuntimeError("no priced symbols yet — run --stage ingest first")

        coverage = risk_config.sizing_coverage(quotes)
        untradable, minimal, total = coverage["untradable"], coverage["minimal"], coverage["total"]
        detail = (
            f"{coverage['tradable']}/{total} sized normally, {len(minimal)} at the "
            f"{risk_config.MIN_SHARES}-share floor, {len(untradable)} untradable at "
            f"{risk_config.CAPITAL_PER_TRADE:,}/trade"
        )
        if untradable:
            detail += f" (e.g. {', '.join(untradable[:4])})"
        if len(untradable) > total / 2:
            raise RuntimeError(f"over half the universe cannot be sized — {detail}")
        return detail
    finally:
        conn.close()


def check_telegram():
    config.require("TELEGRAM_BOT_TOKEN")
    response = requests.get(
        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe", timeout=CHECK_TIMEOUT
    )
    if response.status_code >= 400:
        raise RuntimeError(f"getMe -> HTTP {response.status_code}")
    return f"bot @{response.json()['result'].get('username')}"


def check_calendar():
    from src import holidays_2026 as calendar

    return calendar.assert_consistent()


def _last_session():
    from datetime import date, timedelta

    from src import holidays_2026 as calendar

    day = date.today()
    for _ in range(14):
        if calendar.is_trading_day(day):
            return day
        day -= timedelta(days=1)
    raise RuntimeError("no trading day found in the last 14 days")


def check_bhavcopy():
    """The primary source, end to end: cookie prime, download, unzip, parse.

    Deliberately a real fetch rather than a HEAD — NSE's bot detection answers 200
    with an HTML block page, so only parsing proves the pipeline actually works.
    """
    from src.ingest import fetch_bhavcopy

    day = _last_session()
    bars = fetch_bhavcopy(day)
    covered = sum(1 for s in universe.UNIVERSE if s in bars)
    return f"{day}: {len(bars):,} equity bars, {covered}/{len(universe.UNIVERSE)} of the universe"


def check_fallback():
    """yfinance reachability, and that auto_adjust is being suppressed.

    An adjusted series would silently disagree with bhavcopy's raw traded prices, so
    this checks the values rather than just the call.
    """
    from src.ingest import fetch_yfinance

    day = _last_session()
    symbol = universe.UNIVERSE[0]
    series = fetch_yfinance([symbol], day, day)
    bars = series.get(symbol)
    if not bars:
        raise RuntimeError(f"{symbol}: no bars returned for {day}")
    return f"{symbol} {bars[-1]['date']} close {bars[-1]['close']:.2f}"


def check_source_integrity():
    """Two invariants the ingest stage is supposed to maintain.

    A date carrying more than one source means the two feeds have been blended into
    one day's bars — the exact thing the precedence rule exists to prevent. Phantom
    bars are Yahoo's zero-volume holiday placeholders, which flatten RSI and drag the
    20-day average volume down wherever they survive.
    """
    conn = get_connection()
    try:
        init_db(conn)
        mixed = conn.execute(
            "SELECT date, GROUP_CONCAT(DISTINCT source) AS sources FROM prices "
            "GROUP BY date HAVING COUNT(DISTINCT source) > 1 ORDER BY date DESC LIMIT 5"
        ).fetchall()
        if mixed:
            detail = ", ".join(f"{r['date']} ({r['sources']})" for r in mixed)
            raise RuntimeError(f"{len(mixed)}+ date(s) blend sources: {detail}")

        phantom = conn.execute(
            "SELECT COUNT(*) FROM prices WHERE volume = 0 AND open = high AND high = low AND low = close"
        ).fetchone()[0]
        if phantom:
            raise RuntimeError(f"{phantom} phantom bar(s) stored — run --stage ingest to purge")

        counts = dict(conn.execute("SELECT source, COUNT(*) FROM prices GROUP BY source").fetchall())
        total = sum(counts.values()) or 1
        share = ", ".join(f"{k} {v * 100 // total}%" for k, v in sorted(counts.items()))
        return f"no blended dates, no phantom bars ({share})"
    finally:
        conn.close()


def check_amfi():
    from src.funds import fetch_nav_text, parse_navs

    rows = parse_navs(fetch_nav_text(), config.FUND_SCHEME_CODES)
    if not rows:
        raise RuntimeError("dump parsed to zero rows")
    return f"{len(rows)} NAV row(s) parsed"


def run(dry_run=False, **kwargs):
    checks = [
        _check("env", check_env),
        _check("database", check_db),
        _check("universe", check_universe),
        _check("risk", check_risk),
        _check("sizing", check_sizing_coverage),
        _check("calendar", check_calendar),
        _check("bhavcopy", check_bhavcopy),
        _check("fallback", check_fallback),
        _check("integrity", check_source_integrity),
        _check("amfi", check_amfi),
        _check("telegram", check_telegram) if config.TELEGRAM_BOT_TOKEN else _skip("telegram", "no token"),
    ]

    print(f"\n{'check':<14} {'status':<6} detail")
    print("-" * 76)
    for name, status, note in checks:
        print(f"{name:<14} {status:<6} {note}")
    print("-" * 76)

    counts = table_counts()
    print(f"\n{'table':<14} rows")
    print("-" * 26)
    for table, count in counts.items():
        print(f"{table:<14} {count:>10,}")
    print("-" * 26)

    failures = sum(1 for _, status, _ in checks if status == "FAIL")
    print(
        f"\n{failures} failure(s), "
        f"{sum(1 for _, s, _ in checks if s == 'SKIP')} skipped, "
        f"{sum(1 for _, s, _ in checks if s == 'OK')} ok\n"
    )
    if failures:
        raise RuntimeError(f"doctor: {failures} check(s) failed")
    return counts
