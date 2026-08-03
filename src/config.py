"""Loads configuration from environment variables (via .env if present).

Secrets and per-environment paths only. Anything that is a *decision* rather than
a credential — the tradable universe, risk limits — lives in a committed module
(src/universe.py, src/risk_config.py) so it is reviewable in git history.
"""

import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# `or` rather than a get() default on purpose: GitHub Actions always *sets* an env
# var for a referenced secret, using an empty string when that secret is missing.
# os.environ.get(k, default) only falls back when the key is absent, so a blank
# secret would otherwise win and yield a path of "".
DB_PATH = os.environ.get("DB_PATH") or "output/nse.db"

# Daily OHLCV source. Only 'yahoo' is implemented; the indirection exists so a paid
# feed can replace it without changing ingest.py's callers.
PRICE_SOURCE = os.environ.get("PRICE_SOURCE") or "yahoo"

# History pulled the first time a symbol is seen. 400 calendar days is the *minimum*
# that works at all — it leaves ~270 sessions, and features.py burns the first 210
# warming the 200-day average, so a backtest would only have ~60 tradable days.
# 1500 leaves roughly four years of usable range instead. Raising this later does
# not deepen symbols already stored; run `--stage ingest --backfill` for that.
INGEST_LOOKBACK_DAYS = int(os.environ.get("INGEST_LOOKBACK_DAYS") or 1500)

# AMFI's full NAV dump: plain text, pipe-delimited, refreshed once each evening.
AMFI_NAV_URL = os.environ.get("AMFI_NAV_URL") or "https://www.amfiindia.com/spages/NAVAll.txt"

REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS") or 20)

# --- sentiment layer (observational, optional) ---------------------------------
# Both keys are OPTIONAL. Absent, the sentiment stage is a clean no-op: it prints
# one line and returns. Nothing downstream depends on it, by design — see
# src/sentiment.py for why that independence is load-bearing rather than lazy.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"
GROQ_MODEL = os.environ.get("GROQ_MODEL") or "llama-3.3-70b-versatile"


def require(*names):
    """Raise if any of the named config values are unset. Call explicitly, not at import."""
    missing = [name for name in names if not globals().get(name)]
    if missing:
        raise RuntimeError(f"Missing required config values: {', '.join(missing)}")
