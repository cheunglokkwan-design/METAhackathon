"""Unit tests for RateLimiter, CircuitBreaker, and outbound API instrumentation in server/meta_client.py."""

from types import SimpleNamespace
import pytest

from server import meta_client
from server.meta_client import CircuitBreaker, OfflineError, RateLimiter


class _DummyClient:
    """Fake OpenAI client for testing outbound instrumentation without network."""

    def __init__(self, reply: str = "hint", fail: Exception | None = None, transcript: str = "hello meta"):
        self.calls = 0
        self.reply = reply
        self.fail = fail
        self.transcript = transcript
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat))
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self._create_audio))

    def _create_chat(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise self.fail
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])

    def _create_audio(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise self.fail
        return SimpleNamespace(text=self.transcript)


def test_rate_limiter_sliding_window(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(meta_client.time, "monotonic", lambda: clock[0])

    limiter = RateLimiter(max_calls=2, period=10.0)
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False

    # Advance 5 seconds: still within 10s window
    clock[0] = 105.0
    assert limiter.allow() is False

    # Advance past the 10s window of the first two calls
    clock[0] = 110.1
    assert limiter.allow() is True


def test_circuit_breaker_state_transitions(monkeypatch):
    clock = [200.0]
    monkeypatch.setattr(meta_client.time, "monotonic", lambda: clock[0])

    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0)
    assert cb.state == CircuitBreaker.CLOSED
    assert cb.allow_request() is True

    cb.record_failure()
    assert cb.state == CircuitBreaker.CLOSED
    assert cb.failure_count == 1

    cb.record_failure()
    assert cb.state == CircuitBreaker.OPEN
    assert cb.allow_request() is False

    # After recovery timeout, state becomes HALF_OPEN and allows one probe
    clock[0] = 230.5
    assert cb.state == CircuitBreaker.HALF_OPEN
    assert cb.allow_request() is True
    # Concurrent call while probe is in flight is blocked
    assert cb.allow_request() is False

    # Probe fails -> back to OPEN immediately
    cb.record_failure()
    assert cb.state == CircuitBreaker.OPEN
    assert cb.allow_request() is False

    # Wait recovery timeout again, then succeed -> CLOSED
    clock[0] = 261.0
    assert cb.allow_request() is True
    cb.record_success()
    assert cb.state == CircuitBreaker.CLOSED
    assert cb.failure_count == 0
    assert cb.allow_request() is True


def test_rate_limiter_degrades_to_offline_fallback(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("META_RATE_LIMIT_CALLS", "2")
    monkeypatch.setenv("META_RATE_LIMIT_PERIOD", "60")

    dummy = _DummyClient(reply="check")
    monkeypatch.setattr(meta_client, "_client", lambda: dummy)

    # First two calls use the API
    assert meta_client.classify_intent("what do you think") == "check"
    assert meta_client.classify_intent("what do you think") == "check"
    assert dummy.calls == 2

    # Third call is rate-limited: does NOT call the API and falls back to keyword_intent
    assert meta_client.classify_intent("just give me the answer") == "answer"
    assert dummy.calls == 2


def test_circuit_breaker_trips_and_protects_api(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("META_CB_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("META_CB_RECOVERY_TIMEOUT", "60")

    dummy = _DummyClient(fail=RuntimeError("upstream 500"))
    monkeypatch.setattr(meta_client, "_client", lambda: dummy)

    # Two failing calls trip the circuit breaker; both gracefully return fallback
    assert meta_client.phrase_hint("sign error", "= x", 1, "Check the sign") == "Check the sign"
    assert meta_client.phrase_hint("sign error", "= x", 1, "Check the sign") == "Check the sign"
    assert dummy.calls == 2
    assert meta_client.circuit_breaker.state == CircuitBreaker.OPEN

    # Subsequent calls short-circuit immediately without hitting the client
    assert meta_client.phrase_hint("sign error", "= x", 1, "Check the sign") == "Check the sign"
    assert meta_client.read_page("data:image/png;base64,AAAA") == []
    assert dummy.calls == 2


def test_transcribe_respects_rate_limiter_and_circuit_breaker(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("META_RATE_LIMIT_CALLS", "1")
    monkeypatch.setenv("META_RATE_LIMIT_PERIOD", "60")

    dummy = _DummyClient(transcript="integrate x squared")
    monkeypatch.setattr(meta_client, "_client", lambda: dummy)

    assert meta_client.transcribe(b"audio-bytes") == "integrate x squared"
    assert dummy.calls == 1

    # Second call exceeds rate limit -> raises OfflineError without calling upstream
    with pytest.raises(OfflineError, match="rate limit"):
        meta_client.transcribe(b"audio-bytes")
    assert dummy.calls == 1

    # Empty audio payload is rejected before touching client or rate limiter
    meta_client.rate_limiter.reset()
    with pytest.raises(OfflineError, match="empty audio"):
        meta_client.transcribe(b"")
    assert dummy.calls == 1
