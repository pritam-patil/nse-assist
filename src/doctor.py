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


def check_price_feed():
    """One real symbol against the live feed — proves reachability and that the
    response still has the shape ingest.py parses."""
    from src.ingest import fetch_bars
    from datetime import date, timedelta

    symbol = universe.UNIVERSE[0]
    bars = fetch_bars(symbol, date.today() - timedelta(days=10))
    if not bars:
        raise RuntimeError(f"{symbol}: no bars in the last 10 days")
    return f"{symbol} latest {bars[-1]['date']} close {bars[-1]['close']:.2f}"


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
        _check("price-feed", check_price_feed),
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
