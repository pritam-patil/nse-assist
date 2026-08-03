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


def check_rules():
    from src import rules_config

    return rules_config.assert_consistent()


def check_pipeline_test():
    """Whether a rule is enabled as a systems test rather than a strategy view.

    Reported, never failed. It is a deliberate state — the point is that it stays
    visible, because an enabled flag looks identical to a strategy decision three
    weeks after anyone remembers setting it.
    """
    from src import rules_config

    active = rules_config.active_pipeline_tests()
    if not active:
        return "none active"
    return (f"{', '.join(active)} enabled as a pipeline test since "
            f"{rules_config.PIPELINE_TEST_SINCE} — a losing paper record is expected")


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


def check_costs():
    """Surfaces the cost rates and sanity-checks them.

    Not a network call — it guards against an edit that silently rewrites every net
    expectancy in the backtest. A negative rate or a break-even beyond a few percent
    is a typo, not a broker's price list.
    """
    from src import costs

    example = costs.round_trip(1000.0, 1000.0, max(1, risk_config.CAPITAL_PER_TRADE // 1000))
    rates = costs.as_dict()
    if example["total"] <= 0:
        raise RuntimeError("a round trip costs nothing — rates look unset")
    if example["breakeven_pct"] > 0.05:
        raise RuntimeError(
            f"break-even is {example['breakeven_pct']:.1%} of notional — check the rates"
        )
    return f"rates {rates['snapshot']} · {costs.describe_example(risk_config.CAPITAL_PER_TRADE)}"


def check_secrets():
    """No credentials in tracked files.

    Shares its scanner with the pre-commit hook. Two implementations would disagree
    at the edges, and a checker you have learned to overrule is worse than none — so
    the hook that blocks a commit and the check that audits the repo answer the same
    question with the same code.
    """
    from src import secrets_guard

    findings = secrets_guard.tracked_findings()
    if findings:
        first = findings[0]
        raise RuntimeError(
            f"{len(findings)} tracked file(s) carry credentials, e.g. "
            f"{first[0]}:{first[1]} ({first[2]})"
        )
    return "no credentials in tracked files"


def check_telegram():
    config.require("TELEGRAM_BOT_TOKEN")
    response = requests.get(
        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe", timeout=CHECK_TIMEOUT
    )
    if response.status_code >= 400:
        raise RuntimeError(f"getMe -> HTTP {response.status_code}")
    return f"bot @{response.json()['result'].get('username')}"


def check_calendar():
    """Consistency, and how long the file has left.

    FAILS on expiry rather than warning: past COVERAGE_END every scheduled run is
    already failing, so a doctor that still reports OK is the last thing left
    saying the system is fine.
    """
    from src import holidays_2026 as calendar

    detail = calendar.assert_consistent()
    warning = calendar.expiry_warning()
    if warning and calendar.days_until_expiry() < 0:
        raise RuntimeError(warning)
    return f"{detail}{f' — {warning}' if warning else ''}"


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
        # Blended *basis*, not blended label. Two sources that share an adjustment
        # basis are comparable and may sit in one date; two bases are not.
        from src.ingest import SOURCE_BASIS

        by_date = {}
        for row in conn.execute("SELECT date, source, COUNT(*) n FROM prices GROUP BY date, source"):
            by_date.setdefault(row["date"], set()).add(SOURCE_BASIS.get(row["source"], "unknown"))
        mixed = sorted((d for d, bases in by_date.items() if len(bases) > 1), reverse=True)
        if mixed:
            raise RuntimeError(
                f"{len(mixed)} date(s) blend adjustment bases: {', '.join(mixed[:5])}"
            )

        phantom = conn.execute(
            "SELECT COUNT(*) FROM prices WHERE volume = 0 AND open = high AND high = low AND low = close"
        ).fetchone()[0]
        if phantom:
            raise RuntimeError(f"{phantom} phantom bar(s) stored — run --stage ingest to purge")

        # A price cannot move because the feed changed. A large jump exactly at a
        # source seam is therefore an artefact by definition — the two sources
        # disagree on the adjustment basis — and unlike a big real move there is no
        # judgement call about it.
        seam_jumps = []
        rows = conn.execute(
            "SELECT symbol, date, close, source FROM prices ORDER BY symbol, date"
        ).fetchall()
        previous = {}
        for row in rows:
            prior = previous.get(row["symbol"])
            from src.ingest import SOURCE_BASIS as _BASIS
            if (prior and prior["close"]
                    and _BASIS.get(prior["source"]) != _BASIS.get(row["source"])):
                change = (row["close"] - prior["close"]) / prior["close"]
                if abs(change) >= 0.25:
                    seam_jumps.append((row["symbol"], row["date"], change))
            previous[row["symbol"]] = row

        counts = dict(conn.execute("SELECT source, COUNT(*) FROM prices GROUP BY source").fetchall())
        total = sum(counts.values()) or 1
        share = ", ".join(f"{k} {v * 100 // total}%" for k, v in sorted(counts.items()))

        if seam_jumps:
            worst = sorted(seam_jumps, key=lambda j: -abs(j[2]))[:3]
            detail = ", ".join(f"{s} {d} {c:+.0%}" for s, d, c in worst)
            raise RuntimeError(
                f"{len(seam_jumps)} price jump(s) at a source seam — adjusted and raw bars "
                f"in one series: {detail}. See --stage verify-data"
            )
        return f"no blended dates, no phantom bars, no seam jumps ({share})"
    finally:
        conn.close()


def check_discontinuities():
    """New within-basis price cliffs — the defect class nothing else catches.

    check_source_integrity() fails on jumps ACROSS an adjustment seam, which are
    artefacts by definition. This one covers the harder case: a cliff *inside* a
    single basis, where the provider's own adjustment is wrong. TRENT and VEDL both
    look like this, and neither trips the seam check.

    Passes on anything already on verify_data's acknowledgement list. The point is
    not to re-litigate known ones every Sunday; it is that a new one is loud.
    """
    from src import verify_data

    unreviewed = verify_data.unreviewed_jumps()
    known = len(verify_data.KNOWN_DISCONTINUITIES)
    if unreviewed:
        detail = ", ".join(f"{s} {d} {c:+.0%}" for s, d, c in unreviewed[:4])
        raise RuntimeError(
            f"{len(unreviewed)} unreviewed discontinuity(ies): {detail}. "
            f"Check against bhavcopy, then record the finding in "
            f"verify_data.KNOWN_DISCONTINUITIES"
        )
    pending = sum(1 for _, _, status, _ in verify_data.KNOWN_DISCONTINUITIES
                  if status == "unreviewed")
    return (f"no new discontinuities; {known} known"
            + (f", {pending} still unverified against bhavcopy" if pending else ""))


# Roughly four times the current 12.4 MB. Not a growth budget — the file grows
# about 5 MB a year and would take decades to reach this. It is a guard on the
# ASSUMPTION: successive versions delta-compress well because SQLite rewrites only
# the pages it touches, which holds while the file is mostly append-only price
# history. Something that breaks that shape — a table storing large text per row,
# a much wider universe, a VACUUM that reorders every page — would show up here
# first, and the committed-database decision would deserve re-examining.
MAX_DB_MB = 50


def check_db_size():
    """The size of the thing every workflow commits.

    Fails loud rather than warning, because by the time this trips the repository
    has already been carrying the growth for a while and every future clone pays
    for it.
    """
    import os

    path = config.DB_PATH
    if not os.path.exists(path):
        return "no database yet"
    megabytes = os.path.getsize(path) / 1e6
    if megabytes > MAX_DB_MB:
        raise RuntimeError(
            f"{path} is {megabytes:,.0f} MB, past the {MAX_DB_MB} MB guard — the "
            f"assumption that daily commits stay cheap depends on this file being "
            f"mostly append-only price history. Check what grew before raising it"
        )
    return f"{megabytes:,.1f} MB of {MAX_DB_MB} MB guard (~5 MB/year growth)"


def check_snapshot_age():
    """Committed constants that go stale silently.

    A wrong holiday fails loudly. A stale universe or an out-of-date fee schedule
    does not fail at all — the scan quietly looks at last season's index, and every
    net expectancy shifts by a percentage nobody chose. Both need a clock.
    """
    from src import costs, universe

    warnings = [w for w in (universe.snapshot_warning(), costs.snapshot_warning()) if w]
    if warnings:
        raise RuntimeError("; ".join(warnings))
    return (f"universe {universe.SNAPSHOT_DATE}, cost rates {costs.RATES_SNAPSHOT} "
            f"— both current")


def check_amfi():
    """The dump parses, and every watchlist code is actually in it.

    A code that has been wound up, or mistyped, otherwise shows up as a scheme that
    silently never updates — which looks identical to a fund that simply prices on
    business days only.
    """
    from src import fund_watchlist
    from src.funds import fetch_nav_text, parse_dump

    text = fetch_nav_text()
    everything = parse_dump(text)
    if not everything:
        raise RuntimeError("dump parsed to zero rows")

    codes = set(fund_watchlist.SCHEME_CODES)
    present = {r["scheme_code"] for r in everything}
    unknown = sorted(codes - present)
    detail = f"{len(everything):,} schemes parsed, {len(codes)} watchlisted"
    if unknown:
        raise RuntimeError(f"{detail}; not in the dump: {', '.join(unknown)}")
    return detail


def check_watchlist():
    from src import fund_watchlist

    return fund_watchlist.assert_consistent()


def check_mfapi():
    """The back-history source. Its own check, because it degrades silently.

    AMFI covers the daily NAV, so mfapi.in being down costs nothing today and shows
    up weeks later as a scheme whose long-window metrics never appear. Probing it
    here is what turns that into a fact you can see now.
    """
    from src import fund_watchlist
    from src.funds import fetch_history

    code = fund_watchlist.SCHEME_CODES[0] if fund_watchlist.SCHEME_CODES else "119091"
    rows = fetch_history(code)
    return f"{code}: {len(rows):,} historical NAV(s), {rows[0][1]} to {rows[-1][1]}"


def check_news_map():
    """The company-name map, which fails silently when a key is wrong.

    A key that is not a universe symbol is inert — the symbol falls through to the
    generic query and nothing reports the mapping was skipped. Not a network call
    and not a judgement about retrieval quality; just the edit that breaks quietly.
    """
    from src import news

    return news.assert_consistent()


def check_sentiment_llm():
    """Reachability of whichever LLM provider is configured, if either is.

    SKIPs rather than fails when no key is set: the sentiment layer is optional by
    design, and a doctor that fails on its absence would make an optional thing
    mandatory by the back door.
    """
    from src import llm

    text = llm.generate("Reply with exactly one word: pong")
    return f"{str(text).strip()[:20]} (sentiment scoring reachable)"


def check_freshness():
    """Per-table latest date against what the calendar says to expect.

    A hard failure, not a note. Every other check here answers "can this reach its
    source"; this one answers "did the data actually arrive", and those come apart
    exactly when it matters — a reachable endpoint and an ingest that has not run
    for three days is the state that produces confident, stale reports.
    """
    from src import health

    conn = get_connection()
    try:
        init_db(conn)
        rows = health.freshness(conn)
        parts = []
        stale = []
        for row in rows:
            latest = row["latest"] or "never"
            parts.append(f"{row['table']} {latest}")
            if row["stale"]:
                stale.append(f"{row['table']} at {latest}, expected {row['expected']}")
        if stale:
            raise RuntimeError("; ".join(stale))
        return " · ".join(parts)
    finally:
        conn.close()


def freshness_table():
    """Rows for the printed freshness block. Never raises — it is a report."""
    from src import health

    conn = get_connection()
    try:
        init_db(conn)
        return health.freshness(conn)
    finally:
        conn.close()


def run(dry_run=False, **kwargs):
    checks = [
        _check("env", check_env),
        _check("database", check_db),
        _check("db-size", check_db_size),
        _check("universe", check_universe),
        _check("risk", check_risk),
        _check("costs", check_costs),
        _check("rules", check_rules),
        _check("pipeline-test", check_pipeline_test),
        _check("secrets", check_secrets),
        _check("sizing", check_sizing_coverage),
        _check("calendar", check_calendar),
        _check("bhavcopy", check_bhavcopy),
        _check("fallback", check_fallback),
        _check("integrity", check_source_integrity),
        _check("discontinuity", check_discontinuities),
        _check("snapshots", check_snapshot_age),
        _check("watchlist", check_watchlist),
        _check("amfi", check_amfi),
        _check("mfapi", check_mfapi),
        _check("news-map", check_news_map),
        (_check("sentiment", check_sentiment_llm)
         if (config.GEMINI_API_KEY or config.GROQ_API_KEY)
         else _skip("sentiment", "no GEMINI_API_KEY or GROQ_API_KEY — layer is optional")),
        _check("telegram", check_telegram) if config.TELEGRAM_BOT_TOKEN else _skip("telegram", "no token"),
        _check("freshness", check_freshness),
    ]

    print(f"\n{'check':<14} {'status':<6} detail")
    print("-" * 76)
    for name, status, note in checks:
        print(f"{name:<14} {status:<6} {note}")
    print("-" * 76)

    # Freshness beside the row counts, because a count on its own answers the wrong
    # question. 100,109 price bars looks identical whether the newest is yesterday's
    # or from three weeks ago, and only one of those is a working pipeline.
    print(f"\n{'table':<14} {'latest':<12} {'expected':<12} {'behind':<8} detail")
    print("-" * 76)
    for row in freshness_table():
        behind = "-" if row["behind"] is None else str(row["behind"])
        mark = "  <- stale" if row["stale"] else ""
        print(f"{row['table']:<14} {(row['latest'] or 'never'):<12} "
              f"{(row['expected'] or '-'):<12} {behind:<8} {row['detail']}{mark}")
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
