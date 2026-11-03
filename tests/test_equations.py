"""Equations to solve ("x^2 - 5x + 6 = 0") start a problem instead of reading as a wrong step,
and "give me the answer with working" writes the worked steps."""

from server import checker, meta_client, skills


def answer(*lines, mode="answer"):
    page = [{"id": f"L{i}", "text": t} for i, t in enumerate(lines, 1)]
    return [(a["kind"], a.get("text")) for a in skills.Toolbox(page, 1).route(mode, "test")["annotations"]]


def test_quadratic_is_a_start_and_is_solved_with_working():
    assert checker.check_lines(["x^2 - 5x + 6 = 0"]).lines[0].status == "start"
    assert answer("x^2 - 5x + 6 = 0") == [("write", "(x - 3) (x - 2) = 0"), ("write", "x = 2 or x = 3")]


def test_linear_and_no_real_solutions():
    assert answer("2x + 3 = 7") == [("write", "2x = 4"), ("write", "x = 2")]
    assert answer("x^2 + 1 = 0") == [("write", "No real solutions")]


def test_next_line_must_keep_the_solutions():
    assert checker.check_lines(["x^2 - 5x + 6 = 0", "(x-2)(x-3) = 0"]).first_wrong is None
    assert checker.check_lines(["x^2 - 5x + 6 = 0", "(x-2)(x+3) = 0"]).first_wrong == "L2"


def test_wrong_expansion_is_still_wrong():
    assert checker.check_lines(["x^2 + 2x = x(x+3)"]).first_wrong == "L1"


def test_hints_for_equations():
    assert answer("x^2 - 5x + 6 = 0", mode="hint") == [("note", "Try factorising")]
    assert answer("x^2 + 3x + 1 = 0", mode="hint") == [("note", "Use the quadratic formula")]


def test_working_requests_mean_answer():
    for c in ("give me the answer with working", "show working", "show me the steps", "work it out", "solve it"):
        assert meta_client.keyword_intent(c) == "answer", c
    assert meta_client.keyword_intent("help me solve this") == "hint"
    assert meta_client.keyword_intent("check my working") == "check"


def test_history_reaches_the_agent_prompt():
    t = meta_client.history_text([{"command": "help", "did": "wrote hint"}, {"command": "why?", "did": "why-note"}])
    assert "Student: help -> Margin: wrote hint" in t and "why?" in t
    assert meta_client.history_text([]) == ""


def test_memory_lines_skip_the_page_read(monkeypatch):
    from fastapi.testclient import TestClient
    from server import app as app_module

    called = []
    monkeypatch.setattr(meta_client, "read_page", lambda img: called.append(img) or [])
    r = TestClient(app_module.app).post("/api/assist", json={
        "lines": [{"id": "L1", "text": "30 sin 30 =", "bbox": [100, 300, 300, 50]}],
        "from_memory": True, "mode": "answer", "command": "show working"}).json()
    assert not called
    assert r["agent_trace"][0]["tool"] == "memory"
    assert [a["text"] for a in r["annotations"] if a["kind"] == "write"] == ["= 30 × (1/2)", "= 15"]


def test_a_true_line_below_is_a_new_problem_not_a_wrong_step():
    r = checker.check_lines(["sin 30 = 1/2", "1 + 1 = 2"])
    assert r.first_wrong is None and [l.status for l in r.lines] == ["ok", "ok"]
    assert checker.check_lines(["sin 30 = 1/2", "1 + 1 = 3"]).first_wrong == "L2"


def test_dots_and_stacked_fractions_join_their_ink_row():
    from server.app import _merge_small_rows, _nearest_row
    rows = [[100, 95, 20, 8], [90, 120, 300, 60], [100, 400, 200, 50]]   # i-dot, "sin 30 = 1/2", "1 + 1 = 2"
    merged = _merge_small_rows(rows)
    assert len(merged) == 2 and merged[0][1] == 95
    assert _nearest_row([80, 380, 250, 70], merged) == merged[1]
