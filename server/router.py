"""Decision router: one short call to a small model (Muse Glimmer) decides what Margin does.

Off unless ROUTER_BASE_URL and ROUTER_MODEL are set, so nothing changes by default. When on,
/api/assist in mode "auto" asks the router before Muse Spark. SymPy has already checked the
page by then and its verdict is part of the router's input: the router never judges
correctness. One call returns strict JSON

    {"intent": "hint", "tools": ["give_hint"], "hint_level": 2, "confidence": 0.91}

which replaces classify_intent and the tool-picking step of run_agent for single-tool
requests. Code, not the model, enforces the rules that matter: finish_answer only when the
intent is "answer", and the hint level is capped at the level the client sent.

Fallback order: router -> Muse Spark run_agent -> keyword_intent. The router has its own
rate limiter and circuit breaker (ROUTER_RATE_LIMIT_*, ROUTER_CB_*), so a router outage
never trips Spark's breaker. Any OpenAI-compatible endpoint works: OpenRouter
(meta/muse-glimmer-30b), a Fireworks deployment, or a local vLLM / llama.cpp / Ollama server.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import logging
import os
import re
import time
from typing import Any

from . import checker, meta_client, skills
from .meta_client import CircuitBreaker, RateLimiter

log = logging.getLogger("margin.router")

# The six tutoring intents plus "other": a command that is not about the page.
INTENTS = meta_client.INTENTS + ("other",)
TOOLS = tuple(skills.TOOL_MODE)
MAX_TOOLS = 2

rate_limiter = RateLimiter(env_prefix="ROUTER")
circuit_breaker = CircuitBreaker(env_prefix="ROUTER")

# SymPy diagnosis kind -> FERMAT error dimension (computational, conceptual, notational,
# presentation). "missing_c" and unreadable lines are added by page_state().
ERROR_TYPE = {
    "sign": "computational", "trig_sign": "computational", "arithmetic": "computational",
    "factor": "computational", "not_equal": "unclassified",
    "power_rule": "conceptual", "chain_rule": "conceptual", "product_rule": "conceptual",
    "differentiated": "conceptual", "integrated": "conceptual", "antiderivative": "conceptual",
    "limits": "conceptual", "no_limits": "conceptual",
}

SYSTEM_PROMPT = """You are the decision router of Margin, a calculus notebook that writes ink on the student's page.
SymPy has already checked the page; its verdict is in "page" (first_wrong is the first wrong line or null,
error is the kind of mistake). Never judge the maths yourself. From the student's command, the verdict and
the session, decide what Margin does next. Reply with one JSON object and nothing else:
{"intent": I, "tools": [T], "hint_level": L, "confidence": C}
I: hint (wants help, is stuck, asks what next), answer (wants the solution or the remaining steps),
check (asks whether the work is right), typeset (tidy or type up the page), graph (plot or sketch),
verify (check the answer by differentiating), other (not a request about the page).
T: give_hint, finish_answer, check_steps, typeset_lines, sketch_graph, differentiate_check. One tool;
two only when the command asks for two things, in the order asked. [] when I is other.
An empty command means help with the page: hint, or check when the work is finished and correct.
L: 1 nudge, 2 name the rule, 3 show the fix. Start at 1. Go one level above hints_given only when the
student wrote something since the last hint or asks for more help; otherwise repeat hints_given. Never
above 3. Use 1 when no hint is given.
C: your confidence from 0 to 1.
finish_answer only when the student asks for the answer."""

# JSON schema for providers that support response_format json_schema (vLLM, SGLang, OpenRouter).
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "tools": {"type": "array", "items": {"type": "string", "enum": list(TOOLS)}, "maxItems": MAX_TOOLS},
        "hint_level": {"type": "integer", "minimum": 1, "maximum": 3},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["intent", "tools", "hint_level", "confidence"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- decision


@dataclass(frozen=True)
class Decision:
    intent: str
    tools: tuple[str, ...]
    hint_level: int
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tools"] = list(self.tools)
        return d


def validate(data: Any) -> Decision | None:
    """A Decision if `data` matches the schema exactly, else None (extra keys are ignored)."""
    if not isinstance(data, dict):
        return None
    intent, tools = data.get("intent"), data.get("tools")
    level, conf = data.get("hint_level"), data.get("confidence")
    if intent not in INTENTS or not isinstance(tools, list):
        return None
    if not all(isinstance(t, str) and t in TOOLS for t in tools):
        return None
    tools = list(dict.fromkeys(tools))
    if len(tools) > MAX_TOOLS or (not tools and intent != "other"):
        return None
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 3:
        return None
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        return None
    return Decision(intent, tuple(tools), level, float(conf))


def parse_decision(text: str | None) -> Decision | None:
    """Decision from a model reply; tolerates ``` fences and prose around the JSON."""
    if not text or not text.strip():
        return None
    try:
        data = meta_client._extract_json(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):  # e.g. "[note] {...}": the shared parser grabbed the brackets
        m = re.search(r"\{.*\}", text, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except ValueError:
            return None
    return validate(data)


def is_strict_json(text: str | None) -> bool:
    """True if the whole reply is one schema-valid JSON object (no fences, no prose)."""
    try:
        return validate(json.loads(text or "")) is not None
    except ValueError:
        return False


def apply_guards(d: Decision, max_level: int) -> tuple[Decision, list[str]]:
    """Rules enforced in code: finish_answer only for intent "answer"; hint level <= max_level."""
    notes: list[str] = []
    tools = list(d.tools)
    if "finish_answer" in tools and d.intent != "answer":
        tools.remove("finish_answer")
        notes.append("finish_answer dropped")
        if not tools:
            tools = [skills.INTENT_TOOL.get(d.intent, "give_hint")]
    cap = max(1, min(3, int(max_level or 1)))
    level = min(d.hint_level, cap)
    if level != d.hint_level:
        notes.append(f"level {d.hint_level} capped to {level}")
    return replace(d, tools=tuple(tools), hint_level=level), notes


# --------------------------------------------------------------------------- state


def page_state(result: checker.CheckResult) -> dict[str, Any]:
    """Compact SymPy verdict for the router: no line text, only what the decision needs."""
    wrong = result.wrong
    kind = wrong.diagnosis.kind if wrong is not None and wrong.diagnosis is not None else None
    last = result.lines[-1] if result.lines else None
    if kind is None and last is not None and last.note == "Don't forget + C":
        kind = "missing_c"
    unreadable = sum(1 for r in result.lines if not r.parsed.ok)
    error_type = ERROR_TYPE.get(kind, "notational" if kind == "missing_c" else None)
    if error_type is None and unreadable:
        error_type = "presentation"
    ids = [r.id for r in result.lines]
    return {
        "lines": len(result.lines),
        "chain": result.chain,
        "first_wrong": result.first_wrong,
        "first_wrong_index": ids.index(result.first_wrong) if result.first_wrong in ids else -1,
        "error": kind,
        "error_type": error_type,
        "finished": bool(result.finished),
        "unreadable": unreadable,
    }


def session_state(hints_given: int = 0, wrote_since_hint: bool = False, last_command: str = "") -> dict[str, Any]:
    """What the client knows about this line's history (all optional in /api/assist)."""
    return {"hints_given": max(0, min(20, int(hints_given or 0))), "wrote_since_hint": bool(wrote_since_hint),
            "last_command": (last_command or "").strip()[:200]}


def session_from_level(level: int) -> dict[str, Any]:
    """Session for a client that sends only hint_level (today's web page).

    That page already raises the level when the student asks again about the same line, so
    level L reads as L-1 hints given and a new request for more: the router's rule then
    proposes L again, and nothing changes until the client sends the real history.
    """
    level = max(1, min(3, int(level or 1)))
    return session_state(level - 1, level > 1)


def build_state(command: str, result: checker.CheckResult, session: dict[str, Any] | None = None) -> dict[str, Any]:
    """The router's whole input. The same object is logged, so live sessions can become training rows."""
    return {"command": (command or "").strip()[:200], "page": page_state(result),
            "session": session if session is not None else session_state()}


def messages_for(state: dict[str, Any]) -> list[dict[str, str]]:
    """Chat messages for one decision (used by the server, the data builder and the eval)."""
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(state, ensure_ascii=False, separators=(",", ":"))}]


# --------------------------------------------------------------------------- endpoint


@dataclass(frozen=True)
class Endpoint:
    """One OpenAI-compatible chat endpoint (the router, or Spark for the eval baseline)."""

    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 5.0
    max_tokens: int = 300
    response_format: str = "none"  # none | json_object | json_schema
    temperature: float | None = 0.0

    def client(self):
        from openai import OpenAI

        # Local servers (vLLM, llama.cpp) take any key; the SDK insists on a non-empty one.
        return OpenAI(base_url=self.base_url, api_key=self.api_key or "EMPTY", timeout=self.timeout, max_retries=0)

    def complete(self, messages: list[dict[str, Any]], client: Any = None) -> str:
        """Raw reply text. Raises on any transport or API error."""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.response_format == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        elif self.response_format == "json_schema":
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "margin_decision", "schema": DECISION_SCHEMA, "strict": True}}
        resp = (client or self.client()).chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()


def enabled() -> bool:
    return bool(os.getenv("ROUTER_BASE_URL", "").strip() and os.getenv("ROUTER_MODEL", "").strip())


def endpoint() -> Endpoint:
    """The router endpoint from ROUTER_* env vars. Raises ValueError on a bad URL or format."""
    fmt = os.getenv("ROUTER_RESPONSE_FORMAT", "none").strip().lower() or "none"
    if fmt not in ("none", "json_object", "json_schema"):
        raise ValueError("ROUTER_RESPONSE_FORMAT must be none, json_object or json_schema")
    return Endpoint(
        base_url=meta_client.check_base_url(os.getenv("ROUTER_BASE_URL", "").strip(), "ROUTER_BASE_URL"),
        model=os.getenv("ROUTER_MODEL", "").strip(),
        api_key=os.getenv("ROUTER_API_KEY", "").strip(),
        timeout=float(os.getenv("ROUTER_TIMEOUT", "5.0")),
        max_tokens=int(os.getenv("ROUTER_MAX_TOKENS", "300")),
        response_format=fmt,
    )


def min_confidence() -> float:
    try:
        return float(os.getenv("ROUTER_MIN_CONFIDENCE", "0.6"))
    except ValueError:
        log.warning("ROUTER_MIN_CONFIDENCE is not a number; using 0.6")
        return 0.6


def _client(ep: Endpoint):
    """OpenAI SDK client for the router (a seam for tests)."""
    return ep.client()


def decide(state: dict[str, Any]) -> tuple[Decision | None, str]:
    """(decision, reason). A None decision means: fall back to the next path."""
    if not enabled():
        return None, "router off"
    try:
        ep = endpoint()
        client = _client(ep)
    except Exception as exc:
        log.warning("router configuration invalid: %s", exc)
        return None, "bad router config"
    if not circuit_breaker.allow_request():
        return None, "router paused after errors"
    if not rate_limiter.allow():
        circuit_breaker.cancel_probe()
        return None, "router rate limit"
    t0 = time.monotonic()
    try:
        text = ep.complete(messages_for(state), client)
    except Exception as exc:
        circuit_breaker.record_failure()
        log.warning("router call failed: %s", type(exc).__name__)
        return None, f"router error {type(exc).__name__}"
    ms = (time.monotonic() - t0) * 1000.0
    decision = parse_decision(text)
    if decision is None:
        # The endpoint answered but the model keeps breaking the schema: count it, so a
        # misbehaving model stops costing a round trip on every request.
        circuit_breaker.record_failure()
        log.warning("router reply is not a valid decision: %r", (text or "")[:200])
        return None, "router reply not valid JSON"
    circuit_breaker.record_success()
    log.info("router decision %s in %.0f ms", decision.as_dict(), ms)
    return decision, f"{ms:.0f} ms"


# --------------------------------------------------------------------------- dispatch


def _execute(box: skills.Toolbox, d: Decision, notes: list[str]) -> dict[str, Any]:
    """Run the decided tool(s) at the decided (already capped) hint level."""
    box.level = d.hint_level
    extra = f"; {', '.join(notes)}" if notes else ""
    box.trace.append({"tool": "router", "summary": (
        f"{d.intent} -> {'+'.join(d.tools)}, level {d.hint_level}, conf {d.confidence:.2f}{extra}")[:90]})
    for tool in d.tools:
        box.run(tool, {})
    return box.output()


def route(command: str, box: skills.Toolbox, image: str | None = None,
          session: dict[str, Any] | None = None, spark: bool = True) -> dict[str, Any]:
    """Mode "auto" with the router on: router, then Muse Spark's agent loop, then keywords.

    `box.level` is the client's hint level; the router's level is capped at it. Always returns
    a Toolbox output with an extra "router" entry {state, path, decision} for the log.
    """
    state = build_state(command, box.result, session)
    decision, why = decide(state)
    pending: tuple[Decision, list[str]] | None = None  # a valid two-tool decision, if Spark fails
    if decision is not None:
        guarded, notes = apply_guards(decision, box.level)
        if guarded.intent == "other" or not guarded.tools:
            why = "not a request about the page"
        elif guarded.confidence < min_confidence():
            why = f"low confidence {guarded.confidence:.2f}"
        elif len(guarded.tools) > 1:
            why, pending = "two tools: Spark plans those", (guarded, notes)
        else:
            out = _execute(box, guarded, notes)
            out["router"] = {"state": state, "path": "router", "decision": guarded.as_dict()}
            return out
    box.trace.append({"tool": "router", "summary": f"{why}; falling back"[:90]})
    out, path = None, "spark"
    if spark:
        out = meta_client.run_agent(command, box.lines, image, hint_level=box.level, toolbox=box)
    if out is None and pending is not None:
        out, path = _execute(box, *pending), "router"
    if out is None:
        intent = meta_client.keyword_intent(command)
        out = box.route(intent, f"{'agent unavailable' if spark else 'offline'}: keywords -> {intent}")
        path = "keywords"
    out["router"] = {"state": state, "path": path,
                     "decision": decision.as_dict() if decision is not None else None}
    return out
