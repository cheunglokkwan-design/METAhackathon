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
INTENTS = ("hint", "answer", "check", "typeset", "graph", "verify")
AGENT_MAX_ITERS = 4


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
    """Offline intent from keywords: typeset / graph / verify / answer / check / hint."""
    c = (command or "").lower()
    if any(k in c for k in ("tidy", "typeset", "neat", "type it up", "clean up", "write it out properly")):
        return "typeset"
    if any(k in c for k in ("graph", "plot", "sketch", "draw the curve", "draw it")):
        return "graph"
    if "check" in c and any(k in c for k in ("differentiat", "derivative")):
        return "verify"  # "check it by differentiating"
    if any(k in c for k in ("answer", "solution", "solve it", "finish", "just give", "show me how", "do it for me")):
        return "answer"
    if any(k in c for k in ("check", "is this right", "am i right", "correct?", "verify")):
        return "check"
    return "hint"


def classify_intent(command: str) -> str:
    """One of INTENTS. Muse Spark when online, keywords otherwise."""
    if not command or is_offline():
        return keyword_intent(command)
    reply = _chat([
        {"role": "system", "content": "Classify a student's request to a math tutor. Reply with exactly one word: "
         "hint (wants help or a nudge), answer (wants the full solution or remaining steps), "
         "check (wants to know whether their work is right), typeset (wants the page tidied or typed up), "
         "graph (wants a graph or sketch), or verify (wants the answer checked by differentiating)."},
        {"role": "user", "content": command},
    ], max_tokens=5)
    word = (reply or "").strip().lower().strip(".!\"'")
    return word if word in INTENTS else keyword_intent(command)


# --------------------------------------------------------------------------- agent loop

AGENT_PROMPT = """You are Margin, a calculus tutor that writes tiny notes in ink on a student's notebook page.
You act ONLY through tools. Every tool runs a SymPy referee on the page and writes the ink itself;
you never write mathematics, answers or hints yourself.
Pick the tool(s) that match the request, usually exactly one:
- help / stuck / hint / what next -> give_hint
- is this right / check -> check_steps
- just give me the answer / finish it -> finish_answer
- tidy / typeset / neat -> typeset_lines
- graph / plot / sketch -> sketch_graph
- check by differentiating -> differentiate_check
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
    client = _client()
    if client is None:
        return None
    from .skills import TOOL_SPECS, Toolbox  # local import: skills imports this module

    box = toolbox or Toolbox(lines, hint_level)
    user: list[dict[str, Any]] = [{"type": "text", "text": f"Student said: {command or '(nothing)'}\n"
                                   f"Page lines:\n{_page_text(lines) or '(none)'}"}]
    if image:
        user.append({"type": "image_url", "image_url": {"url": image}})
    messages: list[dict[str, Any]] = [{"role": "system", "content": AGENT_PROMPT},
                                      {"role": "user", "content": user}]
    ran = 0
    for it in range(AGENT_MAX_ITERS):
        try:
            resp = client.chat.completions.create(
                model=CHAT_MODEL, messages=messages, tools=TOOL_SPECS, tool_choice="auto",
                max_tokens=300, temperature=0.2)
            msg = resp.choices[0].message
        except Exception as exc:
            log.warning("agent call %d failed: %s", it + 1, exc)
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
    box.route(intent, f"model replied in text; keywords -> {intent}")


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
