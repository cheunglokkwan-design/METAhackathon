"""API tests in offline mode via FastAPI's TestClient."""
import asyncio

from fastapi.testclient import TestClient

from server.app import app

client = TestClient(app)


def lines(*texts):
    return [{"id": f"L{i}", "text": t, "bbox": [40, 40 + 60 * i, 300, 50]} for i, t in enumerate(texts, 1)]


def kinds(body):
    return [a["kind"] for a in body["annotations"]]


def test_health_offline():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "offline": True, "model": "muse-spark-1.3"}


def test_hint_on_sign_error():
    body = client.post("/api/assist", json={
        "lines": lines("∫ x cos x dx", "= x sin x - ∫ sin x dx", "= x sin x - cos x + C"),
        "command": "hey meta help me with this integral", "mode": "auto", "hint_level": 1,
    }).json()
    assert body["mode"] == "hint" and body["offline"] is True
    assert body["first_wrong"] == "L3"
    assert [l["status"] for l in body["lines"]] == ["start", "ok", "wrong"]
    assert {"kind": "tick", "target": "L2"} in body["annotations"]
    assert {"kind": "circle", "target": "L3"} in body["annotations"]
    note = next(a for a in body["annotations"] if a["kind"] == "note")
    assert note["target"] == "L3" and len(note["text"].split()) <= 8
    assert "+ cos" not in note["text"]  # level 1 must not leak the fix
    assert body["lines"][0]["latex"].startswith("\\int")


def test_hint_level_3_ghost_and_underline():
    body = client.post("/api/assist", json={
        "lines": lines("∫ x cos x dx", "= x sin x + ∫ sin x dx", "= x sin x - cos x + C"),
        "mode": "hint", "hint_level": 3,
    }).json()
    assert body["first_wrong"] == "L2"
    assert {"kind": "underline", "target": "L3"} in body["annotations"]
    ghost = next(a for a in body["annotations"] if a["kind"] == "ghost")
    assert ghost == {"kind": "ghost", "after": "L2", "text": "= x sin x - ∫ sin x dx"}
    assert "write" not in kinds(body)


def test_answer_mode_writes_remaining_steps():
    body = client.post("/api/assist", json={
        "lines": lines("∫ x cos x dx"), "command": "just give me the answer",
    }).json()
    assert body["mode"] == "answer"
    writes = [a for a in body["annotations"] if a["kind"] == "write"]
    assert [w["text"] for w in writes] == ["= x sin x - ∫ sin x dx", "= x sin x + cos x + C"]
    assert all(w["after"] == "L1" for w in writes)


def test_check_mode_all_correct_and_missing_c():
    body = client.post("/api/assist", json={
        "lines": lines("∫ x^3 dx", "= x^4/4"), "command": "can you check this",
    }).json()
    assert body["mode"] == "check" and body["first_wrong"] is None
    assert {"kind": "tick", "target": "L2"} in body["annotations"]
    assert {"kind": "note", "target": "L2", "text": "Don't forget + C", "side": "right"} in body["annotations"]


def test_hint_nudges_next_step_when_only_problem_written():
    body = client.post("/api/assist", json={"lines": lines("∫ x cos x dx"), "hint_level": 2}).json()
    note = next(a for a in body["annotations"] if a["kind"] == "note")
    assert note["text"] == "Let u = x, dv = cos x dx"


def test_offline_image_only_explains():
    body = client.post("/api/assist", json={"image": "data:image/png;base64,AAAA", "command": "help"}).json()
    assert body["lines"] == []
    assert "T tool" in body["annotations"][0]["text"]


def test_transcribe_offline_503():
    r = client.post("/api/transcribe", files={"audio": ("a.webm", b"\x00\x01", "audio/webm")})
    assert r.status_code == 503


def test_transcribe_empty_audio_400():
    r = client.post("/api/transcribe", files={"audio": ("a.webm", b"", "audio/webm")})
    assert r.status_code == 400


def test_transcribe_oversized_audio_413(monkeypatch):
    from server import meta_client

    monkeypatch.setattr(meta_client, "MAX_AUDIO_BYTES", 10)
    r = client.post("/api/transcribe", files={"audio": ("a.webm", b"x" * 11, "audio/webm")})
    assert r.status_code == 413


def test_transcribe_refuses_large_body_from_header():
    from server import app as app_module, meta_client

    too_big = meta_client.MAX_AUDIO_BYTES + app_module.FORM_OVERHEAD + 1
    r = client.post("/api/transcribe", content=b"x", headers={"content-length": str(too_big),
                                                             "content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 413
    assert r.headers["x-content-type-options"] == "nosniff"


def test_transcribe_does_not_block_the_event_loop():
    from server import app as app_module

    route = next(r for r in app_module.app.routes if getattr(r, "path", "") == "/api/transcribe")
    assert not asyncio.iscoroutinefunction(route.endpoint)


def test_unreadable_line_is_unchecked():
    body = client.post("/api/assist", json={"lines": lines("∫ x^2 dx", "= ))((", "= x^3/3 + C")}).json()
    assert [l["status"] for l in body["lines"]] == ["start", "unchecked", "ok"]


def test_level_3_nudge_names_technique_and_ghosts_next_step():
    body = client.post("/api/assist", json={"lines": lines("∫ 2x e^(x^2) dx"), "hint_level": 3}).json()
    note = next(a for a in body["annotations"] if a["kind"] == "note")
    ghost = next(a for a in body["annotations"] if a["kind"] == "ghost")
    assert note["text"] == "Try a u-substitution"
    assert ghost == {"kind": "ghost", "after": "L1", "text": "= e^(x^2) + C"}


def test_ln_abs_line_gets_tick():
    body = client.post("/api/assist", json={"lines": lines("∫ 1/x dx", "= ln|x| + C"), "mode": "check"}).json()
    assert body["first_wrong"] is None
    assert {"kind": "tick", "target": "L2"} in body["annotations"]
