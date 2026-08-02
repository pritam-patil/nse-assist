"""Unified interface to free-tier LLMs: Gemini first, Groq as fallback.

Plain HTTP via requests only — no provider SDKs, so a model or API shape change
doesn't drag in a dependency upgrade.

Ported from content-pipeline, deliberately unchanged in shape so a fix in either
repo reads the same way in the other. The only caller here is src/sentiment.py,
which treats every failure as a no-op — so LLMError propagating out of this module
costs an annotation, never a stage.
"""

import json
import re
import time

import requests

from src.config import GEMINI_API_KEY, GEMINI_MODEL, GROQ_API_KEY, GROQ_MODEL

GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

REQUEST_TIMEOUT_SECONDS = 30
MIN_SECONDS_BETWEEN_CALLS = 2.0
JSON_MODE_INSTRUCTION = (
    "Respond with ONLY valid JSON. No markdown code fences, no commentary, no explanation."
)
JSON_RETRY_INSTRUCTION = (
    "Your previous response was not valid JSON. Return ONLY valid JSON this time."
)

_last_call_at = 0.0


class LLMError(RuntimeError):
    """Raised when both Gemini and Groq fail to produce a usable response."""


def _rate_limit():
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    wait = MIN_SECONDS_BETWEEN_CALLS - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def _strip_json_fences(text):
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return match.group(1) if match else text


def _call_gemini(prompt, system):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")

    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    response = requests.post(
        GEMINI_URL_TEMPLATE.format(model=GEMINI_MODEL),
        params={"key": GEMINI_API_KEY},
        json=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code == 429:
        raise LLMError(f"Gemini rate limited: {response.text[:200]}")
    response.raise_for_status()
    data = response.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    usage = data.get("usageMetadata", {})
    print(
        f"[llm] Gemini ({GEMINI_MODEL}) token usage: "
        f"prompt={usage.get('promptTokenCount', '?')} "
        f"completion={usage.get('candidatesTokenCount', '?')} "
        f"total={usage.get('totalTokenCount', '?')}"
    )
    return text


def _call_groq(prompt, system):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": GROQ_MODEL, "messages": messages},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code == 429:
        raise LLMError(f"Groq rate limited: {response.text[:200]}")
    response.raise_for_status()
    data = response.json()

    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    print(
        f"[llm] Groq ({GROQ_MODEL}) token usage: "
        f"prompt={usage.get('prompt_tokens', '?')} "
        f"completion={usage.get('completion_tokens', '?')} "
        f"total={usage.get('total_tokens', '?')}"
    )
    return text


def _call_with_fallback(prompt, system):
    """Tries Gemini, then Groq on any error or rate limit. Raises LLMError if both fail."""
    _rate_limit()
    try:
        return _call_gemini(prompt, system), "gemini"
    except Exception as exc:
        print(f"[llm] Gemini call failed, falling back to Groq: {exc}")
        from src import runlog

        runlog.log("llm", "fallback", "gemini->groq")

    _rate_limit()
    try:
        return _call_groq(prompt, system), "groq"
    except Exception as exc:
        raise LLMError(f"Both Gemini and Groq failed; last error: {exc}") from exc


def _parse_json(text):
    # strict=False tolerates raw newlines and tabs inside string values. Models emit
    # them routinely whenever the content itself is multi-line — an Instagram caption
    # with paragraph breaks reproduces it every time — and strict JSON forbids literal
    # control characters in strings. Retrying does not help: the model makes the same
    # choice again, so the retry just burns a second call before failing identically.
    return json.loads(_strip_json_fences(text), strict=False)


def generate(prompt, system=None, json_mode=False):
    """Generates text via Gemini, falling back to Groq on error or rate limit.

    If json_mode=True, instructs the model to return only JSON and parses the
    result (stripping markdown fences), retrying once on parse failure. Returns
    the parsed JSON value in that case, otherwise the raw text.
    """
    effective_system = system
    if json_mode:
        effective_system = (
            f"{system}\n\n{JSON_MODE_INSTRUCTION}" if system else JSON_MODE_INSTRUCTION
        )

    text, provider = _call_with_fallback(prompt, effective_system)
    if not json_mode:
        return text

    try:
        return _parse_json(text)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[llm] JSON parse failed from {provider}, retrying once: {exc}")
        retry_system = f"{effective_system}\n\n{JSON_RETRY_INSTRUCTION}"
        text, provider = _call_with_fallback(prompt, retry_system)
        return _parse_json(text)
