"""Stage 2 — indicators computed from the `prices` table.

Deliberately not persisted. Indicators are a pure function of the bars, they are
cheap for 100 symbols, and a stored copy is one more thing that can silently go
stale after a price correction. signals.py and backtest.py both call compute()
and read the same numbers.

Pure stdlib — no pandas/numpy. The series here are a few hundred floats long, so
the dependency would cost more than it saves.
"""

from src.db import get_connection, init_db

# The longest lookback any indicator below needs, plus a margin. A symbol with
# fewer bars than this is skipped rather than given a half-warmed average.
MIN_BARS = 210

SMA_FAST = 20
SMA_SLOW = 50
SMA_TREND = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
VOLUME_AVG_PERIOD = 20


def load_bars(conn, symbol, limit=None):
    """Bars for one symbol, oldest first — the order every function here assumes."""
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM prices WHERE symbol = ? ORDER BY date",
        (symbol,),
    ).fetchall()
    bars = [dict(row) for row in rows]
    return bars[-limit:] if limit else bars


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(closes, period=RSI_PERIOD):
    """Wilder's RSI. Returns None until there are period+1 closes to difference."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    # Seed with a simple average of the first `period` moves, then smooth the rest
    # — this is what makes it Wilder's rather than a plain rolling RSI.
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period

    # A stretch with no down-closes at all: RS is undefined, RSI is 100 by convention.
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def atr(bars, period=ATR_PERIOD):
    """Average true range. True range includes the gap from the prior close, which
    is why this is not just high-low — on these names the gap is often the day."""
    if len(bars) < period + 1:
        return None

    true_ranges = []
    for index in range(1, len(bars)):
        high = bars[index]["high"]
        low = bars[index]["low"]
        prev_close = bars[index - 1]["close"]
        if None in (high, low, prev_close):
            continue
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    if len(true_ranges) < period:
        return None

    value = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


def compute(bars):
    """Every indicator for one symbol's bar history, or None if there is not enough
    history to warm the longest lookback."""
    if len(bars) < MIN_BARS:
        return None

    closes = [bar["close"] for bar in bars if bar["close"] is not None]
    volumes = [bar["volume"] or 0 for bar in bars]
    last = bars[-1]

    sma_fast = sma(closes, SMA_FAST)
    sma_slow = sma(closes, SMA_SLOW)
    # The same pair one bar back, so signals.py can tell a fresh crossover from a
    # trend that has been in place for weeks.
    prev_fast = sma(closes[:-1], SMA_FAST)
    prev_slow = sma(closes[:-1], SMA_SLOW)
    avg_volume = sma([float(v) for v in volumes], VOLUME_AVG_PERIOD)

    return {
        "date": last["date"],
        "close": last["close"],
        "high": last["high"],
        "low": last["low"],
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "sma_trend": sma(closes, SMA_TREND),
        "prev_sma_fast": prev_fast,
        "prev_sma_slow": prev_slow,
        "rsi": rsi(closes),
        "atr": atr(bars),
        "volume": last["volume"],
        "avg_volume": avg_volume,
        # Relative volume: 1.0 is an average day. Used as a conviction filter —
        # a breakout on half the usual volume is mostly noise.
        "rel_volume": (last["volume"] / avg_volume) if avg_volume else None,
        "bars": len(bars),
    }


def compute_for(conn, symbol):
    return compute(load_bars(conn, symbol))


def run(dry_run=False, symbols=None, **kwargs):
    """Recomputes indicators for the universe and reports coverage. Nothing is
    written — this stage exists so a bad ingest surfaces before signals run."""
    from src import universe

    symbols = symbols or universe.UNIVERSE
    conn = get_connection()
    try:
        init_db(conn)
        ready, thin = [], []
        for symbol in symbols:
            bars = load_bars(conn, symbol)
            (ready if len(bars) >= MIN_BARS else thin).append((symbol, len(bars)))

        print(f"[features] {len(ready)}/{len(symbols)} symbol(s) have >= {MIN_BARS} bars")
        if thin:
            preview = ", ".join(f"{s}({n})" for s, n in thin[:8])
            print(f"[features] {len(thin)} short of history: {preview}")
        if not ready:
            raise RuntimeError("no symbol has enough history — run --stage ingest first")
        return len(ready)
    finally:
        conn.close()
