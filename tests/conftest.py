"""Force offline mode for every test (no network, deterministic hints)."""
import pytest

from server import meta_client


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "")
    meta_client.rate_limiter.reset()
    meta_client.circuit_breaker.reset()
