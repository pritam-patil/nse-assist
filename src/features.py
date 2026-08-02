"""Stage 2 — per-symbol indicators, computed strictly as of a given date.

POINT-IN-TIME DISCIPLINE IS THE POINT OF THIS MODULE.

Every entry point takes `as_of` and every one of them filters to `date <= as_of`
before a single number is computed. Nothing here may read a bar dated later than
`as_of`, directly or transitively. That is not a stylistic preference: a backtest
that peeks one day forward will report an edge that does not exist, and it will do
so quietly, with plausible-looking numbers, for as long as you care to run it.
Lookahead bias does not crash — it flatters.

The defence is tests/test_point_in_time.py, which asserts that features computed
as of D are byte-identical whether or not the table contains rows after D. Read
that file before changing anything in here.

`as_of=None` means "everything stored", which is correct for live use where today
is the edge of the data. It is never correct inside a backtest loop, and
backtest.py passes an explicit date.

Indicators are not persisted. They are a pure function of the bars, cheap for 100
symbols, and a stored copy is one more thing that can go stale after a price
correction or a re-adjustment. The per-date cache below is process-local and
exists only to avoid recomputing the same date many times within one run.

The numeric core is plain stdlib; pandas is used only to assemble the tidy frame,
so the arithmetic stays testable without a DataFrame in the way.
"""

import math
from collections import OrderedDict

from src.db import get_connection, init_db

# A 52-week window is 252 trading sessions, not 52*5 calendar days and not
# "however many bars we happen to have". Computing a "52-week high" from a shorter
# window silently answers a different question, so MIN_BARS is sized to make the
# longest lookback real rather than aspirational.
SESSIONS_PER_YEAR = 252
LOOKBACK_52W = SESSIONS_PER_YEAR

SMA_PERIODS = (20, 50, 200)
EMA_PERIODS = (9, 21)
RSI_PERIOD = 14
ATR_PERIOD = 14
VOLUME_AVG_PERIOD = 20
REALIZED_VOL_PERIOD = 20

# Enough history to warm the longest lookback (the 52-week window) with margin.
MIN_BARS = LOOKBACK_52W + 8

# A close-to-close move past this inside the indicator window means the series has
# a discontinuity — an unadjusted split, or a demerger the adjustment does not
# cover. Above almost any real single-session move in a NIFTY 100 name.
DISCONTINUITY_THRESHOLD = 0.25

# Legacy aliases. signals.py reads sma_fast/sma_slow/sma_trend and rel_volume;
# keeping them costs nothing and avoids a rename touching the money path.
SMA_FAST, SMA_SLOW, SMA_TREND = SMA_PERIODS


# --- loading ------------------------------------------------------------------


def load_bars(conn, symbol, as_of=None, limit=None):
    """Bars for one symbol, oldest first, up to and including `as_of`.

    The `date <= ?` clause is the entire point-in-time guarantee for every caller
    that goes through here. `as_of=None` returns everything, which is right for
    live use and wrong inside a backtest.
    """
    if as_of is None:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM prices "
            "WHERE symbol = ? ORDER BY date",
            (symbol,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM prices "
            "WHERE symbol = ? AND date <= ? ORDER BY date",
            (symbol, _iso(as_of)),
        ).fetchall()
    bars = [dict(row) for row in rows]
    return bars[-limit:] if limit else bars


def _iso(day):
    return day if isinstance(day, str) else day.isoformat()


# --- primitives ---------------------------------------------------------------


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values, period):
    """Exponential moving average, seeded with the SMA of the first `period` values.

    Seeding with the simple average rather than the first observation is what keeps
    the early output from being dominated by one arbitrary price; by the time the
    series reaches `as_of` the difference has decayed away, but the seed is what
    makes short histories behave.
    """
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    for price in values[period:]:
        value = (price - value) * multiplier + value
    return value


def rsi(closes, period=RSI_PERIOD):
    """Wilder's RSI. None until there are period+1 closes to difference."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    # Seeded with a simple average of the first `period` moves, then smoothed —
    # this is what makes it Wilder's rather than a plain rolling RSI.
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
        high, low = bars[index]["high"], bars[index]["low"]
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


def daily_returns(closes):
    return [
        closes[i] / closes[i - 1] - 1.0
        for i in range(1, len(closes))
        if closes[i - 1]
    ]


def realized_vol(closes, period=REALIZED_VOL_PERIOD, annualize=True):
    """Standard deviation of daily returns, annualised by default.

    Sample standard deviation (n-1): these are a sample of the return process, not
    the population, and with a 20-day window the difference is not negligible.
    """
    returns = daily_returns(closes)[-period:]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    deviation = math.sqrt(variance)
    return deviation * math.sqrt(SESSIONS_PER_YEAR) if annualize else deviation


def max_jump(bars, window=None):
    """Largest absolute close-to-close move in the window, as (size, date).

    Only the bars the indicators actually read are examined, so a cliff older than
    the longest lookback stops counting once it has scrolled out of every average.
    """
    window = window or MIN_BARS
    recent = bars[-window:] if len(bars) > window else bars
    worst, when = 0.0, None
    for previous, current in zip(recent, recent[1:]):
        if not previous["close"]:
            continue
        change = abs(current["close"] - previous["close"]) / previous["close"]
        if change > worst:
            worst, when = change, current["date"]
    return worst, when


def _distance(price, level):
    """Fractional distance from `level` to `price`. Negative means below."""
    if not level or price is None:
        return None
    return price / level - 1.0


# --- the feature set ----------------------------------------------------------


def compute(bars):
    """Every indicator for one symbol, from a list of bars ending at the as-of date.

    Point-in-time by construction: this function cannot see anything the caller did
    not put in `bars`. The discipline therefore lives entirely in how `bars` is
    selected, which is why load_bars() carries the `date <= as_of` clause and why
    the test suite exercises the pair together rather than this function alone.
    """
    if len(bars) < MIN_BARS:
        return None

    closes = [b["close"] for b in bars if b["close"] is not None]
    highs = [b["high"] for b in bars if b["high"] is not None]
    lows = [b["low"] for b in bars if b["low"] is not None]
    volumes = [float(b["volume"] or 0) for b in bars]
    last, previous = bars[-1], bars[-2]

    smas = {period: sma(closes, period) for period in SMA_PERIODS}
    emas = {period: ema(closes, period) for period in EMA_PERIODS}
    avg_volume = sma(volumes, VOLUME_AVG_PERIOD)

    window_52w = bars[-LOOKBACK_52W:]
    high_52w = max((b["high"] for b in window_52w if b["high"] is not None), default=None)
    low_52w = min((b["low"] for b in window_52w if b["low"] is not None), default=None)

    close = last["close"]
    prior_close = previous["close"]
    jump, jump_date = max_jump(bars)

    out = {
        "date": last["date"],
        "bars": len(bars),
        "open": last["open"],
        "high": last["high"],
        "low": last["low"],
        "close": close,
        "volume": last["volume"],
        "prev_close": prior_close,
        "daily_return": _distance(close, prior_close),
        # Open against the *prior* close, which is the overnight move. Against the
        # same session's close it would just be the intraday reversal.
        "gap_pct": _distance(last["open"], prior_close),
        "rsi_14": rsi(closes),
        "atr_14": atr(bars),
        "atr_pct": (atr(bars) / close) if (close and atr(bars)) else None,
        "volume": last["volume"],
        "avg_volume_20": avg_volume,
        "volume_ratio_20": (last["volume"] / avg_volume) if avg_volume else None,
        "realized_vol_20": realized_vol(closes),
        "high_52w": high_52w,
        "low_52w": low_52w,
        # Negative below the high, positive above the low. Both are 0 at the extreme.
        "dist_52w_high": _distance(close, high_52w),
        "dist_52w_low": _distance(close, low_52w),
        "max_jump": jump,
        "max_jump_date": jump_date,
    }

    for period, value in smas.items():
        out[f"sma_{period}"] = value
        out[f"dist_sma_{period}"] = _distance(close, value)
    for period, value in emas.items():
        out[f"ema_{period}"] = value
        out[f"dist_ema_{period}"] = _distance(close, value)

    # --- legacy aliases, read by signals.py and backtest.py ---
    out["sma_fast"], out["sma_slow"], out["sma_trend"] = (
        smas[SMA_FAST], smas[SMA_SLOW], smas[SMA_TREND],
    )
    # The same pair one bar back, so a fresh crossover is distinguishable from a
    # trend that has been in place for weeks.
    out["prev_sma_fast"] = sma(closes[:-1], SMA_FAST)
    out["prev_sma_slow"] = sma(closes[:-1], SMA_SLOW)
    out["rsi"] = out["rsi_14"]
    out["atr"] = out["atr_14"]
    out["rel_volume"] = out["volume_ratio_20"]
    return out


def compute_for(conn, symbol, as_of=None):
    """Indicators for one symbol as of `as_of`, read from the database."""
    return compute(load_bars(conn, symbol, as_of=as_of))


def bars_as_of(bars, as_of):
    """The in-memory equivalent of load_bars()'s `date <= as_of` clause.

    A backtest walks thousands of as-of dates per symbol; going back to SQLite for
    each one would be tens of thousands of queries to re-read history that has not
    changed. So the same filter exists here, over a list already in memory.

    Two implementations of one rule is exactly how a point-in-time guarantee rots —
    the SQL path gets fixed and the in-memory one does not. tests/ therefore asserts
    the two agree bar-for-bar across a range of dates, so they cannot drift apart
    silently. Filtering on the date rather than a positional index is deliberate:
    an index is only equivalent while the caller's list is complete and sorted, and
    a date means the same thing regardless.
    """
    if as_of is None:
        return list(bars)
    cutoff = _iso(as_of)
    return [bar for bar in bars if bar["date"] <= cutoff]


def compute_as_of(bars, as_of):
    """Indicators from an in-memory bar list, as of `as_of`."""
    return compute(bars_as_of(bars, as_of))


# --- tidy frame, cached per date ----------------------------------------------

# Process-local, bounded, keyed by (as_of, symbols). Purely a within-run optimisation
# — a backtest asks for the same date many times. It is NOT a persistence layer, and
# it is deliberately easy to clear: anything that rewrites price history (ingest,
# backfill) invalidates every entry, so those stages call clear_cache().
_CACHE = OrderedDict()
_CACHE_MAX_ENTRIES = 64

FRAME_COLUMNS = (
    "symbol", "as_of", "date", "bars",
    "close", "prev_close", "daily_return", "gap_pct",
    "sma_20", "sma_50", "sma_200", "dist_sma_20", "dist_sma_50", "dist_sma_200",
    "ema_9", "ema_21", "dist_ema_9", "dist_ema_21",
    "rsi_14", "atr_14", "atr_pct",
    "volume", "avg_volume_20", "volume_ratio_20",
    "realized_vol_20",
    "high_52w", "low_52w", "dist_52w_high", "dist_52w_low",
    "max_jump", "max_jump_date",
)


def clear_cache():
    """Drop every cached frame. Call after anything that rewrites price history."""
    _CACHE.clear()


def cache_info():
    return {"entries": len(_CACHE), "keys": [k[0] for k in _CACHE]}


def feature_frame(conn, as_of=None, symbols=None, use_cache=True):
    """Tidy DataFrame: one row per symbol, one column per feature, all as of `as_of`.

    Symbols without enough history are omitted rather than carried as all-NaN rows —
    a half-warmed 200-day average is not a smaller number, it is a wrong one.

    The returned frame is a copy. Handing out the cached object would let one
    caller's edit silently become another caller's input, which is a nastier bug
    than the recomputation it saves.
    """
    import pandas as pd

    from src import universe

    symbols = tuple(symbols) if symbols else tuple(universe.UNIVERSE)
    key = (_iso(as_of) if as_of is not None else None, symbols)

    if use_cache and key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key].copy()

    rows = []
    for symbol in symbols:
        computed = compute_for(conn, symbol, as_of=as_of)
        if computed is None:
            continue
        row = {"symbol": symbol, "as_of": key[0] or computed["date"]}
        row.update({column: computed.get(column) for column in FRAME_COLUMNS if column not in row})
        rows.append(row)

    frame = pd.DataFrame(rows, columns=list(FRAME_COLUMNS))
    if not rows:
        # An empty frame still carries the schema, so callers can filter and select
        # without special-casing "no symbols had enough history".
        frame = pd.DataFrame({c: pd.Series(dtype="object") for c in FRAME_COLUMNS})

    if use_cache:
        _CACHE[key] = frame
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)
    return frame.copy()


# --- stage --------------------------------------------------------------------


def run(dry_run=False, symbols=None, as_of=None, **kwargs):
    """Reports feature coverage. Nothing is written — this stage exists so a bad
    ingest surfaces before signals run."""
    from src import universe

    symbols = tuple(symbols or universe.UNIVERSE)
    conn = get_connection()
    try:
        init_db(conn)
        clear_cache()
        frame = feature_frame(conn, as_of=as_of, symbols=symbols)

        ready = len(frame)
        print(f"[features] {ready}/{len(symbols)} symbol(s) have >= {MIN_BARS} bars"
              f"{f' as of {_iso(as_of)}' if as_of else ''}")
        if ready < len(symbols):
            short = []
            for symbol in symbols:
                if symbol in set(frame["symbol"]):
                    continue
                short.append((symbol, len(load_bars(conn, symbol, as_of=as_of))))
            preview = ", ".join(f"{s}({n})" for s, n in short[:8])
            print(f"[features] {len(short)} short of history: {preview}")
        if not ready:
            raise RuntimeError("no symbol has enough history — run --stage ingest first")
        return ready
    finally:
        conn.close()
