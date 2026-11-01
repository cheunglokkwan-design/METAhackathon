"""Force offline mode for every test (no network, deterministic hints)."""
import pytest

from server import meta_client, router

ROUTER_ENV = ("ROUTER_BASE_URL", "ROUTER_MODEL", "ROUTER_API_KEY", "ROUTER_RESPONSE_FORMAT",
              "ROUTER_MIN_CONFIDENCE", "ROUTER_TIMEOUT", "ROUTER_MAX_TOKENS")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "")
    for name in ROUTER_ENV:  # a local .env must not turn the router on in tests
        monkeypatch.delenv(name, raising=False)
    meta_client.rate_limiter.reset()
    meta_client.circuit_breaker.reset()
    router.rate_limiter.reset()
    router.circuit_breaker.reset()
