"""A line ending in a bare "=" ("30 sin 30 =") asks for its value; whole-number trig angles like 30
are read as degrees, and the referee accepts the degree answer."""

from server import checker, skills


def run(tool, text, level=1):
    b = skills.Toolbox([{"id": "L1", "text": text}], level)
    b.run(tool)
    return [(a["kind"], a.get("text")) for a in b.annotations]


def test_trailing_equals_is_a_request_not_an_error():
    p = checker.parse_line("30 sin 30 =")
    assert p.ok and p.asks_value and len(p.parts) == 1


def test_answer_and_next_step_work_it_out_in_degrees():
    assert run("finish_answer", "30 sin 30 =") == [("write", "= 15"), ("note", "Taking 30 as degrees")]
    assert run("next_step", "30*sin(30) =")[0] == ("write", "= 15")


def test_hints_build_up_to_the_value():
    assert "degrees" in run("give_hint", "30 sin 30 =", 1)[0][1]
    assert run("give_hint", "30 sin 30 =", 2) == [("note", "sin 30° = 1/2")]
    assert ("ghost", "= 15") in run("give_hint", "30 sin 30 =", 3)
    assert "Nothing left" not in str(run("check_steps", "30 sin 30 ="))


def test_referee_accepts_the_degree_answer_only():
    assert [l.status for l in checker.check_lines(["30 sin 30", "= 15"]).lines] == ["start", "ok"]
    assert checker.check_lines(["30 sin 30", "= 16"]).first_wrong == "L2"


def test_other_values():
    assert checker.solve_remaining(checker.check_lines(["3 + 4 ="])) == ["= 7"]
    assert checker.solve_remaining(checker.check_lines(["sin(1) ="])) == ["≈ 0.8415"]
    assert checker.solve_remaining(checker.check_lines(["2 cos 60 + tan 45 ="])) == ["= 2"]
