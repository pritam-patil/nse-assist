"""Stage — spot-check harness for the prices table.

    python main.py --stage verify-data

Read-only. Prints a per-symbol summary (bar count, earliest, latest) and flags the
defects that are cheap to detect and expensive to miss:

  gaps        more than GAP_THRESHOLD_SESSIONS consecutive sessions with no bar,
              which means a symbol quietly stopped updating
  bad OHLC    close outside [low, high] — the classic signature of a mangled row,
              since a real bar cannot close outside its own range
  mixed basis a symbol holding both adjusted and raw bars (see below)
  jumps       overnight moves large enough to look like an unadjusted split

Sessions are derived from the data rather than from a holiday calendar: a date is
a session when a large share of the universe has a bar on it. That works across
every year in the table, whereas src/holidays_2026.py covers one year — and it
also means a market-wide outage registers as one missing session for everyone
rather than as 100 separate per-symbol gaps.

The mixed-basis check is the important one. ingest.py stores raw traded prices
from NSE's bhavcopy; backfill.py stores split-adjusted prices from yfinance. Each
is correct alone. In one symbol's series they are not comparable, and the seam
between them is invisible in the numbers until a corporate action puts a cliff
there. This is the only place that failure is detectable.
"""

from collections import defaultdict

from src import universe
from src.db import get_connection, init_db
from src.ingest import SOURCE_BASIS

# A date counts as a session when at least this fraction of symbols have a bar.
SESSION_QUORUM = 0.5

GAP_THRESHOLD_SESSIONS = 5

# Overnight move beyond this is either real news or an unadjusted corporate action.
# 25% is above almost any single-session move in a NIFTY 100 name and below the
# smallest common split ratio (1:2 shows up as -50%).
JUMP_THRESHOLD = 0.25

RAW_SOURCES = frozenset(s for s, b in SOURCE_BASIS.items() if b == "raw")
ADJUSTED_SOURCES = frozenset(s for s, b in SOURCE_BASIS.items() if b == "adjusted")


def load(conn, symbols):
    rows = conn.execute(
        "SELECT symbol, date, open, high, low, close, volume, source FROM prices "
        f"WHERE symbol IN ({','.join('?' * len(symbols))}) ORDER BY symbol, date",
        list(symbols),
    ).fetchall()
    series = defaultdict(list)
    for row in rows:
        series[row["symbol"]].append(dict(row))
    return series


def session_dates(series, symbol_count):
    """Dates the market was open, inferred from how many symbols have a bar."""
    counts = defaultdict(int)
    for bars in series.values():
        for bar in bars:
            counts[bar["date"]] += 1
    quorum = max(1, int(symbol_count * SESSION_QUORUM))
    return sorted(d for d, n in counts.items() if n >= quorum)


def find_gaps(bars, sessions_index):
    """Runs of missing sessions longer than the threshold, inside the symbol's own
    span — a symbol that listed late has no gap before it existed."""
    if len(bars) < 2:
        return []
    have = {b["date"] for b in bars}
    first, last = bars[0]["date"], bars[-1]["date"]
    window = [d for d in sessions_index if first <= d <= last]

    gaps, run = [], []
    for day in window:
        if day in have:
            if len(run) > GAP_THRESHOLD_SESSIONS:
                gaps.append((run[0], run[-1], len(run)))
            run = []
        else:
            run.append(day)
    if len(run) > GAP_THRESHOLD_SESSIONS:
        gaps.append((run[0], run[-1], len(run)))
    return gaps


def find_bad_ohlc(bars):
    """Rows that violate the arithmetic a bar cannot violate."""
    bad = []
    for bar in bars:
        values = (bar["open"], bar["high"], bar["low"], bar["close"])
        if any(v is None for v in values):
            bad.append((bar["date"], "null price"))
        elif bar["close"] > bar["high"] or bar["close"] < bar["low"]:
            bad.append((bar["date"], f"close {bar['close']} outside [{bar['low']}, {bar['high']}]"))
        elif bar["low"] > bar["high"]:
            bad.append((bar["date"], f"low {bar['low']} > high {bar['high']}"))
        elif bar["open"] > bar["high"] or bar["open"] < bar["low"]:
            bad.append((bar["date"], f"open {bar['open']} outside [{bar['low']}, {bar['high']}]"))
        elif min(values) <= 0:
            bad.append((bar["date"], "non-positive price"))
    return bad


def find_jumps(bars):
    """Close-to-close moves large enough to suggest an unapplied corporate action."""
    jumps = []
    for previous, current in zip(bars, bars[1:]):
        if not previous["close"]:
            continue
        change = (current["close"] - previous["close"]) / previous["close"]
        if abs(change) >= JUMP_THRESHOLD:
            jumps.append((current["date"], change, previous["source"], current["source"]))
    return jumps


def basis_of(bars):
    """Which adjustment bases a symbol's series contains."""
    sources = {b["source"] for b in bars}
    return {
        "raw": sorted(sources & RAW_SOURCES),
        "adjusted": sorted(sources & ADJUSTED_SOURCES),
        "other": sorted(sources - RAW_SOURCES - ADJUSTED_SOURCES),
        "all": sorted(sources),
    }


def run(dry_run=False, symbols=None, verbose=False, **kwargs):
    symbols = tuple(symbols or universe.UNIVERSE)
    conn = get_connection()
    try:
        init_db(conn)
        series = load(conn, symbols)
        if not series:
            raise RuntimeError("prices table is empty — run --stage backfill or --stage ingest")

        sessions = session_dates(series, len(symbols))
        print(f"\n[verify-data] {len(series)}/{len(symbols)} symbol(s) with bars, "
              f"{len(sessions):,} inferred sessions {sessions[0]} to {sessions[-1]}\n")

        header = f"{'symbol':<12} {'bars':>6} {'earliest':>12} {'latest':>12}  {'sources':<24} flags"
        print(header)
        print("-" * len(header))

        all_gaps, all_bad, all_jumps, mixed, stale = [], [], [], [], []
        latest_session = sessions[-1]

        for symbol in symbols:
            bars = series.get(symbol)
            if not bars:
                print(f"{symbol:<12} {'-':>6} {'-':>12} {'-':>12}  {'-':<24} NO DATA")
                continue

            gaps = find_gaps(bars, sessions)
            bad = find_bad_ohlc(bars)
            jumps = find_jumps(bars)
            basis = basis_of(bars)

            flags = []
            if gaps:
                flags.append(f"{len(gaps)} gap(s)")
                all_gaps.append((symbol, gaps))
            if bad:
                flags.append(f"{len(bad)} bad row(s)")
                all_bad.append((symbol, bad))
            if jumps:
                flags.append(f"{len(jumps)} jump(s)")
                all_jumps.append((symbol, jumps))
            if basis["raw"] and basis["adjusted"]:
                flags.append("MIXED BASIS")
                mixed.append(symbol)
            if bars[-1]["date"] < latest_session:
                flags.append(f"stale (last {bars[-1]['date']})")
                stale.append(symbol)

            if flags or verbose:
                print(
                    f"{symbol:<12} {len(bars):>6,} {bars[0]['date']:>12} {bars[-1]['date']:>12}  "
                    f"{','.join(basis['all']):<24} {'; '.join(flags) or 'ok'}"
                )

        print("-" * len(header))
        clean = len(symbols) - len({s for s, _ in all_gaps} | {s for s, _ in all_bad}
                                   | {s for s, _ in all_jumps} | set(mixed) | set(stale))
        print(f"{clean}/{len(symbols)} symbol(s) clean\n")

        if all_bad:
            print(f"BAD ROWS ({len(all_bad)} symbol(s)) — close outside [low, high] and similar:")
            for symbol, bad in all_bad[:10]:
                for day, why in bad[:3]:
                    print(f"  {symbol:<12} {day}  {why}")
            print()

        if all_gaps:
            print(f"GAPS over {GAP_THRESHOLD_SESSIONS} sessions ({len(all_gaps)} symbol(s)):")
            for symbol, gaps in all_gaps[:10]:
                for first, last, length in gaps[:3]:
                    print(f"  {symbol:<12} {first} to {last}  ({length} sessions)")
            print()

        if all_jumps:
            print(f"JUMPS over {JUMP_THRESHOLD:.0%} ({len(all_jumps)} symbol(s)) — real news, or an "
                  f"unapplied split:")
            for symbol, jumps in all_jumps[:10]:
                for day, change, before, after in jumps[:3]:
                    seam = " [across a source change]" if before != after else ""
                    print(f"  {symbol:<12} {day}  {change:+.1%}  {before} -> {after}{seam}")
            print()

        if mixed:
            print(f"MIXED ADJUSTMENT BASIS ({len(mixed)} symbol(s)):")
            print("  These hold raw bhavcopy bars AND split-adjusted yfinance bars in one series.")
            print("  Each source is correct alone; together they are not a comparable price series,")
            print("  and any indicator spanning the seam reads across two different bases.")
            print(f"  affected: {', '.join(mixed[:12])}{' …' if len(mixed) > 12 else ''}\n")

        return {
            "symbols": len(series),
            "sessions": len(sessions),
            "gaps": len(all_gaps),
            "bad_rows": len(all_bad),
            "jumps": len(all_jumps),
            "mixed_basis": len(mixed),
            "stale": len(stale),
        }
    finally:
        conn.close()
