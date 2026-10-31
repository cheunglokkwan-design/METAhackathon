"""New skills: derivatives, definite integrals, typeset, graph, offline routing, agent loop."""
import json
import math
from types import SimpleNamespace as NS

import pytest
import sympy as sp
from fastapi.testclient import TestClient

from server import meta_client
from server.app import app
from server.checker import check_lines, next_step_hints, parse_line, solve_remaining
from server.render import graph_for, typeset_line
from server.skills import TOOL_SPECS, Toolbox

x = sp.Symbol("x")
client = TestClient(app)


def lines(*texts):
    return [{"id": f"L{i}", "text": t, "bbox": [40, 40 + 60 * i, 300, 50]} for i, t in enumerate(texts, 1)]


def assist(*texts, **kw):
    return client.post("/api/assist", json={"lines": lines(*texts), **kw}).json()


# --------------------------------------------------------------------------- derivatives


@pytest.mark.parametrize("text", [
    "d/dx (x^2 sin x)", "d/dx(x^2 sin x)", "d/dx [x^2 sin x]", "(x^2 sin x)'", "\\frac{d}{dx} x^2 sin x",
])
def test_derivative_notations_parse(text):
    p = parse_line(text)
    assert p.ok, p.error
    assert p.parts[0] == sp.Derivative(x**2 * sp.sin(x), x)


def test_prime_notation_uses_definition_line():
    res = check_lines(["f(x) = x^2 sin x", "f'(x) = 2x sin x + x^2 cos x"])
    assert [l.status for l in res.lines] == ["start", "ok"]
    assert res.chain == "derivative" and res.finished
    res = check_lines(["y = sin(3x)", "dy/dx = 3cos(3x)"])
    assert res.first_wrong is None and res.finished


def test_intermediate_product_rule_line_is_checked():
    res = check_lines(["d/dx (x^2 sin x)", "= (x^2)' sin x + x^2 (sin x)'", "= 2x sin x + x^2 cos x"])
    assert [l.status for l in res.lines] == ["start", "ok", "ok"]


@pytest.mark.parametrize("problem", [
    "d/dx (x^2 sin x)", "d/dx sin(x^2)", "(x e^x)'", "d/dx (3x^2 cos x)", "d/dx (x^2+1)^5", "d/dx ln(2x)",
])
def test_derivative_answer_verifies(problem):
    steps = solve_remaining([problem])
    assert steps
    f = parse_line(problem).parts[0].expr
    assert sp.simplify(parse_line(steps[-1]).parts[0] - sp.diff(f, x)) == 0
    res = check_lines([problem] + steps)
    assert res.first_wrong is None and res.finished


def test_derivative_nudges_name_the_rule():
    assert next_step_hints(check_lines(["d/dx (x^2 sin x)"]))[1] == "Try the product rule"
    assert next_step_hints(check_lines(["d/dx sin(x^2)"]))[1] == "Use the chain rule"
    assert next_step_hints(check_lines(["d/dx (x^2 + sin x)"]))[1] == "Differentiate term by term"


def test_derivative_answer_mode_via_api():
    body = assist("d/dx (x^2 sin x)", command="just give me the answer")
    writes = [a["text"] for a in body["annotations"] if a["kind"] == "write"]
    assert writes == ["= (x^2)' sin x + x^2 (sin x)'", "= x^2 cos x + 2x sin x"]
    assert not any("+ C" in w for w in writes)


# --------------------------------------------------------------------------- definite integrals


@pytest.mark.parametrize("text", ["∫_0^1 x^2 dx", "∫_{0}^{1} x^2 dx", "int from 0 to 1 x^2 dx",
                                  "\\int_0^1 x^2 dx", "integral from 0 to 1 x^2 dx"])
def test_definite_integral_parses(text):
    p = parse_line(text)
    assert p.ok, p.error
    assert p.parts[0] == sp.Integral(x**2, (x, 0, 1))


def test_definite_chain_checks_bracket_and_number():
    res = check_lines(["∫_0^1 x^2 dx", "= [x^3/3]_0^1", "= 1/3"])
    assert [l.status for l in res.lines] == ["start", "ok", "ok"]
    assert res.finished and res.lines[-1].note is None  # no "+ C" on a definite integral
    assert check_lines(["∫_0^π sin x dx", "= -cos x |_0^π", "= 2"]).first_wrong is None


def test_definite_wrong_final_number():
    res = check_lines(["∫_0^1 x^2 dx", "= [x^3/3]_0^1", "= 1/2"])
    assert res.first_wrong == "L3"
    assert res.wrong.diagnosis.kind == "arithmetic"
    assert res.wrong.diagnosis.fix == "1/3"


def test_definite_bracket_wrong_even_if_number_matches():
    # The number alone can match by luck, so the antiderivative inside the bracket is checked too.
    res = check_lines(["∫_0^1 2x dx", "= [x^2 + x - x]_0^1", "= 1"])
    assert res.first_wrong is None
    res = check_lines(["∫_0^1 2x dx", "= [x]_0^1"])  # F' = 1 != 2x, value 1 == 1
    assert res.first_wrong == "L2" and res.wrong.diagnosis.kind == "antiderivative"


def test_definite_answer_steps_and_nudge():
    assert solve_remaining(["∫_0^1 x^2 dx"]) == ["= [x^3/3]_0^1", "= 1/3"]
    assert solve_remaining(["∫_0^1 x^2 dx", "= [x^3/3]_0^1"]) == ["= 1/3"]
    assert next_step_hints(check_lines(["∫_0^1 x^2 dx"]))[1] == "Find an antiderivative first"
    assert next_step_hints(check_lines(["∫_0^1 x^2 dx", "= [x^3/3]_0^1"]))[1] == "Now plug in the limits"


# --------------------------------------------------------------------------- typeset


@pytest.mark.parametrize("text, pretty", [
    ("∫ x cos x dx", "∫ x·cos x dx"),
    ("= x sin x - ∫ sin x dx", "= x·sin x − ∫ sin x dx"),
    ("∫ xcosx dx", "∫ x·cos x dx"),
    ("int x^2dx", "∫ x² dx"),
    ("∫_0^1 x^2 dx = [x^3/3]_0^1 = 1/3", "∫₀¹ x² dx = [x³/3]₀¹ = 1/3"),
    ("= e^(x^2) + C", "= eˣ² + C"),
    ("= 2x - x^3/3", "= 2x − x³/3"),
])
def test_typeset_text(text, pretty):
    out = typeset_line(parse_line(text))
    assert out[0] == pretty
    assert out[1]  # LaTeX from SymPy


def test_typeset_skips_unreadable_lines_via_api():
    body = assist("∫ x^2 dx", "= ))((", "= x^3/3 + C", command="tidy this up")
    assert body["mode"] == "typeset"
    ts = [a for a in body["annotations"] if a["kind"] == "typeset"]
    assert [a["target"] for a in ts] == ["L1", "L3"]
    assert ts[0]["text"] == "∫ x² dx" and ts[0]["latex"].startswith("\\int")
    assert body["agent_trace"][-1]["tool"] == "typeset_lines"


# --------------------------------------------------------------------------- graph


def _finite(pts):
    return all(math.isfinite(a) and math.isfinite(b) for a, b in pts)


def test_graph_of_finished_antiderivative_has_integrand_overlay():
    body = assist("∫ x cos x dx", "= x sin x + cos x + C", command="graph it")
    assert body["mode"] == "graph"
    g = next(a for a in body["annotations"] if a["kind"] == "graph")
    assert g["after"] == "L2" and g["fn"] == "x sin x + cos x" and g["overlay_fn"] == "x cos x"
    assert 100 <= len(g["points"]) <= 120 and _finite(g["points"]) and _finite(g["overlay"])
    lo, hi = g["xrange"]
    assert lo < 0 < hi and g["yrange"][0] < g["yrange"][1]
    x0, y0 = g["points"][60]
    assert abs(y0 - (x0 * math.sin(x0) + math.cos(x0))) < 1e-3


def test_graph_unfinished_shows_only_integrand():
    g = graph_for(check_lines(["∫ x cos x dx"]))
    assert g["fn"] == "x cos x" and "overlay" not in g
    g = graph_for(check_lines(["∫ x cos x dx"]), reveal=True)
    assert g["fn"] == "x sin x + cos x" and g["overlay_fn"] == "x cos x"


def test_graph_skips_non_finite_points():
    g = graph_for(check_lines(["∫ 1/x dx"]), reveal=True)
    assert g["fn"] == "ln|x|" or g["fn"] == "ln x"
    assert _finite(g["points"]) and _finite(g["overlay"]) and len(g["points"]) < 120


def test_graph_definite_has_shade_and_derivative_overlay():
    g = graph_for(check_lines(["∫_0^1 x^2 dx"]))
    assert g["shade"] == [0.0, 1.0] and g["xrange"][0] < 0 and g["xrange"][1] > 1
    g = graph_for(check_lines(["d/dx (x^2 sin x)", "= 2x sin x + x^2 cos x"]))
    assert g["fn"] == "x^2 sin x" and "overlay" in g


def test_graph_nothing_to_draw_is_a_note():
    body = assist("1 + 1 = 2", command="plot this")
    assert body["annotations"][0]["kind"] == "note"


# --------------------------------------------------------------------------- offline routing


@pytest.mark.parametrize("command, intent", [
    ("hey meta help me", "hint"), ("tidy this up", "typeset"), ("make it neat", "typeset"),
    ("can you typeset it", "typeset"), ("graph it", "graph"), ("plot the answer", "graph"),
    ("sketch this", "graph"), ("just give me the answer", "answer"), ("is this right?", "check"),
    ("check it by differentiating", "verify"), ("help me differentiate this", "hint"),
])
def test_keyword_intent(command, intent):
    assert meta_client.keyword_intent(command) == intent


def test_offline_trace_and_explicit_mode():
    body = assist("∫ x cos x dx", "= x sin x + ∫ sin x dx", command="help")
    assert body["agent_trace"][0] == {"tool": "route", "summary": "offline: keywords -> hint"}
    assert body["agent_trace"][1]["tool"] == "give_hint"
    body = assist("∫ x^2 dx", mode="typeset")
    assert body["mode"] == "typeset" and body["annotations"][0]["kind"] == "typeset"


def test_differentiate_check_tool():
    box = Toolbox(lines("∫ x cos x dx", "= x sin x - cos x + C"))
    box.run("differentiate_check", {})
    note = next(a for a in box.annotations if a["kind"] == "note")
    assert note["target"] == "L2" and "not x cos x" in note["text"]


# --------------------------------------------------------------------------- agent loop (mocked)


def _resp(content=None, calls=()):
    tool_calls = [NS(id=f"call_{i}", type="function", function=NS(name=n, arguments=json.dumps(a)))
                  for i, (n, a) in enumerate(calls)] or None
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


class FakeClient:
    """Scripted Muse Spark: agent calls pop the script; phrase_hint calls get a short text."""

    def __init__(self, script):
        self.script = list(script)
        self.agent_calls = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        if "tools" not in kw:
            return _resp("Look closely at this sign")
        self.agent_calls.append(json.loads(json.dumps(kw, default=str)))
        if not self.script:
            return _resp("DONE")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-key")

    def install(script):
        fake = FakeClient(script)
        monkeypatch.setattr(meta_client, "_client", lambda: fake)
        return fake
    return install


PAGE = lines("∫ x cos x dx", "= x sin x - ∫ sin x dx", "= x sin x - cos x + C")


def test_run_agent_tool_loop(online):
    fake = online([_resp(None, [("give_hint", {})]), _resp("DONE")])
    out = meta_client.run_agent("help me", PAGE, None)
    assert out["mode"] == "hint"
    assert {"kind": "circle", "target": "L3"} in out["annotations"]
    assert [t["tool"] for t in out["agent_trace"]] == ["give_hint"]
    first, second = fake.agent_calls
    assert first["tool_choice"] == "auto" and first["model"] == "muse-spark-1.3"
    assert {t["function"]["name"] for t in first["tools"]} == {t["function"]["name"] for t in TOOL_SPECS}
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_0"
    assert "L3 wrong" in json.loads(tool_msg["content"])["summary"]
    assert second["messages"][-2]["tool_calls"][0]["function"]["name"] == "give_hint"


def test_run_agent_two_tools_and_model_text_never_inked(online):
    online([_resp("= 42 is the answer", [("check_steps", {}), ("sketch_graph", {"show_answer": False})]),
            _resp("The answer is x sin x + cos x + C")])
    out = meta_client.run_agent("check it and graph it", PAGE, None)
    kinds = [a["kind"] for a in out["annotations"]]
    assert "graph" in kinds and "circle" in kinds and out["mode"] == "check"
    assert not any("42" in str(a.get("text", "")) for a in out["annotations"])
    assert not any(a["kind"] == "write" for a in out["annotations"])


def test_run_agent_plain_text_reply_routes(online):
    online([_resp("Sure! I'll draw that for you.")])
    out = meta_client.run_agent("graph it", PAGE, None)
    assert out["mode"] == "graph"
    assert out["agent_trace"][0]["summary"].startswith("model replied in text")
    online([_resp("I would call typeset_lines here.")])
    out = meta_client.run_agent("whatever", PAGE, None)
    assert out["mode"] == "typeset"


def test_run_agent_stops_after_four_iterations(online):
    fake = online([_resp(None, [("check_steps", {})]) for _ in range(6)])
    out = meta_client.run_agent("check", PAGE, None)
    assert len(fake.agent_calls) == 4
    assert out["annotations"].count({"kind": "circle", "target": "L3"}) == 1  # deduplicated


def test_run_agent_unknown_tool_and_bad_args(online):
    bad = NS(choices=[NS(message=NS(content=None, tool_calls=[
        NS(id="a", type="function", function=NS(name="launch_rockets", arguments="{}")),
        NS(id="b", type="function", function=NS(name="give_hint", arguments="{not json")),
    ]))])
    online([bad, _resp("DONE")])
    out = meta_client.run_agent("help", PAGE, None)
    assert [t["tool"] for t in out["agent_trace"]] == ["give_hint"]


def test_api_online_uses_agent_and_falls_back(online):
    online([_resp(None, [("finish_answer", {})]), _resp("DONE")])
    body = client.post("/api/assist", json={"lines": lines("∫ x cos x dx"), "command": "do it"}).json()
    assert body["offline"] is False and body["mode"] == "answer"
    assert [a["text"] for a in body["annotations"] if a["kind"] == "write"][-1] == "= x sin x + cos x + C"
    assert body["agent_trace"][0]["tool"] == "finish_answer"

    online([RuntimeError("503 from api")])
    body = client.post("/api/assist", json={"lines": lines("∫ x cos x dx"), "command": "graph it"}).json()
    assert body["mode"] == "graph"
    assert body["agent_trace"][0]["summary"] == "agent unavailable: keywords -> graph"
