"""
summary.py — an optional "quick take" (3 plain-English bullets) generated
using the VISITOR'S OWN Gemini key — the same key used for the real
extraction (Step2). There is no separate owner key anymore.

This is never load-bearing. If it fails, is empty, or the key hits its
quota, it silently returns None — the main extraction is completely
unaffected either way.

Auto-lock / auto-reset
-----------------------
Gemini's free tier enforces a daily request quota. When a 429 /
RESOURCE_EXHAUSTED comes back, Google's error payload usually includes a
`retryDelay` in seconds — used if present, otherwise a conservative 24h
lock is applied (the free tier's daily counter resets at midnight Pacific).
The lock is persisted in the shared sqlite db, so:
  - it survives server restarts
  - every call checks "is now < locked_until?" first — a no-op, no wasted
    request, and nothing visible breaks for any visitor while locked
  - once the stored timestamp passes, it just starts working again on its
    own, no code change or redeploy needed
"""
from __future__ import annotations

import os
import re
import time
import logging

import store

log = logging.getLogger("arx.summary")

_MODEL = "gemini-3.1-flash-lite"   # cheap + fast — this is a bonus, not the product
_DEFAULT_LOCK_SECONDS = 24 * 60 * 60
_MAX_INPUT_CHARS = 15_000           # keep the bonus call small and cheap


def is_locked() -> bool:
    return time.time() < store.get_summary_lock()


def generate(pdf_text: str, api_key: str) -> list[str] | None:
    """Best-effort. Returns up to 3 bullet strings, or None.

    api_key is the caller-supplied (visitor's) Gemini key — the same one
    used for the real extraction. No separate owner key is used anymore.
    """
    if not api_key:
        return None
    if is_locked():
        return None

    from google import genai as google_genai
    from google.genai import types

    text = pdf_text[:_MAX_INPUT_CHARS]
    prompt = (
        "In exactly 3 short bullet points, plain English, summarise how this "
        "company performed this year based on the financial-statement excerpt "
        "below. No jargon, no explanation of formatting, just the headline "
        "story (revenue direction, profit direction, one notable detail). "
        "Return only the 3 bullets, one per line, each starting with '- '.\n\n"
        + text
    )

    try:
        client = google_genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=200),
        )
        lines = [
            re.sub(r"^-+\s*", "", ln).strip()
            for ln in (response.text or "").splitlines()
            if ln.strip()
        ]
        bullets = [ln for ln in lines if ln][:3]
        return bullets or None

    except Exception as exc:  # noqa: BLE001 — SDK exception types vary by version
        msg = str(exc)
        if "RESOURCE_EXHAUSTED" in msg or "429" in msg or "quota" in msg.lower():
            lock_seconds = _extract_retry_delay(msg) or _DEFAULT_LOCK_SECONDS
            store.set_summary_lock(time.time() + lock_seconds)
            log.warning(
                "Owner Gemini key hit its quota — locking the summary "
                "feature for %ss (auto-reopens after that).", lock_seconds,
            )
        else:
            log.warning("Summary generation failed (non-fatal): %s", exc)
        return None


def _extract_retry_delay(msg: str) -> int | None:
    m = re.search(r'"retryDelay":\s*"(\d+)s"', msg)
    return int(m.group(1)) if m else None