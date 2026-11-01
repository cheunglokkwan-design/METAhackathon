"""Router data builder and eval (scripts/build_router_data.py, scripts/eval_router.py). No network."""

import json
import random

import pytest
import sympy as sp

from scripts import build_router_data as b
from scripts import eval_router as ev
from server import router

x = b.X


def seed(kind, f, limits=None):
    return b.Seed("t", "synthetic", kind, f, limits)


def page_errors(s):
    return {p.mistake: p for p in b.pages_for(s, random.Random(0))}


# --------------------------------------------------------------------------- chains & mistakes


@pytest.mark.parametrize("s, mistake, error, line", [
    (seed("integral", x * sp.cos(x)), "sign_flip", "sign", "L3"),
    (seed("integral", x * sp.cos(x)), "ibp_sign", "sign", "L2"),
    (seed("integral", x * sp.cos(x)), "ibp_wrong_term", "not_equal", "L2"),
    (seed("integral", sp.cos(3 * x)), "lost_factor", "factor", "L2"),
    (seed("integral", x ** 2), "missing_c", "missing_c", None),
    (seed("integral", x ** 2), "differentiated", "differentiated", "L2"),
    (seed("derivative", sp.sin(x ** 2)), "chain_rule_miss", "chain_rule", "L2"),
    (seed("derivative", x ** 2 * sp.sin(x)), "product_uv", "product_rule", "L2"),
    (seed("derivative", x ** 2 * sp.sin(x)), "product_one_term", "product_rule", "L2"),
    (seed("derivative", sp.exp(x) + x ** 3), "power_rule_exp", "not_equal", "L2"),
    (seed("definite", 2 * x + 1, (1, 3)), "limits_swapped", "sign", "L3"),
])
def test_mistakes_are_caught_by_sympy(s, mistake, error, line):
    page = page_errors(s)[mistake]
    assert page.state["error"] == error and page.state["first_wrong"] == line
    assert not page.correct


def test_correct_pages_are_all_ticked_and_include_partial_work():
    pages = [p for p in b.pages_for(seed("integral", x * sp.cos(x)), random.Random(0)) if p.correct]
    assert [len(p.lines) for p in pages] == [1, 2, 3]
    assert pages[-1].state["finished"] and not pages[0].state["finished"]
    assert all(p.state["first_wrong"] is None and p.state["error"] is None for p in pages)


@pytest.mark.parametrize("kind, f, ptype", [
    ("integral", x * sp.exp(x), "by_parts"), ("integral", 2 * x * sp.exp(x ** 2), "u_sub"),
    ("integral", 3 * x ** 2 + 1, "power"), ("integral", sp.cos(2 * x), "trig"), ("integral", 1 / x, "power"),
    ("derivative", sp.sin(x) / x, "quotient"), ("derivative", x * sp.log(x), "product"),
    ("derivative", (x ** 2 + 1) ** 3, "chain"), ("derivative", sp.exp(3 * x), "exp_log"),
    ("definite", x ** 2, "definite"),
])
def test_classify_problem_type(kind, f, ptype):
    assert b.classify(kind, f) == ptype


@pytest.mark.parametrize("text, kind, line", [
    ("(1) Calculate the definite integral: $\\int_{0}^{1}(e^{x}+2x)dx$", "definite", "∫_0^1 (2x + e^x) dx"),
    ("Find $\\int x e^{x} \\, dx$.", "integral", "∫ x e^x dx"),
    ("Find the derivative of the function: $y= \\frac{x^{2}+\\sin 2x}{e^{x}}$", "derivative", None),
    ("Evaluate \\( \\int \\sin(3t)\\,dt \\)", "integral", "∫ sin(3x) dx"),
])
def test_extract_seed_from_latex(text, kind, line):
    s = b.extract_seed(text, "numina", "n-1")
    assert s is not None and s.kind == kind
    if line:
        assert b.problem_line(s) == line


@pytest.mark.parametrize("text", [
    "Prove that the sequence $ (\\sin n) $ does not have a limit.",
    "Let $f$ be differentiable with $f'(x) > f(x)$. Find the solution set.",
    "Compute $\\int_{L}(x y-1) d x + x^{2} y d y$ along the line.",
    "Find the derivative of $f(x)=3x+1$.",  # linear: nothing to teach
])
def test_extract_seed_skips_non_problems(text):
    assert b.extract_seed(text, "numina", "n-1") is None


def test_numina_drops_aops_and_invalid_rows():
    keep = b.SOURCES["numina"]["keep"]
    assert keep({"source": "cn_k12", "problem_is_valid": "Yes"})
    assert not keep({"source": "aops_forum", "problem_is_valid": "Yes"})
    assert not keep({"source": "cn_k12", "problem_is_valid": "No"})
    assert b.SOURCES["deepmath"]["keep"]({"topic": "Mathematics -> Calculus -> Integral Calculus"})


# --------------------------------------------------------------------------- commands & labels


def test_asr_noise_is_deterministic_and_unpunctuated():
    a = b.asr_noise("Hey Meta, check my integral please?", random.Random(3))
    assert a == b.asr_noise("Hey Meta, check my integral please?", random.Random(3))
    assert a != "Hey Meta, check my integral please?" and a == a.lower()
    assert not any(ch in a for ch in ",?")


@pytest.mark.parametrize("given, wrote, escalate, level", [
    (0, False, False, 1), (1, False, False, 1), (1, True, False, 2), (1, False, True, 2),
    (2, True, False, 3), (2, False, False, 2), (3, True, True, 3), (5, False, False, 3),
])
def test_hint_level_rule(given, wrote, escalate, level):
    assert b.hint_level(router.session_state(given, wrote), escalate) == level


def test_empty_command_label_depends_on_page():
    pages = b.pages_for(seed("integral", x ** 2), random.Random(0))
    done = next(p for p in pages if p.correct and p.state["finished"])
    wrong = next(p for p in pages if p.mistake == "sign_flip")
    s = router.session_state()
    assert b.label("hint", ["give_hint"], done, s, False, True, False)["tools"] == ["check_steps"]
    assert b.label("hint", ["give_hint"], wrong, s, False, True, False)["tools"] == ["give_hint"]
    assert b.label("graph", ["sketch_graph"], wrong, router.session_state(2, True), False, False, True) == {
        "intent": "graph", "tools": ["sketch_graph"], "hint_level": 1, "confidence": 0.85}


# --------------------------------------------------------------------------- end to end


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("router")
    templates = b._synthetic_templates()
    keep = [t for t in templates if t[0] == "definite"][:2] + [t for t in templates if t[0] == "integral"][::3] \
        + [t for t in templates if t[0] == "derivative"][::3]
    orig = b._synthetic_templates
    b._synthetic_templates = lambda: keep
    try:
        manifest = b.build(b.parse_args(["--sources", "synthetic", "--out", str(out), "--raw", str(out / "raw"),
                                         "--train", "300", "--test", "120", "--synthetic-draws", "1"]))
    finally:
        b._synthetic_templates = orig
    return out, manifest


def test_build_writes_fireworks_ready_rows(built):
    out, manifest = built
    train = ev.load_jsonl(out / "train.jsonl")
    assert len(train) == manifest["train"]["rows"] == 300
    for row in train:
        assert set(row) == {"messages"}
        system, user, assistant = row["messages"]
        assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant"]
        assert system["content"] == router.SYSTEM_PROMPT
        assert router.messages_for(json.loads(user["content"]))[1] == user  # one schema with the server
        assert router.is_strict_json(assistant["content"])
        d = router.parse_decision(assistant["content"])
        assert "finish_answer" not in d.tools or d.intent == "answer"
    assert manifest["max_chars_per_row"] < 2048 * 3  # well inside Fireworks' 2,048-token rows


def test_build_holds_out_problem_types_mistakes_and_phrasings(built):
    out, manifest = built
    assert not {"quotient", "definite"} & set(manifest["train"]["ptype"])
    assert not {"ibp_wrong_term", "power_rule_exp"} & set(manifest["train"]["mistake"])
    assert 0.2 <= manifest["train"]["correct_chain_fraction"] <= 0.4
    test = ev.load_jsonl(out / "test.jsonl")
    held = manifest["holdout"]
    allowed = {t for ts in held["test_templates"].values() for t in ts} | set(held["test_combos"]) | {"(empty)"}
    assert all(r["meta"]["template"] in allowed for r in test)
    assert any(r["meta"]["ptype"] in ("quotient", "definite") for r in test)
    train_users = {r["messages"][1]["content"] for r in ev.load_jsonl(out / "train.jsonl")}
    assert not train_users & {r["messages"][1]["content"] for r in test}
    assert {"hint", "answer", "check", "typeset", "graph", "verify", "other"} <= set(manifest["train"]["intent"])
    assert 0 < manifest["train"]["noisy_fraction"] < 1


def test_eval_keyword_baseline_runs_offline(built, tmp_path):
    out, _ = built
    report = ev.main(["--data", str(out / "test.jsonl"), "--train", str(out / "train.jsonl"),
                      "--systems", "keywords", "--out", str(tmp_path / "eval.json")])
    kw = report["systems"]["keywords"]
    assert kw["rows"] == 120 and kw["json_valid"] == 1.0 and kw["errors"] == 0
    assert 0 < kw["tool_accuracy"] < 1
    assert json.loads((tmp_path / "eval.json").read_text())["systems"]["keywords"] == kw


# --------------------------------------------------------------------------- eval metrics


def row(intent, tools, level, noisy=False):
    gold = json.dumps({"intent": intent, "tools": tools, "hint_level": level, "confidence": 0.95})
    return {"messages": [{}, {"content": "{}"}, {"content": gold}], "meta": {"noisy": noisy}}


def pred(intent, tools, level, ms=100.0):
    return ev.Prediction(json.dumps({"intent": intent, "tools": tools, "hint_level": level, "confidence": 0.9}), ms)


def test_score_perfect_and_penalises_over_revealing():
    rows = [row("hint", ["give_hint"], 1), row("hint", ["give_hint"], 2, noisy=True), row("check", ["check_steps"], 1)]
    perfect = ev.score(rows, [pred("hint", ["give_hint"], 1), pred("hint", ["give_hint"], 2), pred("check", ["check_steps"], 1)])
    assert perfect["tool_accuracy"] == 1 and perfect["json_valid"] == 1 and perfect["hint_error"]["weighted"] == 0
    assert perfect["intent_macro_f1_clean"] == 1 and perfect["intent_macro_f1_noisy"] == 1

    over = ev.score(rows[:2], [pred("hint", ["give_hint"], 3), pred("hint", ["give_hint"], 2)])
    under = ev.score(rows[1:2] + rows[:1], [pred("hint", ["give_hint"], 1), pred("hint", ["give_hint"], 1)])
    assert over["hint_error"]["weighted"] == 2.0 and over["hint_error"]["over_reveal_rate"] == 0.5
    assert under["hint_error"]["weighted"] == 0.5  # under-revealing by one costs half as much


def test_score_counts_invalid_json_and_answer_leaks():
    rows = [row("hint", ["give_hint"], 1), row("hint", ["give_hint"], 1), row("answer", ["finish_answer"], 1)]
    preds = [ev.Prediction("```json\n" + pred("answer", ["finish_answer"], 1).text + "\n```", 50.0),
             ev.Prediction("no idea", 70.0), ev.Prediction("", 0.0, "Timeout")]
    s = ev.score(rows, preds)
    assert s["json_valid"] == 0 and s["parsed"] == pytest.approx(1 / 3) and s["errors"] == 1
    assert s["answer_leak"] == 1 and s["tool_accuracy"] == 0
    assert s["confusion_first_tool"] == {"finish_answer": {"invalid": 1}, "give_hint": {"finish_answer": 1, "invalid": 1}}
    assert s["latency_ms"] == {"p50": 60.0, "p95": pytest.approx(69.0)}


def test_macro_f1_and_mcnemar():
    assert ev.macro_f1(["a", "a", "b"], ["a", "b", "b"]) == pytest.approx((2 / 3 + 2 / 3) / 2)
    assert ev.macro_f1([], []) is None
    assert ev.mcnemar([True] * 5 + [False], [False] * 5 + [False]) == {
        "a_only_right": 5, "b_only_right": 0, "p_value": 0.0625}
    assert ev.mcnemar([True, False], [True, False])["p_value"] == 1.0


def test_few_shot_covers_every_intent():
    train = [row(i, [t], 1) for i, t in [("hint", "give_hint"), ("hint", "give_hint"), ("check", "check_steps"),
                                          ("other", None)] if True]
    shots = ev.few_shot([{"messages": [{}, {"role": "user", "content": str(k)}, r["messages"][2]]} for k, r in
                         enumerate(train)], 3)
    intents = [json.loads(m["content"])["intent"] for m in shots if "intent" in m.get("content", "")]
    assert sorted(intents) == ["check", "hint", "other"]
