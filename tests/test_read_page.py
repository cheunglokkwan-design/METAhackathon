"""Page reading with a reasoning model: empty replies, plain-text replies, ink-row boxes."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from server import app as app_module
from server import meta_client

IMG = "data:image/png;base64,AAAA"


class FakeClient:
    """Returns the queued (content, finish_reason) replies in order and records max_tokens."""

    def __init__(self, replies):
        self.replies, self.budgets = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.budgets.append(kw.get("max_tokens"))
        content, finish = self.replies.pop(0)
        msg = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], usage=None)


def _online(monkeypatch, client):
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setattr(meta_client, "_client", lambda: client)
    meta_client.circuit_breaker.reset()
    meta_client.rate_limiter.reset()


def test_empty_reply_retries_with_bigger_budget(monkeypatch):
    fake = FakeClient([("", "length"), ('{"lines": [{"text": "2 + 2 = 4", "bbox": [90, 120, 820, 200]}]}', "stop")])
    _online(monkeypatch, fake)
    lines = meta_client.read_page(IMG)
    assert [l["text"] for l in lines] == ["2 + 2 = 4"]
    assert fake.budgets[1] > fake.budgets[0] >= 4000
    assert meta_client.last_read_error is None


def test_empty_reply_twice_reports_reason(monkeypatch):
    _online(monkeypatch, FakeClient([("", "length"), ("", "length")]))
    assert meta_client.read_page(IMG) == []
    assert meta_client.last_read_error == "empty reply (finish_reason=length)"


def test_plain_text_reply_is_used(monkeypatch):
    _online(monkeypatch, FakeClient([("Here is the page:\n∫ x dx\n= x^2/2 + C", "stop")]))
    assert [l["text"] for l in meta_client.read_page(IMG)] == ["∫ x dx", "= x^2/2 + C"]


def test_ink_rows_give_boxes_and_failure_reason_reaches_page(monkeypatch):
    client = TestClient(app_module.app)
    _online(monkeypatch, FakeClient([('{"lines": ["∫ 2x dx", "= x^2 + C"]}', "stop")] + [("", "stop")] * 8))
    r = client.post("/api/assist", json={"image": IMG, "mode": "check", "command": "check",
                                         "ink_rows": [[100, 300, 400, 60], [100, 370, 380, 60]]}).json()
    assert [l["bbox"] for l in r["lines"]] == [[100, 300, 400, 60], [100, 370, 380, 60]]

    _online(monkeypatch, FakeClient([("", "length"), ("", "length")]))
    r = client.post("/api/assist", json={"image": IMG, "command": "help"}).json()
    assert r["read_error"].startswith("empty reply")
    assert "read nothing: empty reply" in r["agent_trace"][0]["summary"]
    assert r["annotations"][0]["text"] == "I couldn't read the page just now. Try again?"
