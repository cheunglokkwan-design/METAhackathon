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
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "muse-image-1.0")
MAX_PICTURE_B64 = 3_000_000          # keeps the reply under Vercel's 4.5 MB response limit
INTENTS = ("hint", "answer", "check", "typeset", "graph", "verify", "next", "why", "picture")
AGENT_MAX_ITERS = 4
MAX_IMAGE_CHARS = 5_000_000
MAX_AUDIO_BYTES = 10 * 1024 * 1024
_DATA_IMAGE_RE = re.compile(r"^data:image/(?:png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=\s]+$", re.A)


class OfflineError(RuntimeError):
    """Raised when a feature has no offline fallback (e.g. audio transcription)."""


class RateLimiter:
    """Thread-safe sliding-window rate limiter for outbound model API calls.

    Limits come from <env_prefix>_RATE_LIMIT_CALLS / _PERIOD unless given explicitly.
    """

    def __init__(self, max_calls: int | None = None, period: float | None = None,
                 env_prefix: str = "META") -> None:
        self._max_calls, self._period, self._env = max_calls, period, env_prefix
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        max_calls = self._max_calls if self._max_calls is not None else int(os.getenv(f"{self._env}_RATE_LIMIT_CALLS", "30"))
        period = self._period if self._period is not None else float(os.getenv(f"{self._env}_RATE_LIMIT_PERIOD", "60.0"))
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
    """Thread-safe circuit breaker (closed -> open -> half_open -> closed).

    Settings come from <env_prefix>_CB_FAILURE_THRESHOLD / _RECOVERY_TIMEOUT unless given.
    """

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, failure_threshold: int | None = None, recovery_timeout: float | None = None,
                 env_prefix: str = "META") -> None:
        self._threshold, self._timeout, self._env = failure_threshold, recovery_timeout, env_prefix
        self._lock = threading.Lock()
        self.reset()

    def _cfg(self) -> tuple[int, float]:
        t = self._threshold if self._threshold is not None else max(1, int(os.getenv(f"{self._env}_CB_FAILURE_THRESHOLD", "3")))
        r = self._timeout if self._timeout is not None else float(os.getenv(f"{self._env}_CB_RECOVERY_TIMEOUT", "60.0"))
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


def check_base_url(url: str, var: str = "MODEL_BASE_URL") -> str:
    """`url` if it is https:// (or http://localhost); ValueError naming `var` otherwise."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not parsed.netloc or (parsed.scheme != "https" and not (parsed.scheme == "http" and host in ("localhost", "127.0.0.1"))):
        raise ValueError(f"{var} must be an https:// URL (or http://localhost)")
    return url


def base_url() -> str:
    """Validated Meta API base URL from MODEL_BASE_URL (HTTPS required except localhost)."""
    return check_base_url(os.getenv("MODEL_BASE_URL", BASE_URL).strip())


def is_offline() -> bool:
    return not api_key()


def _client():
    """OpenAI SDK client pointed at the Meta Model API, or None offline."""
    if is_offline():
        return None
    from openai import OpenAI

    return OpenAI(base_url=base_url(), api_key=api_key(), timeout=30.0, max_retries=1)


def _chat(messages: list[dict[str, Any]], max_tokens: int = 1500) -> str | None:
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


PICTURE_STYLE = ("A clean hand-drawn pencil sketch on plain white paper, like a diagram in a maths or physics "
                 "textbook: simple dark grey lines, light hatching, no colour, no text or labels. Subject: ")


def _image_mime(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return None


def generate_picture(description: str) -> str | None:
    """A pencil-sketch picture from Muse Image as a data: URL, or None (offline, refused, too big)."""
    import base64

    import httpx

    if is_offline() or not description.strip():
        return None
    if not circuit_breaker.allow_request():
        return None
    if not rate_limiter.allow():
        circuit_breaker.cancel_probe()
        return None
    try:
        resp = httpx.post(
            base_url().rstrip("/") + "/images/generations",
            headers={"Authorization": f"Bearer {api_key()}", "Accept": "application/json"},
            json={"model": IMAGE_MODEL, "prompt": PICTURE_STYLE + description.strip()[:300], "n": 1,
                  "size": "1024x1024"},
            timeout=60.0,
        )
        if resp.status_code != 200:
            circuit_breaker.record_success()            # Meta answered; it refused this prompt
            log.warning("Muse Image HTTP %s: %s", resp.status_code, resp.text[:160])
            return None
        b64 = (resp.json().get("data") or [{}])[0].get("b64_json") or ""
        circuit_breaker.record_success()
    except Exception as exc:
        circuit_breaker.record_failure()
        log.warning("Muse Image failed: %s", type(exc).__name__)
        return None
    if not b64 or len(b64) > MAX_PICTURE_B64:
        return None
    try:
        mime = _image_mime(base64.b64decode(b64[:64] + "=" * (-len(b64[:64]) % 4)))
    except ValueError:
        mime = None
    return f"data:{mime};base64,{b64}" if mime else None


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

READ_PROMPT = """You are reading an image of a student's handwritten math notebook page.
Transcribe EVERY line of handwritten math or numbers, top to bottom, even if it is simple
arithmetic or not calculus. Write each line as plain text SymPy can parse: ∫ ... dx for
integrals, ^ for powers, sin(x), cos(x), ln(x), e^x, sqrt(x); keep a leading "=" if the line
starts with one, and "+ C" if the student wrote it. Copy mistakes exactly; do not fix them.
Return ONLY JSON, no explanation: {"lines": [{"text": "∫ x cos(x) dx", "bbox": [x, y, w, h]}]}
where bbox is the line's box in image pixels, origin top-left. If there is truly no math,
return {"lines": []}."""

TEXT_PROMPT = """Transcribe the handwriting in this image exactly, line by line, top to bottom: words as
words, maths as plain text (x^2, sqrt(x), ∫ x dx, sin(30)). Fix nothing and add nothing.
Return ONLY JSON: {"lines": ["first line", "second line"]}. If nothing is written, {"lines": []}."""


def read_handwriting(image_dataurl: str) -> list[str]:
    """Muse Spark vision: the handwriting in a cropped image as lines of text ([] offline or on failure)."""
    url = (image_dataurl or "").strip()
    if is_offline() or len(url) > MAX_IMAGE_CHARS or not _DATA_IMAGE_RE.match(url):
        return []
    reply = _chat([{"role": "system", "content": TEXT_PROMPT},
                   {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}],
                  max_tokens=READ_MAX_TOKENS)
    data = _extract_json(reply or "")
    lines = data.get("lines") if isinstance(data, dict) else None
    texts = [str(l.get("text", "") if isinstance(l, dict) else l)[:300].strip() for l in (lines or [])]
    return [t for t in texts if t][:40]


# Muse Spark is a reasoning model: thinking tokens count against max_tokens, so a small budget
# can end with an empty answer (finish_reason "length"). Give page reading plenty of room.
READ_MAX_TOKENS = int(os.getenv("READ_MAX_TOKENS", "8000"))
READ_TIMEOUT_S = float(os.getenv("READ_TIMEOUT_S", "55"))


def read_error_text(reason: str | None) -> str:
    """Why Meta couldn't read the page, in words a student understands."""
    r = (reason or "").lower()
    if "timeout" in r:
        return "Meta took too long to answer"
    if "429" in r or "rate limit" in r:
        return "too many requests at once, wait a few seconds"
    if "paused" in r:
        return "Meta had errors, so it's paused for a minute"
    if "401" in r or "403" in r:
        return "the Meta API key was refused"
    if "empty reply" in r:
        return "Meta ran out of thinking room"
    if "too large" in r:
        return "the page picture was too big"
    if "error 5" in r:
        return "Meta's server had an error"
    if "connection" in r:
        return "couldn't reach Meta"
    return reason or "unknown error"

# Why the last read_page call found nothing, for the page's agent trace (None when it worked).
last_read_error: str | None = None


def _chat_full(messages: list[dict[str, Any]], max_tokens: int) -> tuple[str | None, str]:
    """Like _chat, but also says why there is no text: (text or None, reason)."""
    try:
        client = _client()
    except Exception as exc:
        return None, f"bad API config ({type(exc).__name__})"
    if client is None:
        return None, "offline"
    if not circuit_breaker.allow_request():
        return None, "Meta API paused after repeated errors"
    if not rate_limiter.allow():
        circuit_breaker.cancel_probe()
        return None, "rate limit reached"
    resp = None
    for attempt in range(2):
        try:
            # Reading handwriting is a reasoning call: give it more time than the 30 s default.
            c = client.with_options(timeout=READ_TIMEOUT_S, max_retries=0) if hasattr(client, "with_options") else client
            resp = c.chat.completions.create(
                model=CHAT_MODEL, messages=messages, max_tokens=max_tokens)
            break
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            log.warning("read_page: Meta API error %s (try %d): %s", status, attempt + 1, str(exc)[:300])
            transient = status is None or status == 429 or status >= 500
            if attempt == 0 and transient:
                continue                                   # one more try for timeouts and server errors
            circuit_breaker.record_failure()
            return None, f"Meta API error {status or type(exc).__name__}"
    circuit_breaker.record_success()
    choice = resp.choices[0]
    text = (choice.message.content or "").strip()
    finish = getattr(choice, "finish_reason", None)
    log.info("read_page: finish_reason=%s, %d chars, usage=%s", finish, len(text),
             getattr(resp, "usage", None))
    if not text:
        return None, f"empty reply (finish_reason={finish})"
    return text, "ok"


def _parse_lines(reply: str) -> list[dict[str, Any]] | None:
    """JSON lines from a reply; plain text lines as a fallback; None if nothing usable."""
    try:
        data = _extract_json(reply)
    except ValueError:
        data = None
    if data is not None:
        items = data.get("lines", []) if isinstance(data, dict) else data
        lines = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, str) and item.strip():
                lines.append({"text": item.strip(), "bbox": None})
                continue
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                continue
            bbox = item.get("bbox")
            ok = isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox)
            lines.append({"text": str(item["text"]).strip(), "bbox": [float(v) for v in bbox] if ok else None})
        return lines
    # Not JSON: take lines that look like maths (digits, operators or ∫).
    rows = [r.strip(" -*`") for r in reply.splitlines()]
    rows = [r for r in rows if r and re.search(r"[0-9∫=+^]", r) and len(r) < 120]
    return [{"text": r, "bbox": None} for r in rows] or None


def read_page(image_dataurl: str) -> list[dict[str, Any]]:
    """Muse Spark vision: page image -> [{text, bbox}]. Empty list offline or on failure.

    Sets last_read_error to a short reason when nothing was read.
    """
    global last_read_error
    last_read_error = None
    if not image_dataurl or is_offline():
        last_read_error = "offline" if image_dataurl else "no page image"
        return []
    url = image_dataurl.strip()
    if len(url) > MAX_IMAGE_CHARS or not _DATA_IMAGE_RE.match(url):
        log.warning("read_page: rejected invalid or oversized image data URL")
        last_read_error = "page image too large or not an image"
        return []
    messages = [{"role": "user", "content": [
        {"type": "text", "text": READ_PROMPT},
        {"type": "image_url", "image_url": {"url": url}},
    ]}]
    reply, reason = _chat_full(messages, READ_MAX_TOKENS)
    if reply is None and reason.startswith("empty reply"):
        reply, reason = _chat_full(messages, READ_MAX_TOKENS * 2)   # one retry with more room
    if reply is None:
        last_read_error = reason
        return []
    lines = _parse_lines(reply)
    if lines is None:
        log.warning("read_page: unusable reply: %r", reply[:300])
        last_read_error = "Meta's reply had no readable lines"
        return []
    if not lines:
        last_read_error = "Meta saw no math on the page"
    return lines


# --------------------------------------------------------------------------- intent


NEXT_RE = re.compile(r"what'?s next|what is next|what next|next step|what now|\bcontinue\b|keep going|one more step")
WHY_RE = re.compile(r"\bwhy\b|\bexplain|how does (?:this|that|it) work|how did (?:you|that)")


def keyword_intent(command: str) -> str:
    """Offline intent from keywords: typeset / graph / verify / next / why / answer / check / hint."""
    c = (command or "").lower().replace("’", "'")
    if any(k in c for k in ("tidy", "typeset", "neat", "type it up", "clean up", "write it out properly")):
        return "typeset"
    if any(k in c for k in ("picture", "image", "diagram", "illustrat", "drawing of")):
        return "picture"
    if any(k in c for k in ("graph", "plot", "sketch", "draw the curve", "draw it")):
        return "graph"
    if "check" in c and any(k in c for k in ("differentiat", "derivative")):
        return "verify"  # "check it by differentiating"
    if NEXT_RE.search(c):
        return "next"
    if WHY_RE.search(c):
        return "why"
    if any(k in c for k in ("answer", "solution", "solve it", "finish", "just give", "show me how", "do it for me")):
        return "answer"
    if any(k in c for k in ("check", "is this right", "am i right", "correct?", "verify")):
        return "check"
    return "hint"


def classify_intent(command: str) -> str:
    """One of INTENTS. Muse Spark when online, keywords otherwise."""
    if not command or not command.strip() or is_offline():
        return keyword_intent(command)
    reply = _chat([
        {"role": "system", "content": "Classify a student's request to a math tutor. Reply with exactly one word: "
         "hint (wants help or a nudge), answer (wants the full solution or remaining steps), "
         "check (wants to know whether their work is right), typeset (wants the page tidied or typed up), "
         "graph (wants a graph or sketch), verify (wants the answer checked by differentiating), "
         "next (wants only the next step written), or why (wants the rule behind a step explained)."},
        {"role": "user", "content": command},
    ], max_tokens=1500)
    word = (reply or "").strip().lower().strip(".!\"'")
    return word if word in INTENTS else keyword_intent(command)


# --------------------------------------------------------------------------- agent loop

AGENT_PROMPT = """You are Margin, a calculus tutor that writes tiny notes in ink on a student's notebook page.
You act ONLY through tools. Every tool runs a SymPy referee on the page and writes the ink itself;
you never write mathematics, answers or hints yourself.
Pick the tool(s) that match the request, usually exactly one:
- help / stuck / hint -> give_hint
- what's next / next step / continue -> next_step (writes one step only)
- why / explain / how does this work -> explain_rule
- is this right / check -> check_steps
- just give me the answer / finish it -> finish_answer
- tidy / typeset / neat -> typeset_lines
- graph / plot / sketch -> sketch_graph
- check by differentiating -> differentiate_check
- 3D graph / surface / z = f(x, y) / two variables -> sketch_3d (pass the expression if it is not on the page)
- picture / diagram / image / "draw a ..." something that is not a graph -> draw_picture with a short
  description of the scene (e.g. "a ladder leaning on a wall", "a cone being filled with water")
You may combine two (e.g. "check it and graph it"). Never call finish_answer unless the student
asked for the answer. When you are done, reply with the single word DONE."""


def _page_text(lines: list[dict[str, Any]]) -> str:
    return "\n".join(f"{l.get('id') or f'L{i}'}: {l.get('text', '')}" for i, l in enumerate(lines, 1))


def _tool_calls_of(msg: Any) -> list[tuple[str, str, dict[str, Any]]]:
    """[(id, name, args)] from an SDK message; malformed arguments become {}."""
    out = []
    for i, call in enumerate(getattr(msg, "tool_calls", None) or []):
        fn = getattr(call, "function", None)
        name = getattr(fn, "name", None)
        if not name:
            continue
        try:
            args = json.loads(getattr(fn, "arguments", None) or "{}")
        except ValueError:
            args = {}
        out.append((getattr(call, "id", None) or f"call_{i}", name, args if isinstance(args, dict) else {}))
    return out


def run_agent(command: str, lines: list[dict[str, Any]], image: str | None = None,
              hint_level: int = 1, toolbox: Any = None) -> dict[str, Any] | None:
    """Muse Spark picks tools; each runs locally (server.skills) and its result feeds back.

    At most AGENT_MAX_ITERS model calls. The page's annotations come only from tool
    results. A plain-text reply with no tool call falls back to keyword routing.
    Returns {"mode", "annotations", "agent_trace"}, or None when offline or the very first
    model call fails (the caller then routes by keywords).
    """
    try:
        client = _client()
    except Exception as exc:
        log.warning("Meta client configuration invalid, using offline fallback: %s", exc)
        return None
    if client is None:
        return None
    from .skills import TOOL_SPECS, Toolbox  # local import: skills imports this module

    box = toolbox or Toolbox(lines, hint_level)
    user: list[dict[str, Any]] = [{"type": "text", "text": f"Student said: {command or '(nothing)'}\n"
                                   f"Page lines:\n{_page_text(lines) or '(none)'}"}]
    if image and len(image.strip()) <= MAX_IMAGE_CHARS and _DATA_IMAGE_RE.match(image.strip()):
        user.append({"type": "image_url", "image_url": {"url": image.strip()}})
    messages: list[dict[str, Any]] = [{"role": "system", "content": AGENT_PROMPT},
                                      {"role": "user", "content": user}]
    ran = 0
    for it in range(AGENT_MAX_ITERS):
        if not circuit_breaker.allow_request():
            log.warning("Meta API circuit breaker is OPEN, stopping agent")
            if it == 0:
                return None
            box.trace.append({"tool": "muse-spark", "summary": "circuit breaker open; stopped"})
            break
        if not rate_limiter.allow():
            circuit_breaker.cancel_probe()
            log.warning("Meta API rate limit exceeded, stopping agent")
            if it == 0:
                return None
            box.trace.append({"tool": "muse-spark", "summary": "rate limit exceeded; stopped"})
            break
        try:
            resp = client.chat.completions.create(
                model=CHAT_MODEL, messages=messages, tools=TOOL_SPECS, tool_choice="auto",
                max_tokens=4000, temperature=0.2)
            circuit_breaker.record_success()
            msg = resp.choices[0].message
        except Exception as exc:
            circuit_breaker.record_failure()
            log.warning("agent call %d failed: %s", it + 1, type(exc).__name__)
            if it == 0:
                return None
            box.trace.append({"tool": "muse-spark", "summary": "model call failed; stopped"})
            break
        calls = _tool_calls_of(msg)
        if not calls:
            if ran == 0:
                _route_from_text(box, msg.content or "", command)
            break
        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for cid, name, args in calls]})
        for cid, name, args in calls:
            result = box.run(name, args)
            ran += 1
            messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(result)})
    return box.output()


def _route_from_text(box: Any, text: str, command: str) -> None:
    """The model answered in prose: use a tool it named, else the keyword intent."""
    from .skills import TOOL_MODE

    named = [t for t in TOOL_MODE if t in text]
    if named:
        box.trace.append({"tool": "muse-spark", "summary": f"named {named[0]} in text"})
        box.run(named[0], {})
        return
    intent = keyword_intent(command)
    box.route(intent, f"model replied in text; keywords -> {intent}", command)


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
    ], max_tokens=1500)
    hint = (reply or "").strip().strip("\"'").splitlines()[0] if reply else ""
    if not hint or len(hint.split()) > 10:
        return fallback
    return hint


def phrase_why(prose: list[str], topic: str) -> list[str] | None:
    """Muse Spark rewords the plain-word lines of a why-note (never the formula or maths).

    Returns None offline or when the reply is unusable; explain.explanation() also rejects
    any line with maths in it or over 12 words, keeping the template's own line.
    """
    if is_offline() or not prose:
        return None
    reply = _chat([
        {"role": "system", "content": (
            "You reword tiny margin notes that explain a calculus rule to a student. Reword each line "
            "in a friendly voice, at most 12 words per line, same meaning. Plain words only: no "
            "formulas, symbols, digits or variable letters. Return ONLY JSON: {\"lines\": [...]} "
            "with exactly as many lines as given.")},
        {"role": "user", "content": json.dumps({"rule": topic.replace("_", " "), "lines": prose})},
    ], max_tokens=1500)
    if not reply:
        return None
    try:
        data = _extract_json(reply)
    except ValueError:
        return None
    lines = data.get("lines") if isinstance(data, dict) else data
    if not isinstance(lines, list) or len(lines) != len(prose):
        return None
    return [str(l).strip() for l in lines]


# --------------------------------------------------------------------------- voice


# Calculus words the recogniser should prefer ("integral", not "in a grill").
VOICE_KEYWORDS = ["Meta", "integral", "integrate", "derivative", "differentiate", "sine", "cosine",
                  "tangent", "dx", "chain rule", "product rule", "quotient rule", "limit", "squared",
                  "cubed", "hint", "check my work", "answer", "graph", "tidy", "why", "next step"]


class TranscribeError(Exception):
    """Muse Voice Transcribe answered with an error. The message is safe to show the student."""


def is_wav(data: bytes) -> bool:
    return len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _asr_post(audio_bytes: bytes, mode: str = "PUSH_TO_TALK") -> dict[str, Any]:
    """POST /asr/transcribe: multipart with a JSON `request` part and a WAV `audio` part."""
    import httpx

    basic = {"model": VOICE_MODEL, "audioEncoding": "WAV", "mode": mode}

    def post(settings: dict[str, Any]):
        return httpx.post(
            base_url().rstrip("/") + "/asr/transcribe",
            headers={"Authorization": f"Bearer {api_key()}", "Accept": "application/json"},
            files={"request": (None, json.dumps(settings), "application/json"),
                   "audio": ("command.wav", audio_bytes, "audio/wav")},
            timeout=30.0,
        )

    resp = post({**basic, "keywords": VOICE_KEYWORDS, "languageBias": ["English"]})
    if resp.status_code in (400, 422):
        # The optional biasing fields are the likeliest thing to be refused: try once with the bare request.
        log.warning("Meta transcription HTTP %s with keywords; retrying without them", resp.status_code)
        resp = post(basic)
    if resp.status_code != 200:
        try:
            err = resp.json()
            err = err.get("error", err) if isinstance(err, dict) else {}
            msg = str(err.get("message") or err.get("type") or "")[:120]
        except Exception:
            msg = ""
        log.warning("Meta transcription HTTP %s: %s", resp.status_code, msg)
        raise TranscribeError(f"Meta voice error {resp.status_code}" + (f": {msg}" if msg else ""))
    return resp.json()


def transcribe(audio_bytes: bytes, filename: str = "command.wav") -> str:
    return transcribe_full(audio_bytes, filename)["text"]


def _asr_text(body: dict[str, Any]) -> str:
    text = body.get("transcript") or " ".join(t.get("transcript", "") for t in body.get("turns") or [])
    return (text or "").strip()


def transcribe_full(audio_bytes: bytes, filename: str = "command.wav") -> dict[str, Any]:
    """Muse Voice Transcribe on a 16 or 24 kHz mono 16-bit WAV (the browser converts).

    Raises OfflineError offline or when the call can't be made (caller returns 503),
    ValueError when the audio is not WAV, TranscribeError when Meta rejects it.
    """
    if not audio_bytes:
        raise OfflineError("empty audio payload")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise OfflineError("audio payload too large")
    if is_offline():
        raise OfflineError("no MODEL_API_KEY")
    try:
        base_url()
    except ValueError as exc:
        log.warning("Meta client configuration invalid: %s", exc)
        raise OfflineError("service unavailable") from exc
    if not is_wav(audio_bytes):
        raise ValueError("Muse Voice Transcribe needs WAV audio")
    if not circuit_breaker.allow_request():
        log.warning("Meta API circuit breaker is OPEN, skipping transcription")
        raise OfflineError("circuit breaker open")
    if not rate_limiter.allow():
        circuit_breaker.cancel_probe()
        log.warning("Meta API rate limit exceeded, skipping transcription")
        raise OfflineError("rate limit exceeded")
    try:
        body = _asr_post(audio_bytes)
        if not _asr_text(body):
            # Push-to-talk heard nothing: ask once more with Meta's own speech detection.
            log.warning("Meta transcription empty (session %s, %s ms); retrying in ENDPOINTING mode",
                        body.get("sessionId"), body.get("audioDurationMs"))
            body = _asr_post(audio_bytes, "ENDPOINTING")
        circuit_breaker.record_success()
    except TranscribeError:
        circuit_breaker.record_success()               # Meta answered; the audio was the problem
        raise
    except Exception as exc:
        circuit_breaker.record_failure()
        log.warning("Meta transcription failed: %s", type(exc).__name__)
        raise OfflineError("transcription failed") from exc
    text = _asr_text(body)
    if not text:
        log.warning("Meta transcription empty again (session %s, %s ms)", body.get("sessionId"), body.get("audioDurationMs"))
    return {"text": text, "session": body.get("sessionId"), "audio_ms": body.get("audioDurationMs")}
