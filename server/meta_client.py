"""Thin wrapper around the Meta Model API (OpenAI-compatible).

Muse Spark reads the page, classifies intent and phrases hints. It never decides
correctness. Every function degrades to a rule-based fallback when there is no
MODEL_API_KEY or the API call fails (offline mode).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("margin.meta")

BASE_URL = "https://api.meta.ai/v1"
CHAT_MODEL = "muse-spark-1.3"
VOICE_MODEL = "muse-voice-transcribe-1.0"
INTENTS = ("hint", "answer", "check")


class OfflineError(RuntimeError):
    """Raised when a feature has no offline fallback (e.g. audio transcription)."""


def api_key() -> str:
    return os.getenv("MODEL_API_KEY", "").strip()


def is_offline() -> bool:
    return not api_key()


def _client():
    """OpenAI SDK client pointed at the Meta Model API, or None offline."""
    if is_offline():
        return None
    from openai import OpenAI

    return OpenAI(base_url=BASE_URL, api_key=api_key(), timeout=30.0, max_retries=1)


def _chat(messages: list[dict[str, Any]], max_tokens: int = 400) -> str | None:
    """One chat completion; None on any failure."""
    client = _client()
    if client is None:
        return None
    try:
        resp = client.chat.completions.create(
            model=CHAT_MODEL, messages=messages, max_tokens=max_tokens, temperature=0.2
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.warning("Meta API call failed, using offline fallback: %s", exc)
        return None


def _extract_json(text: str) -> Any:
    """Parse JSON from a reply that may be wrapped in ``` fences or prose."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"(\[.*\]|\{.*\})", text, re.S)
        if m:
            return json.loads(m.group(1))
        raise


# --------------------------------------------------------------------------- page reading

READ_PROMPT = """You are reading a photo of a calculus notebook page.
Transcribe EACH handwritten math line, top to bottom, as plain text SymPy can parse:
use ∫ ... dx for integrals, ^ for powers, sin(x), cos(x), ln(x), e^x, sqrt(x), and keep
a leading "=" if the line starts with one. Write "+ C" if the student wrote it.
Do not fix mistakes: copy exactly what is written. Skip doodles and prose.
Return ONLY JSON: {"lines": [{"text": "∫ x cos(x) dx", "bbox": [x, y, w, h]}]}
where bbox is the line's box in image pixels, origin top-left."""


def read_page(image_dataurl: str) -> list[dict[str, Any]]:
    """Muse Spark vision: page image -> [{text, bbox}]. Empty list offline or on failure."""
    if not image_dataurl or is_offline():
        return []
    reply = _chat(
        [{"role": "user", "content": [
            {"type": "text", "text": READ_PROMPT},
            {"type": "image_url", "image_url": {"url": image_dataurl}},
        ]}],
        max_tokens=1200,
    )
    if not reply:
        return []
    try:
        data = _extract_json(reply)
    except ValueError:
        log.warning("read_page: reply was not JSON: %r", reply[:200])
        return []
    items = data.get("lines", []) if isinstance(data, dict) else data
    lines = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        bbox = item.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            bbox = None
        lines.append({"text": str(item["text"]).strip(), "bbox": bbox})
    return lines


# --------------------------------------------------------------------------- intent


def keyword_intent(command: str) -> str:
    """Offline intent: answer / check / hint from keywords."""
    c = (command or "").lower()
    if any(k in c for k in ("answer", "solution", "solve it", "finish", "just give", "show me how", "do it for me")):
        return "answer"
    if any(k in c for k in ("check", "is this right", "am i right", "correct?", "verify")):
        return "check"
    return "hint"


def classify_intent(command: str) -> str:
    """hint | answer | check. Muse Spark when online, keywords otherwise."""
    if not command or is_offline():
        return keyword_intent(command)
    reply = _chat([
        {"role": "system", "content": "Classify a student's request to a math tutor. Reply with exactly one word: "
         "hint (wants help or a nudge), answer (wants the full solution or remaining steps), "
         "or check (wants to know whether their work is right)."},
        {"role": "user", "content": command},
    ], max_tokens=5)
    word = (reply or "").strip().lower().strip(".!\"'")
    return word if word in INTENTS else keyword_intent(command)


# --------------------------------------------------------------------------- hints


def phrase_hint(verdict: str, line: str, level: int, fallback: str | None = None) -> str:
    """Rewrite a SymPy verdict as a short (<= 8 words) friendly ink hint.

    Level 1-2 must not reveal the answer. Level 3 is the exact fix, so the rule-based
    text is used as is (math must stay exact). Falls back to `fallback` offline.
    """
    fallback = fallback or "Check this step"
    if level >= 3 or is_offline():
        return fallback
    reply = _chat([
        {"role": "system", "content": (
            "You write tiny margin notes on a student's calculus page in handwriting. "
            "Rewrite the checker's verdict as ONE friendly hint of at most 8 words. "
            + ("Gentle nudge only: say where to look, never the correct result. " if level == 1 else
               "Name the rule or mistake, but never write the corrected expression. ")
            + "No quotes, no emoji, no preamble.")},
        {"role": "user", "content": f"Student line: {line}\nChecker verdict: {verdict}\nRule-based hint: {fallback}"},
    ], max_tokens=30)
    hint = (reply or "").strip().strip("\"'").splitlines()[0] if reply else ""
    if not hint or len(hint.split()) > 10:
        return fallback
    return hint


# --------------------------------------------------------------------------- voice


def transcribe(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """Muse Voice Transcribe. Raises OfflineError offline or on failure (caller returns 503)."""
    client = _client()
    if client is None:
        raise OfflineError("no MODEL_API_KEY")
    try:
        resp = client.audio.transcriptions.create(model=VOICE_MODEL, file=(filename, audio_bytes))
    except Exception as exc:
        raise OfflineError(f"transcription failed: {exc}") from exc
    return (getattr(resp, "text", None) or "").strip()
