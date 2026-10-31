"""Regression set: common calculus mistakes. Each must be caught on the right line with
a hint that names the right idea, and the level-1 hint must not leak the fix."""
import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.checker import check_lines

client = TestClient(app)

# (lines, expected first_wrong, expected diagnosis kind, keyword in the level 1/2 hints)
MISTAKES = [
    (["∫ x cos x dx", "= x sin x - ∫ sin x dx", "= x sin x - cos x + C"], "L3", "sign", "sign"),
    (["∫ x^2 dx = 2x"], "L1", "differentiated", "integrate"),
    (["∫ x^3 dx = x^4/3 + C"], "L1", "power_rule", "power"),
    (["∫ cos(2x) dx = sin(2x) + C"], "L1", "factor", "1/2"),
    (["∫ e^(2x) dx", "= e^(2x) + C"], "L2", "factor", "1/2"),
    (["d/dx (x^2 sin x)", "= 2x sin x"], "L2", "product_rule", "two terms"),
    (["d/dx (x^2 sin x)", "= 2x cos x"], "L2", "product_rule", "u'v + uv'"),
    (["d/dx sin(x^2)", "= cos(x^2)"], "L2", "chain_rule", "chain"),
    (["f(x) = e^(3x)", "f'(x) = e^(3x)"], "L2", "chain_rule", "3x"),
    (["d/dx cos x = sin x"], "L1", "trig_sign", "−sin"),
    (["d/dx (x cos x)", "= cos x + x sin x"], "L2", "trig_sign", "sign"),
    (["d/dx x^3 = 3x^3"], "L1", "power_rule", "lower the power"),
    (["∫_0^2 x dx = -2"], "L1", "limits", "limits"),
    (["∫_1^2 2x dx = 4"], "L1", "limits", "lower limit"),
    (["∫_0^1 x^2 dx", "= [x^3/2]_0^1", "= 1/2"], "L2", "antiderivative", "power"),
]


@pytest.mark.parametrize("lines, first_wrong, kind, keyword", MISTAKES,
                         ids=[" | ".join(m[0]) for m in MISTAKES])
def test_mistake_is_caught(lines, first_wrong, kind, keyword):
    res = check_lines(lines)
    assert res.first_wrong == first_wrong
    d = res.wrong.diagnosis
    assert d.kind == kind, d.verdict
    assert keyword.lower() in (d.hints[1] + " " + d.hints[2]).lower(), d.hints
    if d.fix:
        assert d.fix.split("+ C")[0].strip() not in d.hints[1]


@pytest.mark.parametrize("lines, first_wrong, kind, keyword", MISTAKES[:10])
def test_mistake_ink_through_api(lines, first_wrong, kind, keyword):
    payload = [{"id": f"L{i}", "text": t} for i, t in enumerate(lines, 1)]
    body = client.post("/api/assist", json={"lines": payload, "command": "help", "hint_level": 1}).json()
    assert body["first_wrong"] == first_wrong
    assert {"kind": "circle", "target": first_wrong} in body["annotations"]
    note = next(a for a in body["annotations"] if a["kind"] == "note")
    assert note["target"] == first_wrong and len(note["text"].split()) <= 8


def test_fixes_are_correct_and_pass_the_referee():
    """The level-3 fix for every mistake, written in place, must check out."""
    for lines, *_ in MISTAKES:
        res = check_lines(lines)
        fixed = res.wrong.corrected()
        assert fixed, lines
        idx = [l.id for l in res.lines].index(res.first_wrong)
        again = check_lines(lines[:idx] + [fixed])
        assert again.first_wrong is None, (lines, fixed, again.verdict)
