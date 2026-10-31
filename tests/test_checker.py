"""Referee tests: parsing, step checking, mistake detectors, and the solver."""
import sympy as sp

from server.checker import check_lines, parse_line, solve_remaining

x = sp.Symbol("x")


def statuses(res):
    return [l.status for l in res.lines]


def test_correct_by_parts_chain():
    res = check_lines(["∫ x cos x dx", "= x sin x - ∫ sin x dx", "= x sin x + cos x + C"])
    assert statuses(res) == ["start", "ok", "ok"]
    assert res.first_wrong is None and res.finished
    assert res.lines[-1].note is None


def test_sign_error_on_final_by_parts_line():
    # ∫ sin x dx = -cos x, so "- ∫ sin x dx" becomes "+ cos x"; "- cos x" is the slip.
    res = check_lines(["∫ x cos x dx", "= x sin x - ∫ sin x dx", "= x sin x - cos x + C"])
    assert res.first_wrong == "L3"
    d = res.wrong.diagnosis
    assert d.kind == "sign"
    assert "cos x" in d.hints[2]
    assert d.fix == "x sin x + cos x + C"


def test_sign_error_on_integral_term():
    res = check_lines(["∫ x cos x dx", "= x sin x + ∫ sin x dx"])
    assert res.first_wrong == "L2"
    assert res.wrong.diagnosis.kind == "sign"


def test_forgot_half_in_cos_2x():
    res = check_lines(["∫ cos(2x) dx = sin(2x) + C"])
    d = res.wrong.diagnosis
    assert d.kind == "factor"
    assert "1/2" in d.hints[2]
    assert res.wrong.corrected() == "∫ cos(2x) dx = sin(2x)/2 + C"


def test_differentiated_instead_of_integrated():
    res = check_lines(["∫ x^2 dx = 2x"])
    d = res.wrong.diagnosis
    assert d.kind == "differentiated"
    assert d.fix == "x^3/3 + C"


def test_power_rule_divided_by_old_power():
    d = check_lines(["∫ x^3 dx = x^4/3 + C"]).wrong.diagnosis
    assert d.kind == "power_rule"
    assert "4" in d.hints[2]


def test_power_rule_forgot_to_raise():
    d = check_lines(["∫ 3x^2 dx = x^2 + C"]).wrong.diagnosis
    assert d.kind == "power_rule"


def test_missing_plus_c_is_minor():
    res = check_lines(["∫ x^3 dx", "= x^4/4"])
    assert res.first_wrong is None
    assert statuses(res) == ["start", "ok"]
    assert res.lines[-1].note == "Don't forget + C"


def test_generic_wrong_step():
    res = check_lines(["∫ x^2 dx", "= sin x + C"])
    assert res.wrong.diagnosis.kind == "not_equal"


def test_lines_after_first_wrong_are_unchecked():
    res = check_lines(["∫ x cos x dx", "= x sin x + ∫ sin x dx", "= x sin x - cos x + C"])
    assert statuses(res) == ["start", "wrong", "unchecked"]


def test_unicode_and_implicit_parsing():
    p = parse_line("∫ 2x·e^x − 3√x dx")
    assert p.ok and p.parts[0] == sp.Integral(2 * x * sp.exp(x) - 3 * sp.sqrt(x), x)
    assert parse_line("= x sin x − ln|x| + c").has_c == [True]
    assert parse_line("int x^2dx").parts[0] == sp.Integral(x**2, x)
    assert parse_line("sin^2 x + cos²x").parts[0] == sp.sin(x) ** 2 + sp.cos(x) ** 2
    assert parse_line("e^2x").parts[0] == sp.exp(2 * x)


def _assert_answer_verifies(problem: str, integrand):
    steps = solve_remaining([problem])
    assert steps and steps[-1].endswith("+ C")
    final = parse_line(steps[-1])
    assert final.ok
    assert sp.simplify(sp.diff(final.parts[0], x) - integrand) == 0
    # The whole written chain must pass the referee too.
    res = check_lines([problem] + steps)
    assert res.first_wrong is None and res.finished


def test_answer_by_parts_path_x_cos_x():
    steps = solve_remaining(["∫ x cos x dx"])
    assert steps == ["= x sin x - ∫ sin x dx", "= x sin x + cos x + C"]
    _assert_answer_verifies("∫ x cos x dx", x * sp.cos(x))


def test_answer_verifies_for_common_integrals():
    _assert_answer_verifies("∫ x e^x dx", x * sp.exp(x))
    _assert_answer_verifies("∫ x ln x dx", x * sp.log(x))
    _assert_answer_verifies("∫ x^2 e^x dx", x**2 * sp.exp(x))
    _assert_answer_verifies("∫ cos(2x) dx", sp.cos(2 * x))
    _assert_answer_verifies("∫ 3x^2 + 1 dx", 3 * x**2 + 1)


def test_answer_continues_from_partial_work():
    steps = solve_remaining(["∫ x cos x dx", "= x sin x - ∫ sin x dx"])
    assert steps == ["= x sin x + cos x + C"]


# --------------------------------------------------------------------------- round 2

import pytest

from server.checker import next_step_hints


@pytest.mark.parametrize("text, expected", [
    ("ln|x|", sp.log(sp.Abs(x))),
    ("sin^2 x", sp.sin(x) ** 2),
    ("sin^-1 x", sp.asin(x)),
    ("sin^−1 x", sp.asin(x)),
    ("arcsin x", sp.asin(x)),
    ("tan^-1 x", sp.atan(x)),
    ("tan^(-1) x", sp.atan(x)),
    ("1/(1+x^2)", 1 / (1 + x**2)),
    ("√x", sp.sqrt(x)),
    ("x√x", x**sp.Rational(3, 2)),
    ("e^{2x}", sp.exp(2 * x)),
    ("∫ xdx", sp.Integral(x, x)),
    ("∫ x^2dx", sp.Integral(x**2, x)),
    ("∫ dx/x", sp.Integral(1 / x, x)),
    ("−x + 3", -x + 3),
    ("2 ∫ x dx", 2 * sp.Integral(x, x)),
])
def test_parse_robustness(text, expected):
    p = parse_line(text)
    assert p.ok, p.error
    assert p.parts[0] == expected


def test_ln_abs_x_antiderivative_is_correct():
    res = check_lines(["∫ 1/x dx", "= ln|x| + C"])
    assert [l.status for l in res.lines] == ["start", "ok"]
    assert solve_remaining(["∫ 1/x dx"]) == ["= ln|x| + C"]


def test_inverse_trig_antiderivative():
    res = check_lines(["∫ 1/(1+x^2) dx", "= tan^-1 x + C"])
    assert res.first_wrong is None


@pytest.mark.parametrize("problem, u", [
    ("∫ 2x e^(x^2) dx", "x^2"),
    ("∫ x cos(x^2) dx", "x^2"),
    ("∫ 2x(x^2+1)^5 dx", "x^2 + 1"),
])
def test_u_sub_detected_and_solved(problem, u):
    hints = next_step_hints(check_lines([problem]))
    assert hints[1] == "Try a u-substitution"
    assert hints[2] == f"Let u = {u}"
    steps = solve_remaining([problem])
    assert check_lines([problem] + steps).finished


def test_technique_named_for_power_rule_and_parts():
    assert next_step_hints(check_lines(["∫ x^3 dx"]))[1] == "Use the power rule"
    assert next_step_hints(check_lines(["∫ x e^x dx"]))[1] == "Try integration by parts"
