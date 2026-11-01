"""Decision router (server/router.py): JSON parsing, code-enforced caps and the fallback chain.

No network: the router's OpenAI client is replaced by a fake that returns canned replies.
"""

from types import SimpleNamespace
import json

from fastapi.testclient import TestClient
import pytest

from server import meta_client, router, skills
from server.app import app
from server.meta_client import CircuitBreaker

client = TestClient(app)
WRONG_PAGE = ["∫ x cos x dx", "= x sin x - ∫ sin x dx", "= x sin x - cos x + C"]  # L3 sign slip
GOOD_PAGE = ["∫ x^2 dx", "= x^3/3 + C"]


def lines(*texts):
    return [{"id": f"L{i}", "text": t, "bbox": [40, 40 + 60 * i, 300, 50]} for i, t in enumerate(texts, 1)]


def decision(intent="hint", tools=("give_hint",), level=1, conf=0.9, **extra):
    return json.dumps({"intent": intent, "tools": list(tools), "hint_level": level, "confidence": conf, **extra})


class FakeRouter:
    """Stands in for the OpenAI client of the router endpoint."""

    def __init__(self, reply="", fail=None):
        self.reply, self.fail, self.calls, self.kwargs = reply, fail, 0, None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if self.fail:
            raise self.fail
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


@pytest.fixture
def router_on(monkeypatch):
    """Router configured (Spark still offline); returns a function that installs a fake reply."""
    monkeypatch.setenv("ROUTER_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("ROUTER_MODEL", "muse-glimmer-30b")

    def install(reply="", fail=None):
        fake = FakeRouter(reply, fail)
        monkeypatch.setattr(router, "_client", lambda ep: fake)
        return fake

    return install


def no_spark(monkeypatch):
    """Spark is online but must never be called."""
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setattr(meta_client, "run_agent", lambda *a, **k: pytest.fail("Spark was called"))
    monkeypatch.setattr(meta_client, "phrase_hint", lambda verdict, line, level, fallback=None: fallback)


# --------------------------------------------------------------------------- parsing


def test_parse_valid_decision():
    d = router.parse_decision(decision("hint", ["give_hint"], 2, 0.91))
    assert d == router.Decision("hint", ("give_hint",), 2, 0.91)
    assert router.is_strict_json(decision())


@pytest.mark.parametrize("text", [
    "```json\n" + decision() + "\n```",
    "Sure! Here is the decision: " + decision() + " Hope that helps.",
])
def test_parse_tolerates_fences_and_prose_but_not_strict(text):
    assert router.parse_decision(text) == router.Decision("hint", ("give_hint",), 1, 0.9)
    assert not router.is_strict_json(text)


def test_parse_finds_object_after_bracketed_prose():
    assert router.parse_decision("[decision] " + decision("check", ["check_steps"])).intent == "check"


def test_bad_min_confidence_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ROUTER_MIN_CONFIDENCE", "high")
    assert router.min_confidence() == 0.6


def test_parse_ignores_extra_keys_and_duplicate_tools():
    d = router.parse_decision(decision(tools=["give_hint", "give_hint"], why="stuck"))
    assert d.tools == ("give_hint",)


def test_parse_other_intent_may_have_no_tools():
    assert router.parse_decision(decision("other", [], 1, 0.8)).tools == ()


@pytest.mark.parametrize("text", [
    None, "", "   ", "give_hint", "[]", "not json at all", '{"intent": "hint"',
    decision(intent="explain"),                         # unknown intent
    decision(tools=["write_answer"]),                   # unknown tool
    decision(tools="give_hint"),                        # tools must be a list
    decision(tools=[]),                                 # empty tools only for "other"
    decision(tools=["give_hint", "check_steps", "sketch_graph"]),  # more than two tools
    decision(level=0), decision(level=4), decision(level="2"), decision(level=True), decision(level=1.5),
    decision(conf=1.5), decision(conf=-0.1), decision(conf="high"), decision(conf=None),
    json.dumps({"intent": "hint", "tools": ["give_hint"], "hint_level": 1}),   # no confidence
])
def test_parse_rejects_invalid(text):
    assert router.parse_decision(text) is None
    assert not router.is_strict_json(text)


# --------------------------------------------------------------------------- guards


def test_guard_drops_finish_answer_unless_intent_is_answer():
    d, notes = router.apply_guards(router.Decision("hint", ("finish_answer",), 1, 0.9), 3)
    assert d.tools == ("give_hint",) and notes == ["finish_answer dropped"]
    d, _ = router.apply_guards(router.Decision("check", ("check_steps", "finish_answer"), 1, 0.9), 3)
    assert d.tools == ("check_steps",)
    d, notes = router.apply_guards(router.Decision("answer", ("finish_answer",), 1, 0.9), 3)
    assert d.tools == ("finish_answer",) and notes == []


@pytest.mark.parametrize("proposed, cap, expected", [(3, 1, 1), (3, 2, 2), (2, 3, 2), (1, 3, 1), (3, 0, 1), (3, 9, 3)])
def test_guard_caps_hint_level_at_client_level(proposed, cap, expected):
    d, _ = router.apply_guards(router.Decision("hint", ("give_hint",), proposed, 0.9), cap)
    assert d.hint_level == expected


# --------------------------------------------------------------------------- state


def test_state_carries_sympy_verdict_not_line_text():
    box = skills.Toolbox(lines(*WRONG_PAGE))
    state = router.build_state("  help me  ", box.result, router.session_state(1, True, "help"))
    assert state["command"] == "help me"
    assert state["page"] == {"lines": 3, "chain": "integral", "first_wrong": "L3", "first_wrong_index": 2,
                             "error": "sign", "error_type": "computational", "finished": False, "unreadable": 0}
    assert state["session"] == {"hints_given": 1, "wrote_since_hint": True, "last_command": "help"}
    user = router.messages_for(state)[1]["content"]
    assert "cos" not in user and json.loads(user) == state


def test_state_missing_c_is_notational():
    page = router.page_state(skills.Toolbox(lines("∫ x^3 dx", "= x^4/4")).result)
    assert page["first_wrong"] is None and page["first_wrong_index"] == -1
    assert (page["error"], page["error_type"]) == ("missing_c", "notational")


# --------------------------------------------------------------------------- dispatch


def test_router_off_by_default_keeps_current_behaviour(monkeypatch):
    called = []
    monkeypatch.setattr(router, "decide", lambda state: called.append(state))
    body = client.post("/api/assist", json={"lines": lines(*WRONG_PAGE), "command": "help"}).json()
    assert not router.enabled() and called == []
    assert "router" not in body and body["mode"] == "hint"
    assert client.get("/api/health").json() == {"ok": True, "offline": True, "model": "muse-spark-1.3"}


def test_router_decision_runs_one_tool_without_spark(monkeypatch, router_on):
    fake = router_on(decision("check", ["check_steps"], 1, 0.95))
    no_spark(monkeypatch)
    body = client.post("/api/assist", json={"lines": lines(*WRONG_PAGE), "command": "is this right?"}).json()
    assert fake.calls == 1 and fake.kwargs["model"] == "muse-glimmer-30b" and fake.kwargs["temperature"] == 0.0
    assert "response_format" not in fake.kwargs
    assert body["mode"] == "check" and body["first_wrong"] == "L3"
    assert body["router"]["path"] == "router" and body["router"]["state"]["page"]["error"] == "sign"
    assert body["agent_trace"][0]["tool"] == "router"
    assert body["agent_trace"][1]["tool"] == "check_steps"


def test_router_hint_level_is_capped_by_client(monkeypatch, router_on):
    router_on(decision("hint", ["give_hint"], 3, 0.95))
    no_spark(monkeypatch)
    body = client.post("/api/assist", json={"lines": lines(*WRONG_PAGE), "command": "help", "hint_level": 1}).json()
    assert body["router"]["decision"]["hint_level"] == 1
    assert "ghost" not in [a["kind"] for a in body["annotations"]]  # level 3 would ghost the fix
    assert "capped" in body["agent_trace"][0]["summary"]

    router_on(decision("hint", ["give_hint"], 3, 0.95))
    body = client.post("/api/assist", json={"lines": lines(*WRONG_PAGE), "command": "help", "hint_level": 3}).json()
    assert "ghost" in [a["kind"] for a in body["annotations"]]


def test_router_cannot_reveal_answer_without_answer_intent(monkeypatch, router_on):
    router_on(decision("hint", ["finish_answer"], 1, 0.99))
    no_spark(monkeypatch)
    body = client.post("/api/assist", json={"lines": lines("∫ x cos x dx"), "command": "help"}).json()
    assert body["mode"] == "hint"
    assert "write" not in [a["kind"] for a in body["annotations"]]


def test_router_passes_session_and_json_schema(monkeypatch, router_on):
    monkeypatch.setenv("ROUTER_RESPONSE_FORMAT", "json_schema")
    fake = router_on(decision("hint", ["give_hint"], 2, 0.9))
    no_spark(monkeypatch)
    client.post("/api/assist", json={"lines": lines(*WRONG_PAGE), "command": "more help", "hint_level": 3,
                                     "hints_given": 1, "wrote_since_hint": True, "last_command": "help"})
    assert fake.kwargs["response_format"]["json_schema"]["schema"] == router.DECISION_SCHEMA
    state = json.loads(fake.kwargs["messages"][1]["content"])
    assert state["session"] == {"hints_given": 1, "wrote_since_hint": True, "last_command": "help"}


@pytest.mark.parametrize("level, given, wrote", [(1, 0, False), (2, 1, True), (3, 2, True)])
def test_session_defaults_to_the_clients_level(monkeypatch, router_on, level, given, wrote):
    fake = router_on(decision("hint", ["give_hint"], level, 0.9))
    no_spark(monkeypatch)
    body = client.post("/api/assist", json={"lines": lines(*WRONG_PAGE), "command": "help", "hint_level": level}).json()
    state = json.loads(fake.kwargs["messages"][1]["content"])
    assert state["session"] == {"hints_given": given, "wrote_since_hint": wrote, "last_command": ""}
    assert body["router"]["decision"]["hint_level"] == level


@pytest.mark.parametrize("reply, fail", [
    ("I think you want a hint.", None),                           # not JSON
    (decision(level=7), None),                                    # schema violation
    (decision(conf=0.2), None),                                   # below ROUTER_MIN_CONFIDENCE
    (decision("other", [], 1, 0.9), None),                        # not about the page
    ("", TimeoutError("router timed out")),                       # timeout / transport error
])
def test_router_falls_back_to_spark(monkeypatch, router_on, reply, fail):
    router_on(reply, fail)
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    seen = []

    def spark(command, lines_, image, hint_level=1, toolbox=None):
        seen.append(hint_level)
        toolbox.run("give_hint", {})
        return toolbox.output()

    monkeypatch.setattr(meta_client, "run_agent", spark)
    monkeypatch.setattr(meta_client, "phrase_hint", lambda verdict, line, level, fallback=None: fallback)
    body = client.post("/api/assist", json={"lines": lines(*WRONG_PAGE), "command": "help", "hint_level": 2}).json()
    assert seen == [2]  # Spark keeps the client's level
    assert body["router"]["path"] == "spark"
    assert "falling back" in body["agent_trace"][0]["summary"]


def test_router_then_spark_then_keywords(monkeypatch, router_on):
    router_on("garbage")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setattr(meta_client, "run_agent", lambda *a, **k: None)  # Spark's first call failed
    monkeypatch.setattr(meta_client, "phrase_hint", lambda verdict, line, level, fallback=None: fallback)
    body = client.post("/api/assist", json={"lines": lines(*WRONG_PAGE), "command": "graph it"}).json()
    assert body["mode"] == "graph" and body["router"]["path"] == "keywords"
    assert body["agent_trace"][1]["summary"] == "agent unavailable: keywords -> graph"


def test_router_with_spark_offline_goes_to_keywords(router_on):
    router_on("", RuntimeError("connection refused"))
    body = client.post("/api/assist", json={"lines": lines(*WRONG_PAGE), "command": "tidy it up"}).json()
    assert body["offline"] is True and body["mode"] == "typeset"
    assert body["router"]["path"] == "keywords" and body["router"]["decision"] is None
    assert body["agent_trace"][1]["summary"] == "offline: keywords -> typeset"


def test_two_tools_go_to_spark_first(monkeypatch, router_on):
    router_on(decision("check", ["check_steps", "sketch_graph"], 1, 0.9))
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(meta_client, "run_agent", lambda *a, **k: calls.append(1))  # returns None: Spark failed
    monkeypatch.setattr(meta_client, "phrase_hint", lambda verdict, line, level, fallback=None: fallback)
    body = client.post("/api/assist", json={"lines": lines(*GOOD_PAGE), "command": "check it and graph it"}).json()
    assert calls == [1]
    # Spark failed, so the router's own valid two-tool decision runs rather than keywords.
    assert body["router"]["path"] == "router"
    assert [t["tool"] for t in body["agent_trace"]][-2:] == ["check_steps", "sketch_graph"]


def test_router_breaker_is_separate_from_spark(monkeypatch, router_on):
    monkeypatch.setenv("ROUTER_CB_FAILURE_THRESHOLD", "2")
    fake = router_on("", RuntimeError("upstream 500"))
    for _ in range(3):
        client.post("/api/assist", json={"lines": lines(*GOOD_PAGE), "command": "help"})
    assert fake.calls == 2  # third request skipped the router: its breaker is open
    assert router.circuit_breaker.state == CircuitBreaker.OPEN
    assert meta_client.circuit_breaker.state == CircuitBreaker.CLOSED
    assert client.get("/api/health").json()["router"]["circuit_breaker"] == "open"


def test_invalid_replies_trip_the_breaker_and_success_resets(monkeypatch, router_on):
    monkeypatch.setenv("ROUTER_CB_FAILURE_THRESHOLD", "3")
    router_on("nope")
    state = router.build_state("help", skills.Toolbox(lines(*GOOD_PAGE)).result)
    for _ in range(2):
        assert router.decide(state) == (None, "router reply not valid JSON")
    router_on(decision())
    d, _ = router.decide(state)
    assert d is not None and router.circuit_breaker.failure_count == 0


def test_router_rate_limit(monkeypatch, router_on):
    monkeypatch.setenv("ROUTER_RATE_LIMIT_CALLS", "1")
    fake = router_on(decision())
    state = router.build_state("help", skills.Toolbox(lines(*GOOD_PAGE)).result)
    assert router.decide(state)[0] is not None
    assert router.decide(state) == (None, "router rate limit")
    assert fake.calls == 1
    assert meta_client.rate_limiter.allow()  # Spark's limiter untouched


@pytest.mark.parametrize("url", ["http://10.0.0.5:8000/v1", "ftp://example.com", "file:///etc/passwd", "localhost:8000"])
def test_router_rejects_insecure_base_url(monkeypatch, router_on, url):
    fake = router_on(decision())
    monkeypatch.setenv("ROUTER_BASE_URL", url)
    state = router.build_state("help", skills.Toolbox(lines(*GOOD_PAGE)).result)
    assert router.decide(state) == (None, "bad router config")
    assert fake.calls == 0


def test_router_bad_response_format_is_config_error(monkeypatch, router_on):
    router_on(decision())
    monkeypatch.setenv("ROUTER_RESPONSE_FORMAT", "yaml")
    with pytest.raises(ValueError, match="ROUTER_RESPONSE_FORMAT"):
        router.endpoint()


def test_router_needs_both_url_and_model(monkeypatch):
    monkeypatch.setenv("ROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    assert not router.enabled()
    monkeypatch.setenv("ROUTER_MODEL", "meta/muse-glimmer-30b")
    assert router.enabled()
    ep = router.endpoint()
    assert (ep.base_url, ep.model, ep.api_key, ep.response_format) == (
        "https://openrouter.ai/api/v1", "meta/muse-glimmer-30b", "", "none")
