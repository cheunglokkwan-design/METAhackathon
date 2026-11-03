"""3D surfaces (sketch_3d, and sketch_graph on a two-variable function) and Muse Image pictures."""

import base64

import pytest

from server import checker, meta_client, render, skills


def tool(name, lines, args=None):
    b = skills.Toolbox([{"id": f"L{i}", "text": t} for i, t in enumerate(lines, 1)])
    b.run(name, args or {})
    return b.annotations


def test_surface_from_the_page_or_an_expression():
    g = tool("sketch_3d", ["z = x^2 - y^2"])[0]
    assert g["kind"] == "graph3d" and g["fn"] == "z = x^2 - y^2" and g["after"] == "L1"
    n = render.GRID3D
    assert len(g["xs"]) == n and len(g["z"]) == n and all(len(r) == n for r in g["z"])
    assert g["zrange"] == [-4.0, 4.0]
    assert tool("sketch_3d", ["30 sin 30 ="], {"expression": "sin(x)cos(y)"})[0]["fn"] == "z = sin x cos y"


def test_graph_request_on_two_variables_draws_3d_and_definitions_are_not_wrong():
    assert [a["kind"] for a in tool("sketch_graph", ["f(x, y) = x y"])] == ["graph3d"]
    for line in ("z = x^2 + y^2", "f(x,y) = x^2 y", "v = 30 sin 30"):
        assert checker.check_lines([line]).first_wrong is None


def test_no_surface_without_two_variables():
    a = tool("sketch_3d", ["∫ x dx"])
    assert a[0]["kind"] == "note" and "x and y" in a[0]["text"]


def test_spiky_surfaces_are_clipped():
    g = render.graph3d_for(render.surface_expr(checker.check_lines(["1"]), "1/(x^2+y^2)"))
    assert g["zrange"][1] < 10


def test_picture_needs_meta_and_a_subject(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert tool("draw_picture", ["x"], {"description": "a cone"})[0]["text"] == "Pictures need Meta online"
    assert skills.picture_subject("Hey Meta, draw me a picture of a ladder leaning on a wall") == "a ladder leaning on a wall"
    assert meta_client.keyword_intent("can you draw a diagram of a pendulum") == "picture"


def test_picture_from_muse_image(monkeypatch):
    import httpx
    monkeypatch.setenv("MODEL_API_KEY", "k")
    meta_client.rate_limiter.reset()
    meta_client.circuit_breaker.reset()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    seen = {}

    class R:
        status_code = 200
        def json(self):
            return {"data": [{"b64_json": base64.b64encode(png).decode()}]}

    def post(url, headers, json, timeout):
        seen.update(url=url, body=json)
        return R()
    monkeypatch.setattr(httpx, "post", post)
    a = tool("draw_picture", ["x"], {"description": "a ladder on a wall"})[0]
    assert a["kind"] == "picture" and a["image"].startswith("data:image/png;base64,")
    assert seen["url"].endswith("/images/generations") and seen["body"]["model"] == "muse-image-1.0"
    assert "ladder on a wall" in seen["body"]["prompt"] and "pencil sketch" in seen["body"]["prompt"]


def test_stored_interactions_leave_pictures_out():
    from server.app import _without_pictures
    resp = {"annotations": [{"kind": "picture", "image": "data:image/png;base64,AAAA"}, {"kind": "note", "text": "x"}]}
    out = _without_pictures(resp)
    assert out["annotations"][0]["image"] == "(picture not stored)" and resp["annotations"][0]["image"].startswith("data:")
