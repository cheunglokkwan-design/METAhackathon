"""Vercel page + Railway data: CORS for the Vercel origin, cross-site cookies, /api/config.js."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from server import app as app_module
from server.app import app
from server.auth import auth_rate_limiter, write_rate_limiter
from server.db import db

VERCEL = "https://met-ahackathon.vercel.app"


@pytest.fixture(autouse=True)
def _isolated_test_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-super-secret-jwt-key-with-at-least-32-bytes-2026")
    monkeypatch.delenv("COOKIE_SAMESITE", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    db.reset_for_tests("sqlite:///:memory:")
    auth_rate_limiter.reset()
    write_rate_limiter.reset()
    yield
    db.reset_for_tests(None)


def test_cors_preflight_allows_vercel_with_credentials():
    c = TestClient(app)
    r = c.options("/api/notebooks", headers={
        "Origin": VERCEL,
        "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization,content-type,x-csrf-token",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == VERCEL
    assert r.headers["access-control-allow-credentials"] == "true"
    assert "x-csrf-token" in r.headers["access-control-allow-headers"].lower()


def test_cors_refuses_other_sites():
    c = TestClient(app)
    r = c.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    pre = c.options("/api/notebooks", headers={"Origin": "https://evil.example",
                                               "Access-Control-Request-Method": "GET"})
    assert pre.status_code == 400


def test_error_responses_carry_cors_headers():
    r = TestClient(app).get("/api/notebooks", headers={"Origin": VERCEL})
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == VERCEL


def test_config_js_reads_either_env_name(monkeypatch):
    c = TestClient(app)
    monkeypatch.delenv("MARGIN_API_URL", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_API_URL", raising=False)
    r = c.get("/api/config.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.text.strip() == 'window.MARGIN_API_URL = "";'
    monkeypatch.setenv("NEXT_PUBLIC_API_URL", "https://sweet-youth-production-a78c.up.railway.app/")
    assert c.get("/api/config.js").text.strip() == \
        'window.MARGIN_API_URL = "https://sweet-youth-production-a78c.up.railway.app";'
    monkeypatch.setenv("MARGIN_API_URL", "javascript:alert(1)")  # not a URL: ignored
    assert app_module.data_api_url() == ""


def test_https_cookies_are_samesite_none_secure():
    c = TestClient(app, base_url="https://testserver")
    r = c.post("/api/auth/register", json={"email": "noether@example.com", "password": "RingTheory1921!",
                                           "display_name": "Emmy"})
    assert r.status_code in (200, 201), r.text
    cookies = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    assert cookies and all("samesite=none" in v.lower() and "secure" in v.lower() for v in cookies)


def test_http_cookies_stay_lax():
    c = TestClient(app)
    r = c.post("/api/auth/register", json={"email": "hilbert@example.com", "password": "Hotel1nfinity!",
                                           "display_name": "David"})
    assert r.status_code in (200, 201), r.text
    cookies = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    assert cookies and all("samesite=lax" in v.lower() for v in cookies)
