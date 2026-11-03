"""Lasso › To text: /api/read turns a cropped image of handwriting into lines of text."""

from fastapi.testclient import TestClient

from server import app as app_module, meta_client

client = TestClient(app_module.app)
IMG = "data:image/png;base64,iVBORw0KGgo="


def test_offline_says_so(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    r = client.post("/api/read", json={"image": IMG})
    assert r.status_code == 503 and "offline" in r.json()["detail"]


def test_reads_lines(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "k")
    seen = {}

    def chat(messages, max_tokens=1500):
        seen["messages"] = messages
        return '```json\n{"lines": ["Newton second law", {"text": "F = m a"}, ""]}\n```'
    monkeypatch.setattr(meta_client, "_chat", chat)
    r = client.post("/api/read", json={"image": IMG})
    assert r.json() == {"lines": ["Newton second law", "F = m a"]}
    assert seen["messages"][1]["content"][0]["image_url"]["url"] == IMG


def test_refuses_non_images(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setattr(meta_client, "_chat", lambda *a, **k: '{"lines": ["x"]}')
    assert client.post("/api/read", json={"image": "javascript:alert(1)"}).json() == {"lines": []}
