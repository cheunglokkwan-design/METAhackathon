"""Thin wrapper around the Meta Model API (OpenAI-compatible).

Muse Spark reads the page, classifies intent and phrases hints. It never decides
correctness. Every function degrades to a rule-based fallback when there is no
MODEL_API_KEY or the API call fails (offline mode).
"""

from __future__ import annotations

from collections import deque
import json
import logging
import os
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("margin.meta")

BASE_URL = os.getenv("MODEL_BASE_URL", "https://api.meta.ai/v1")
CHAT_MODEL = os.getenv("CHAT_MODEL", "muse-spark-1.3")
VOICE_MODEL = os.getenv("VOICE_MODEL", "muse-voice-transcribe-1.0")
INTENTS = ("hint", "answer", "check")
MAX_IMAGE_CHARS = 5_000_000
MAX_AUDIO_BYTES = 10 * 1024 * 1024
_DATA_IMAGE_RE = re.compile(r"^data:image/(?:png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=\s]+$", re.A)


class OfflineError(RuntimeError):
    """Raised when a feature has no offline fallback (e.g. audio transcription)."""


class RateLimiter:
    """Thread-safe sliding-window rate limiter for outbound Meta API calls."""

    def __init__(self, max_calls: int | None = None, period: float | None = None) -> None:
        self._max_calls, self._period = max_calls, period
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        max_calls = self._max_calls if self._max_calls is not None else int(os.getenv("META_RATE_LIMIT_CALLS", "30"))
        period = self._period if self._period is not None else float(os.getenv("META_RATE_LIMIT_PERIOD", "60.0"))
        if max_calls <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            while self._timestamps and self._timestamps[0] <= now - period:
                self._timestamps.popleft()
            if len(self._timestamps) >= max_calls:
                return False
            self._timestamps.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._timestamps.clear()


class CircuitBreaker:
    """Thread-safe circuit breaker (closed -> open -> half_open -> closed)."""

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, failure_threshold: int | None = None, recovery_timeout: float | None = None) -> None:
        self._threshold, self._timeout = failure_threshold, recovery_timeout
        self._lock = threading.Lock()
        self.reset()

    def _cfg(self) -> tuple[int, float]:
        t = self._threshold if self._threshold is not None else max(1, int(os.getenv("META_CB_FAILURE_THRESHOLD", "3")))
        r = self._timeout if self._timeout is not None else float(os.getenv("META_CB_RECOVERY_TIMEOUT", "60.0"))
        return t, r

    @property
    def state(self) -> str:
        _, timeout = self._cfg()
        with self._lock:
            if self._state == self.OPEN and (time.monotonic() - self._opened_at) >= timeout:
                return self.HALF_OPEN
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def allow_request(self) -> bool:
        _, timeout = self._cfg()
        now = time.monotonic()
        with self._lock:
            if self._state == self.CLOSED:
                return True
            if self._state == self.OPEN:
                if (now - self._opened_at) < timeout:
                    return False
                self._state, self._probe = self.HALF_OPEN, True
                return True
            if not self._probe:
                self._probe = True
                return True
            return False

    def cancel_probe(self) -> None:
        with self._lock:
            if self._state == self.HALF_OPEN:
                self._probe = False

    def record_success(self) -> None:
        with self._lock:
            self._failure_count, self._state, self._probe = 0, self.CLOSED, False

    def record_failure(self) -> None:
        threshold, _ = self._cfg()
        now = time.monotonic()
        with self._lock:
            self._failure_count += 1
            if self._state == self.HALF_OPEN or self._failure_count >= threshold:
                self._state, self._opened_at, self._probe = self.OPEN, now, False

    def reset(self) -> None:
        with self._lock:
            self._state, self._failure_count, self._opened_at, self._probe = self.CLOSED, 0, 0.0, False


rate_limiter = RateLimiter()
circuit_breaker = CircuitBreaker()


def api_key() -> str:
    return os.getenv("MODEL_API_KEY", "").strip()


def base_url() -> str:
    """Validated Meta API base URL from MODEL_BASE_URL (HTTPS required except localhost)."""
    url = os.getenv("MODEL_BASE_URL", BASE_URL).strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not parsed.netloc or (parsed.scheme != "https" and not (parsed.scheme == "http" and host in ("localhost", "127.0.0.1"))):
        raise ValueError("MODEL_BASE_URL must be an https:// URL (or http://localhost)")
    return url


def is_offline() -> bool:
    return not api_key()


def _client():
    """OpenAI SDK client pointed at the Meta Model API, or None offline."""
    if is_offline():
        return None
    from openai import OpenAI

    return OpenAI(base_url=base_url(), api_key=api_key(), timeout=30.0, max_retries=1)


def _chat(messages: list[dict[str, Any]], max_tokens: int = 400) -> str | None:
    """One chat completion; None on any failure, rate limit, or open circuit."""
    try:
        client = _client()
    except Exception as exc:
        log.warning("Meta client configuration invalid, using offline fallback: %s", exc)
        return None
    if client is None:
        return None
    if not circuit_breaker.allow_request():
        log.warning("Meta API circuit breaker is OPEN, using offline fallback")
        return None
    if not rate_limiter.allow():
        circuit_breaker.cancel_probe()
        log.warning("Meta API rate limit exceeded, using offline fallback")
        return None
    try:
        resp = client.chat.completions.create(
            model=CHAT_MODEL, messages=messages, max_tokens=max_tokens, temperature=0.2
        )
        circuit_breaker.record_success()
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        circuit_breaker.record_failure()
        log.warning("Meta API call failed, using offline fallback: %s", type(exc).__name__)
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
    url = image_dataurl.strip()
    if len(url) > MAX_IMAGE_CHARS or not _DATA_IMAGE_RE.match(url):
        log.warning("read_page: rejected invalid or oversized image data URL")
        return []
    reply = _chat(
        [{"role": "user", "content": [
            {"type": "text", "text": READ_PROMPT},
            {"type": "image_url", "image_url": {"url": url}},
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
    if not command or not command.strip() or is_offline():
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
    if not audio_bytes:
        raise OfflineError("empty audio payload")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise OfflineError("audio payload too large")
    try:
        client = _client()
    except Exception as exc:
        log.warning("Meta client configuration invalid: %s", exc)
        raise OfflineError("service unavailable") from exc
    if client is None:
        raise OfflineError("no MODEL_API_KEY")
    if not circuit_breaker.allow_request():
        log.warning("Meta API circuit breaker is OPEN, skipping transcription")
        raise OfflineError("circuit breaker open")
    if not rate_limiter.allow():
        circuit_breaker.cancel_probe()
        log.warning("Meta API rate limit exceeded, skipping transcription")
        raise OfflineError("rate limit exceeded")
    safe_name = os.path.basename(filename or "audio.webm") or "audio.webm"
    try:
        resp = client.audio.transcriptions.create(model=VOICE_MODEL, file=(safe_name, audio_bytes))
        circuit_breaker.record_success()
    except Exception as exc:
        circuit_breaker.record_failure()
        log.warning("Meta transcription failed: %s", type(exc).__name__)
        raise OfflineError("transcription failed") from exc
    return (getattr(resp, "text", None) or "").strip()
