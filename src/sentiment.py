"""Stage — news sentiment for assembled candidates. OBSERVES, NEVER ACTS.

    python main.py --stage sentiment      # runs after signals in the evening chain

WHAT THIS DOES NOT DO, STATED FIRST BECAUSE IT IS THE WHOLE DESIGN

It does not filter a candidate. It does not resize one. It does not veto, reorder,
rank, or break a tie. Nothing in signals.py, journal.py or backtest.py imports this
module, and the import direction is the enforcement: sentiment reads the assembled
portfolio, and the portfolio cannot read sentiment back. A test asserts that.

The reason is not caution for its own sake. An unvalidated signal wired into
execution is indistinguishable from a validated one at the moment it costs you
money, and by then the paper record has been contaminated — every trade it touched
is now a trade of a different strategy, and the evaluation gate is measuring
something that no longer exists. Observation is free. Action is not, and the price
is paid in the only asset this project has, which is an uncontaminated record.

WHY IT RUNS AFTER ASSEMBLY AND ONLY ON THE SURVIVORS

Scoring every rule firing would mean an LLM call per firing per day for headlines
about positions that were never going to be taken. The assembled portfolio is
typically a handful of names. It is also the right population: the question this
layer will eventually be asked is "would sentiment have improved the trades you
actually took", and only the survivors become trades.

FAILURE IS A NO-OP, DELIBERATELY AND AT EVERY LEVEL

No API key, RSS down, LLM refusing, malformed JSON, a score outside the range —
each returns nothing and the run continues. A brief without sentiment is complete.
A brief that failed to send because a free news feed was slow is broken. The
asymmetry is total, so the error handling is too.

GRADUATION IS PRE-COMMITTED

60 annotated closed trades AND a visible outcome difference between the
negative-sentiment cohort and the rest, before an acting role is even designed.
If it graduates it enters as a veto-only filter, evaluated in its own right — never
as a signal generator. See the README; the thresholds are frozen there alongside
the paper-trading gate and pinned by tests/test_sentiment.py.
"""

import json
from datetime import datetime, timezone

from src import config, news
from src.db import get_connection, init_db
from src.runlog import today

SCORE_MIN = -1.0
SCORE_MAX = 1.0

# Truncated hard. A rationale is a label on a number, not an essay, and an
# unbounded field from a model is an unbounded field in a Telegram message.
MAX_RATIONALE_CHARS = 180

SYSTEM_PROMPT = (
    "You score news sentiment for Indian equities. You are given recent headlines "
    "about one NSE-listed company. Judge only what the headlines say about that "
    "company's near-term prospects as the market would read them.\n\n"
    "Score from -1.0 (clearly negative) through 0.0 (neutral, mixed, or nothing "
    "substantive) to +1.0 (clearly positive).\n\n"
    "Score 0.0 when the headlines are routine coverage, price commentary, or "
    "unrelated to the company's prospects. Most days are 0.0. Do not manufacture a "
    "view from thin material — a confident score on weak evidence is worse than a "
    "neutral one, because it is indistinguishable from a confident score on strong "
    "evidence.\n\n"
    'Return JSON: {"score": <float>, "rationale": "<one short sentence>"}'
)


def build_prompt(symbol, headlines):
    listing = "\n".join(f"- {h['title']}" for h in headlines)
    return (
        f"Company: {symbol} (NSE)\n"
        f"Recent headlines ({len(headlines)}):\n{listing}\n\n"
        "Score the sentiment for this company."
    )


def _clamp(value):
    """A score outside the range means the model ignored the scale. Rejected rather
    than clipped: clipping turns a misunderstanding into a confident extreme."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not SCORE_MIN <= score <= SCORE_MAX:
        return None
    return round(score, 3)


def score_headlines(symbol, headlines):
    """(score, rationale, provider) or (None, None, None). Never raises."""
    if not headlines:
        return None, None, None
    if not (config.GEMINI_API_KEY or config.GROQ_API_KEY):
        return None, None, None

    from src import llm

    try:
        result = llm.generate(build_prompt(symbol, headlines),
                              system=SYSTEM_PROMPT, json_mode=True)
    except Exception as exc:
        print(f"[sentiment] {symbol}: scoring failed ({exc.__class__.__name__}) — skipped")
        return None, None, None

    if not isinstance(result, dict):
        print(f"[sentiment] {symbol}: model returned {type(result).__name__}, not an object — skipped")
        return None, None, None

    score = _clamp(result.get("score"))
    if score is None:
        print(f"[sentiment] {symbol}: unusable score {result.get('score')!r} — skipped")
        return None, None, None

    rationale = str(result.get("rationale") or "").strip()[:MAX_RATIONALE_CHARS]
    return score, rationale, "llm"


def assembled_signals(conn, date=None):
    """Today's assembled candidates — the rows signals.py actually wrote.

    Reads the signals table rather than re-running assembly, so this scores exactly
    what was proposed and cannot disagree with it.
    """
    date = date or today()
    rows = conn.execute(
        "SELECT id, symbol, rule FROM signals WHERE date = ? ORDER BY symbol", (date,)
    ).fetchall()
    return [dict(r) for r in rows]


def already_scored(conn, signal_id):
    row = conn.execute(
        "SELECT 1 FROM news_sentiment WHERE signal_id = ?", (signal_id,)
    ).fetchone()
    return row is not None


def store(conn, signal_id, symbol, date, score, rationale, headlines, provider=None):
    """One row. INSERT OR IGNORE against the UNIQUE index, so a replayed evening
    keeps the score fetched at the time rather than overwriting a point-in-time
    record with a later view of the news."""
    conn.execute(
        """INSERT OR IGNORE INTO news_sentiment
           (signal_id, symbol, date, score, rationale, headlines_json,
            headline_count, provider, model, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (signal_id, symbol, date, score, rationale,
         json.dumps(headlines, ensure_ascii=False), len(headlines), provider,
         config.GEMINI_MODEL,
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


def for_signals(conn, signal_ids):
    """{signal_id: row} for the brief. Empty dict when the table is empty."""
    if not signal_ids:
        return {}
    slots = ",".join("?" * len(signal_ids))
    rows = conn.execute(
        f"SELECT * FROM news_sentiment WHERE signal_id IN ({slots})", list(signal_ids)
    ).fetchall()
    return {r["signal_id"]: dict(r) for r in rows}


def run(dry_run=False, date=None, **kwargs):
    date = date or today()
    conn = get_connection()
    try:
        init_db(conn)
        candidates = assembled_signals(conn, date)
        if not candidates:
            print(f"[sentiment] no assembled candidates on {date} — nothing to score")
            return 0

        if not (config.GEMINI_API_KEY or config.GROQ_API_KEY):
            print("[sentiment] no GEMINI_API_KEY or GROQ_API_KEY — skipping "
                  "(the layer is optional and nothing downstream depends on it)")
            return 0

        pending = [c for c in candidates if not already_scored(conn, c["id"])]
        if not pending:
            print(f"[sentiment] all {len(candidates)} candidate(s) already scored")
            return 0

        # One fetch of the market-wide feeds for the whole run, not one per symbol.
        market = news.market_headlines()
        scored = 0

        for candidate in pending:
            symbol = candidate["symbol"]
            headlines = news.headlines_for(symbol, market_items=market)
            if not headlines:
                print(f"[sentiment] {symbol}: no headlines found — skipped")
                continue

            score, rationale, provider = score_headlines(symbol, headlines)
            if score is None:
                continue

            if dry_run:
                print(f"[sentiment] {symbol}: {score:+.2f} — {rationale} (dry run)")
            else:
                store(conn, candidate["id"], symbol, date, score, rationale,
                      headlines, provider)
                print(f"[sentiment] {symbol}: {score:+.2f} ({len(headlines)} headlines) — {rationale}")
            scored += 1

        print(f"[sentiment] {scored} of {len(pending)} candidate(s) annotated. "
              "Observational only — nothing downstream reads this.")
        return scored
    finally:
        conn.close()
