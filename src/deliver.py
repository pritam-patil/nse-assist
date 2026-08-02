"""Stage 7 — sends the day's signals, ledger state and fund NAVs to Telegram.

Telegram is the only output channel, so there is no downstream fallback: the retry
here *is* the resilience. Network errors and 5xx retry with backoff; 4xx never
does, because a bad token or chat id will not fix itself.
"""

import html
import time

import requests

from src import config, fund_watchlist, funds, risk_config, rules_config, runlog, signals
from src.db import get_connection, init_db
from src.journal import open_positions, realised_pnl, summary
from src.runlog import today

API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/{method}"

# Telegram's hard limit for a text message.
TELEGRAM_MAX_MESSAGE_CHARS = 4096

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3

_SPLIT_SEPARATORS = ("\n\n", "\n", ". ", " ")


def _post(method, data):
    config.require("TELEGRAM_BOT_TOKEN")
    url = API_URL_TEMPLATE.format(token=config.TELEGRAM_BOT_TOKEN, method=method)

    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(2**attempt)
        try:
            response = requests.post(url, data=data, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_error = RuntimeError(f"Telegram {method} network error: {exc}")
            continue
        if response.status_code >= 500:
            last_error = RuntimeError(f"Telegram {method} returned {response.status_code}")
            continue
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {payload.get('description')}")
        return payload["result"]
    raise last_error or RuntimeError(f"Telegram {method} failed")


def split_message(text, limit=TELEGRAM_MAX_MESSAGE_CHARS):
    """Splits on the coarsest separator that works, so a chunk break lands between
    sections rather than mid-number. A hard cut is the last resort only."""
    if len(text) <= limit:
        return [text]

    for separator in _SPLIT_SEPARATORS:
        if separator not in text:
            continue
        chunks, current = [], ""
        for part in text.split(separator):
            candidate = f"{current}{separator}{part}" if current else part
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = part
        if current:
            chunks.append(current)
        if all(len(chunk) <= limit for chunk in chunks):
            return chunks

    return [text[i : i + limit] for i in range(0, len(text), limit)]


def send_message(text, dry_run=False, parse_mode="HTML"):
    """Send, splitting if needed. `parse_mode=None` sends plain text.

    Plain text matters for a message that carries no markup: under HTML parse mode a
    bare ampersand or angle bracket in a symbol name is a 400 from Telegram, so
    opting out beats escaping content that was never markup in the first place.
    """
    if dry_run:
        print(f"[deliver] would send {len(text)} chars:\n{text}")
        return []
    config.require("TELEGRAM_CHAT_ID")
    payloads = []
    for chunk in split_message(text):
        data = {"chat_id": config.TELEGRAM_CHAT_ID, "text": chunk}
        if parse_mode:
            data["parse_mode"] = parse_mode
        payloads.append(_post("sendMessage", data))
    return payloads


def _todays_signals(conn, date):
    rows = conn.execute(
        "SELECT symbol, rule, direction, entry, stop, target, size, status "
        "FROM signals WHERE date = ? ORDER BY symbol",
        (date,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_report(conn, date):
    lines = [f"<b>nse-assist — {date}</b>"]

    todays = _todays_signals(conn, date)
    lines.append(f"\n<b>Signals ({len(todays)})</b>")
    if todays:
        for signal in todays:
            arrow = "▲" if signal["direction"] == "long" else "▼"
            lines.append(
                f"{arrow} <code>{html.escape(signal['symbol'])}</code> {html.escape(signal['rule'])}"
                f"\n   entry {signal['entry']:.2f} · stop {signal['stop']:.2f} · "
                f"target {signal['target']:.2f} · {signal['size']} sh · {signal['status']}"
            )
    elif not signals.ENABLED_RULES:
        # An empty scan has two very different causes and the reader cannot tell
        # them apart from a zero. "No rule fired" invites you to wait for tomorrow;
        # "no rules enabled" means nothing will fire tomorrow either, and the system
        # is idle by decision rather than by market conditions.
        disabled = ", ".join(sorted(signals.RULES))
        lines.append(
            "<b>NO RULES ENABLED</b> — nothing will fire until one is re-enabled.\n"
            f"All {len(signals.RULES)} were disabled by walk-forward validation: none "
            "was profitable out-of-sample in a majority of windows.\n"
            f"<i>{html.escape(disabled)}</i>"
        )
    else:
        lines.append("none — no rule fired today")

    positions = open_positions(conn)
    lines.append(f"\n<b>Open ({len(positions)}/{risk_config.MAX_OPEN_POSITIONS})</b>")
    for position in positions:
        lines.append(
            f"<code>{html.escape(position['symbol'])}</code> {position['direction']} "
            f"@ {position['entry_price']:.2f} since {position['entry_date']}"
        )
    if not positions:
        lines.append("flat")

    stats = summary(conn)
    lines.append(
        f"\n<b>Ledger</b>\ntoday {realised_pnl(conn, date):,.0f} · "
        f"lifetime {stats['total_pnl']:,.0f} over {stats['closed']} closed "
        f"({stats['wins']} wins)"
    )
    lines.append(
        f"limits: {risk_config.MAX_DAILY_LOSS:,} loss / {risk_config.DAILY_PROFIT_TARGET:,} target"
    )

    navs = funds.latest_navs(conn, fund_watchlist.SCHEME_CODES)
    if navs:
        lines.append("\n<b>Parked cash — latest NAVs</b>")
        for nav in navs:
            label = fund_watchlist.label_for(nav["scheme_code"])
            lines.append(f"{html.escape(label)}\n   {nav['nav']:,.4f} ({nav['date']})")

    health = runlog.health_line()
    if health:
        lines.append(f"\n<i>{html.escape(health)}</i>")

    # Paper only. Worth repeating in the message itself, not just the README.
    lines.append("\n<i>Paper trades. Not investment advice.</i>")
    return "\n".join(lines)


def run(dry_run=False, **kwargs):
    conn = get_connection()
    try:
        init_db(conn)
        report = build_report(conn, today())
        send_message(report, dry_run=dry_run)
        print(f"[deliver] report sent ({len(report)} chars)" if not dry_run else "[deliver] dry run")
        return report
    finally:
        conn.close()
