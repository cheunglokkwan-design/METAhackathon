"""Force offline mode for every test (no network, deterministic hints)."""
import pytest


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "")
