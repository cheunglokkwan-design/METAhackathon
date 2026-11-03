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
    assert r["annotations"][0]["text"] == "I couldn't read the page: Meta ran out of thinking room. Try again?"


def test_a_timeout_is_retried_once_and_explained(monkeypatch):
    class Slow(FakeClient):
        fails = 1

        def create(self, **kw):
            if self.fails:
                self.fails -= 1
                raise TimeoutError("read timed out")
            return super().create(**kw)

    _online(monkeypatch, Slow([('{"lines": ["30 sin 30 ="]}', "stop")]))
    assert [l["text"] for l in meta_client.read_page(IMG)] == ["30 sin 30 ="]

    class Dead(FakeClient):
        def create(self, **kw):
            raise TimeoutError("read timed out")

    _online(monkeypatch, Dead([]))
    assert meta_client.read_page(IMG) == []
    assert meta_client.read_error_text(meta_client.last_read_error) == "Meta took too long to answer"


class NotFound(Exception):
    status_code = 404

    def __init__(self, model):
        super().__init__("not found")
        self.body = {"error": {"message": f"The model `{model}` does not exist or is not available",
                               "code": "model_not_found"}}


def test_unknown_model_falls_back_to_a_muse_spark_that_exists(monkeypatch):
    seen = []

    def create(**kw):
        seen.append(kw["model"])
        if kw["model"] != "muse-spark-1.2":
            raise NotFound(kw["model"])
        msg = SimpleNamespace(content='{"lines": ["30 sin 30 ="]}', tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    _online(monkeypatch, fake)
    monkeypatch.setattr(meta_client, "CHAT_MODEL", "muse-spark-9")
    monkeypatch.setitem(meta_client._working, "model", None)
    assert [l["text"] for l in meta_client.read_page(IMG)] == ["30 sin 30 ="]
    assert seen[0] == "muse-spark-9" and "muse-spark-1.2" in seen
    assert meta_client.chat_model() == "muse-spark-1.2"


def test_404_everywhere_says_which_settings_to_check(monkeypatch):
    def create(**kw):
        raise NotFound(kw["model"])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    _online(monkeypatch, fake)
    monkeypatch.setitem(meta_client._working, "model", None)
    assert meta_client.read_page(IMG) == []
    assert meta_client.last_read_error.startswith("Meta API error 404 (The model")
    assert "CHAT_MODEL" in meta_client.read_error_text(meta_client.last_read_error)
