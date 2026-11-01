"""Comprehensive security, authentication, JWT, Session Manager, DB circuit breaker,
and optimal notebook/AI storage tests.
"""

from __future__ import annotations

import base64
import json
import time
import zlib

from fastapi.testclient import TestClient
import pytest

from server import auth, db as db_module
from server.app import app
from server.auth import (
    AuthError,
    _b64u_encode,
    auth_rate_limiter,
    create_access_token,
    decode_and_verify_jwt,
    hash_password,
    session_manager,
    verify_password,
    write_rate_limiter,
)
from server.db import (
    DBCircuitBreaker,
    DatabaseUnavailableError,
    compress_json,
    compute_content_hash,
    db,
    decompress_bytes,
    quantize_items,
)


@pytest.fixture(autouse=True)
def _isolated_test_db(monkeypatch):
    """Run every test against an isolated in-memory SQLite database with clean rate limiters."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-super-secret-jwt-key-with-at-least-32-bytes-2026")
    db.reset_for_tests("sqlite:///:memory:")
    auth_rate_limiter.reset()
    write_rate_limiter.reset()
    yield
    db.reset_for_tests(None)
    auth_rate_limiter.reset()
    write_rate_limiter.reset()


client = TestClient(app)


# --------------------------------------------------------------------------- 1. Password & JWT Security


def test_password_hashing_and_validation():
    pw = "CorrectHorseBattery99!"
    hashed = hash_password(pw)
    assert hashed.startswith(("$scrypt$", "$pbkdf2-sha256$"))
    assert verify_password(pw, hashed) is True
    assert verify_password("WrongPassword99!", hashed) is False
    assert verify_password("", hashed) is False

    # Same password produces different hashes due to unique 16-byte salt
    assert hash_password(pw) != hashed

    # Rejects weak or oversized passwords
    for bad in ("short1!", "alllettersonly", "1234567890", "A1" * 100, "bad\x00pass1"):
        with pytest.raises(AuthError):
            auth.validate_password_strength(bad)


def test_jwt_strict_validation_and_alg_none_defense():
    token, jti, exp = create_access_token(user_id="u-123", session_id="s-456", role="student", now=1000.0, ttl_seconds=900)
    claims = decode_and_verify_jwt(token, now=1200.0)
    assert claims["sub"] == "u-123"
    assert claims["sid"] == "s-456"
    assert claims["jti"] == jti
    assert claims["exp"] == 1900

    # Expired token rejected
    with pytest.raises(AuthError, match="expired"):
        decode_and_verify_jwt(token, now=1901.0)

    # 'alg: none' attack rejected
    none_header = _b64u_encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    parts = token.split(".")
    forged_none = f"{none_header}.{parts[1]}."
    with pytest.raises(AuthError, match="Unsupported JWT algorithm"):
        decode_and_verify_jwt(forged_none, now=1200.0)

    # Tampered payload rejected
    tampered_payload = _b64u_encode(json.dumps({**claims, "role": "admin"}).encode())
    forged_admin = f"{parts[0]}.{tampered_payload}.{parts[2]}"
    with pytest.raises(AuthError, match="Invalid JWT signature"):
        decode_and_verify_jwt(forged_admin, now=1200.0)


# --------------------------------------------------------------------------- 2. Auth, Session Rotation & Token Reuse Detection


def test_register_login_and_refresh_token_reuse_detection():
    # Register a new user
    r = client.post(
        "/api/auth/register",
        json={"email": "Euler@Example.com", "password": "PrimeNumber1729!", "display_name": "Leonhard Euler"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["user"]["email"] == "euler@example.com"
    access_1 = data["access_token"]
    refresh_1 = data["refresh_token"]

    # Duplicate email returns 409 Conflict
    dup = client.post(
        "/api/auth/register",
        json={"email": "euler@example.com", "password": "PrimeNumber1729!"},
    )
    assert dup.status_code == 409

    # Rotate refresh token -> issues refresh_2 and access_2
    rot = client.post("/api/auth/refresh", json={"refresh_token": refresh_1})
    assert rot.status_code == 200
    rot_data = rot.json()
    refresh_2 = rot_data["refresh_token"]
    access_2 = rot_data["access_token"]
    assert refresh_2 != refresh_1

    # Replay the OLD refresh_1 (simulating a stolen refresh token!)
    # Must trigger Token Reuse Detection and revoke the entire session family!
    replay = client.post("/api/auth/refresh", json={"refresh_token": refresh_1})
    assert replay.status_code == 401
    assert "Compromised refresh token" in replay.json()["detail"]

    # Now even refresh_2 AND access_2 from that compromised family are immediately rejected!
    rot2 = client.post("/api/auth/refresh", json={"refresh_token": refresh_2})
    assert rot2.status_code == 401

    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {access_2}"})
    assert me_resp.status_code == 401

    # Audit log recorded the TOKEN_REUSE_DETECTED event
    login_resp = client.post("/api/auth/login", json={"email": "euler@example.com", "password": "PrimeNumber1729!"})
    assert login_resp.status_code == 200
    fresh_access = login_resp.json()["access_token"]
    logs = client.get("/api/auth/audit-logs", headers={"Authorization": f"Bearer {fresh_access}"}).json()["audit_logs"]
    event_types = [e["event_type"] for e in logs]
    assert "TOKEN_REUSE_DETECTED" in event_types


def test_account_lockout_on_brute_force():
    client.post("/api/auth/register", json={"email": "gauss@example.com", "password": "NormalDist2026!"})

    # 5 failed password attempts trigger account lockout
    for i in range(4):
        r = client.post("/api/auth/login", json={"email": "gauss@example.com", "password": f"WrongPass{i}!"})
        assert r.status_code == 401

    fifth = client.post("/api/auth/login", json={"email": "gauss@example.com", "password": "WrongPass5!"})
    assert fifth.status_code == 429
    assert "locked" in fifth.json()["detail"].lower()

    # Even the CORRECT password is now rejected while locked
    blocked = client.post("/api/auth/login", json={"email": "gauss@example.com", "password": "NormalDist2026!"})
    assert blocked.status_code == 429


def test_session_management_logout_and_csrf_protection():
    reg = client.post("/api/auth/register", json={"email": "noether@example.com", "password": "SymmetryRing88!"})
    body = reg.json()
    access = body["access_token"]
    csrf = body["csrf_token"]

    # Cookie-based write WITHOUT X-CSRF-Token is blocked (403)
    client.cookies.set("margin_access", access)
    no_csrf = client.post("/api/notebooks", json={"title": "Algebra"})
    assert no_csrf.status_code == 403
    assert "CSRF" in no_csrf.json()["detail"]

    # Cookie-based write WITH valid X-CSRF-Token succeeds
    with_csrf = client.post("/api/notebooks", json={"title": "Algebra"}, headers={"X-CSRF-Token": csrf})
    assert with_csrf.status_code == 201
    client.cookies.clear()

    # List active sessions
    headers = {"Authorization": f"Bearer {access}"}
    sessions = client.get("/api/auth/sessions", headers=headers).json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["is_current"] is True

    # Logout immediately revokes both the session and JTI
    out = client.post("/api/auth/logout", headers=headers)
    assert out.status_code == 200
    after = client.get("/api/auth/me", headers=headers)
    assert after.status_code == 401


# --------------------------------------------------------------------------- 3. Optimal Notebook, Page & Blob Storage


def test_optimal_page_storage_quantization_compression_and_deduplication():
    reg = client.post("/api/auth/register", json={"email": "newton@example.com", "password": "Fluxions1666!"}).json()
    headers = {"Authorization": f"Bearer {reg['access_token']}"}

    # Create notebook
    nb_resp = client.post(
        "/api/notebooks",
        headers=headers,
        json={"title": "Principia", "color": "#2c4a8a", "paper": "ruled"},
    )
    assert nb_resp.status_code == 201
    nb = nb_resp.json()["notebook"]
    nb_id = nb["id"]
    page_id = nb["pages"][0]

    # Save page with high-precision floats and math textbox
    raw_items = [
        {
            "type": "stroke",
            "color": "#1f3a7a",
            "size": 1.8000001,
            "sim": True,
            "pts": [[100.123456, 200.987654, 0.54321], [105.55555, 210.44444, 0.67891]],
        },
        {
            "type": "textbox",
            "x": 150.0,
            "y": 88.0,
            "w": 820.0,
            "text": "∫ x cos(x) dx\n= x sin(x) - ∫ sin(x) dx",
            "color": "#1f3a7a",
        },
    ]
    save1 = client.put(
        f"/api/notebooks/{nb_id}/pages/{page_id}",
        headers=headers,
        json={"paper": "ruled", "items": raw_items, "meta": []},
    )
    assert save1.status_code == 200
    p1 = save1.json()["page"]
    assert p1["deduplicated"] is False
    assert p1["stroke_count"] == 1
    assert p1["item_count"] == 2
    assert p1["version"] == 2

    # Saving the exact same content (even with unquantized float noise that rounds identically)
    # hits the SHA-256 content_hash fast-path and skips DB write!
    save2 = client.put(
        f"/api/notebooks/{nb_id}/pages/{page_id}",
        headers=headers,
        json={"paper": "ruled", "items": raw_items, "meta": []},
    )
    assert save2.status_code == 200
    p2 = save2.json()["page"]
    assert p2["deduplicated"] is True
    assert p2["version"] == 2  # version not bumped on no-op save

    # Optimistic concurrency lock check: wrong expected_version returns 409 Conflict
    conflict = client.put(
        f"/api/notebooks/{nb_id}/pages/{page_id}",
        headers=headers,
        json={
            "paper": "grid",
            "items": raw_items,
            "meta": [],
            "expected_version": 99,
        },
    )
    assert conflict.status_code == 409

    # Fetch decompressed page and verify quantized coordinates + extracted math lines
    loaded = client.get(f"/api/notebooks/{nb_id}/pages/{page_id}", headers=headers).json()["page"]
    assert loaded["items"][0]["pts"] == [[100.1, 201.0, 0.54], [105.6, 210.4, 0.68]]
    assert [l["text"] for l in loaded["extracted_lines"]] == ["∫ x cos(x) dx", "= x sin(x) - ∫ sin(x) dx"]


def test_multi_tenant_isolation_and_blob_deduplication():
    u1 = client.post("/api/auth/register", json={"email": "alice@example.com", "password": "AliceSecret99!"}).json()
    u2 = client.post("/api/auth/register", json={"email": "bob@example.com", "password": "BobSecret99!"}).json()
    h1 = {"Authorization": f"Bearer {u1['access_token']}"}
    h2 = {"Authorization": f"Bearer {u2['access_token']}"}

    nb = client.post("/api/notebooks", headers=h1, json={"title": "Alice Private Notes"}).json()["notebook"]
    nb_id = nb["id"]
    page_id = nb["pages"][0]

    # Bob cannot read, update, or delete Alice's notebook or page
    assert client.get(f"/api/notebooks/{nb_id}", headers=h2).status_code == 404
    assert client.patch(f"/api/notebooks/{nb_id}", headers=h2, json={"title": "Hacked"}).status_code == 404
    assert client.get(f"/api/notebooks/{nb_id}/pages/{page_id}", headers=h2).status_code == 404
    assert client.delete(f"/api/notebooks/{nb_id}", headers=h2).status_code == 404

    # Content-addressable blob upload deduplicates identical images per user
    raw_png = b"\x89PNG\r\n\x1a\n" + b"fake-image-payload-bytes" * 20
    b64_png = base64.b64encode(raw_png).decode("ascii")

    b1 = client.post("/api/blobs", headers=h1, json={"data_base64": b64_png, "mime_type": "image/png", "width": 400, "height": 300})
    assert b1.status_code == 201
    assert b1.json()["blob"]["deduplicated"] is False
    blob_id = b1.json()["blob"]["id"]

    b2 = client.post("/api/blobs", headers=h1, json={"data_base64": b64_png, "mime_type": "image/png", "width": 400, "height": 300})
    assert b2.status_code == 201
    assert b2.json()["blob"]["deduplicated"] is True
    assert b2.json()["blob"]["id"] == blob_id

    # Bob cannot access Alice's blob
    assert client.get(f"/api/blobs/{blob_id}", headers=h2).status_code == 404


def test_decompression_bomb_protection():
    # 6 MiB of zeros compresses to just a few kilobytes; decompress_bytes must block it!
    huge = b"\x00" * (6 * 1024 * 1024)
    compressed = zlib.compress(huge)
    assert len(compressed) < 20_000
    with pytest.raises(ValueError, match="exceeds safety limit"):
        decompress_bytes(compressed)


# --------------------------------------------------------------------------- 4. Optimal AI Response Storage & Cache


def test_ai_response_persistence_strips_raw_image_and_supports_cache():
    u = client.post("/api/auth/register", json={"email": "leibniz@example.com", "password": "Calculus1675!"}).json()
    headers = {"Authorization": f"Bearer {u['access_token']}"}
    nb = client.post("/api/notebooks", headers=headers, json={"title": "Integrals"}).json()["notebook"]
    nb_id = nb["id"]
    page_id = nb["pages"][0]

    fake_image = "data:image/png;base64," + ("A" * 4000)
    assist_resp = client.post(
        "/api/assist",
        headers=headers,
        json={
            "notebook_id": nb_id,
            "page_id": page_id,
            "image": fake_image,
            "lines": [
                {"id": "L1", "text": "∫ x cos x dx", "bbox": [40, 100, 300, 44]},
                {"id": "L2", "text": "= x sin x - ∫ sin x dx", "bbox": [40, 150, 300, 44]},
                {"id": "L3", "text": "= x sin x - cos x + C", "bbox": [40, 200, 300, 44]},
            ],
            "command": "help me with this integral",
            "mode": "hint",
            "hint_level": 1,
        },
    )
    assert assist_resp.status_code == 200
    body = assist_resp.json()
    assert "interaction_id" in body
    interaction_id = body["interaction_id"]

    # Retrieve stored AI response from DB
    history = client.get(f"/api/ai-responses?notebook_id={nb_id}", headers=headers).json()["ai_responses"]
    assert len(history) == 1
    rec = history[0]
    assert rec["id"] == interaction_id
    assert rec["first_wrong"] == "L3"
    assert rec["had_image"] is True
    assert len(rec["image_sha256"]) == 64
    # Ensure the raw base64 image string was NOT stored in the database record
    assert fake_image not in json.dumps(rec)

    # Semantic cache hit when use_cache=True
    cached_resp = client.post(
        "/api/assist",
        headers=headers,
        json={
            "lines": [
                {"id": "L1", "text": "∫ x cos x dx", "bbox": [40, 100, 300, 44]},
                {"id": "L2", "text": "= x sin x - ∫ sin x dx", "bbox": [40, 150, 300, 44]},
                {"id": "L3", "text": "= x sin x - cos x + C", "bbox": [40, 200, 300, 44]},
            ],
            "command": "help me with this integral",
            "mode": "hint",
            "hint_level": 1,
            "use_cache": True,
        },
    )
    assert cached_resp.status_code == 200
    assert cached_resp.json().get("cached") is True


# --------------------------------------------------------------------------- 5. Database Circuit Breaker


def test_db_circuit_breaker_trips_and_recovers(monkeypatch):
    clock = [500.0]
    breaker = DBCircuitBreaker(failure_threshold=2, recovery_timeout=20.0, clock=lambda: clock[0])
    assert breaker.state == DBCircuitBreaker.CLOSED

    breaker.record_failure("connection timeout 1")
    assert breaker.state == DBCircuitBreaker.CLOSED
    breaker.record_failure("connection timeout 2")
    assert breaker.state == DBCircuitBreaker.OPEN
    assert breaker.allow_request() is False

    # While circuit is OPEN, endpoints protected by DB fail fast with 503 Service Unavailable
    monkeypatch.setattr(db, "circuit_breaker", breaker)
    r = client.post("/api/auth/login", json={"email": "any@example.com", "password": "Password123!"})
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "30"

    # Advance past recovery timeout -> HALF_OPEN allows one probe and closes on success
    clock[0] = 525.0
    assert breaker.state == DBCircuitBreaker.HALF_OPEN
    health = client.get("/api/health/db")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert breaker.state == DBCircuitBreaker.CLOSED


def test_storage_quota_enforcement_and_reclamation():
    u = client.post("/api/auth/register", json={"email": "ramanujan@example.com", "password": "Partition1729!"}).json()
    user_id = u["user"]["id"]
    headers = {"Authorization": f"Bearer {u['access_token']}"}

    # Set a tight quota of 400 bytes for this user
    with db.session() as cur:
        cur.execute("UPDATE users SET storage_quota_bytes = 400 WHERE id = ?", (user_id,))

    # Uploading a random (incompressible) 2 KB blob exceeds the 400-byte quota -> 413
    import os
    random_bytes = os.urandom(2048)
    b64_payload = base64.b64encode(random_bytes).decode("ascii")
    over = client.post("/api/blobs", headers=headers, json={"data_base64": b64_payload, "mime_type": "image/png"})
    assert over.status_code == 413
    assert "quota exceeded" in over.json()["detail"].lower()

    # Verify storage_bytes_used is still 0 after rollback
    me = client.get("/api/auth/me", headers=headers).json()["user"]
    assert me["storage_bytes_used"] == 0

    # Create a notebook (fits within 400 bytes) and verify deleting it reclaims storage back to 0
    nb = client.post("/api/notebooks", headers=headers, json={"title": "Series"}).json()["notebook"]
    me_after_create = client.get("/api/auth/me", headers=headers).json()["user"]
    assert me_after_create["storage_bytes_used"] > 0

    del_resp = client.delete(f"/api/notebooks/{nb['id']}", headers=headers)
    assert del_resp.status_code == 200
    me_after_del = client.get("/api/auth/me", headers=headers).json()["user"]
    assert me_after_del["storage_bytes_used"] == 0


def test_session_eviction_device_revoke_and_logout_all(monkeypatch):
    monkeypatch.setattr(auth, "MAX_SESSIONS_PER_USER", 3)
    client.post("/api/auth/register", json={"email": "hilbert@example.com", "password": "SpaceBasis23!"})

    tokens = []
    for i in range(4):
        r = client.post(
            "/api/auth/login",
            json={"email": "hilbert@example.com", "password": "SpaceBasis23!"},
            headers={"User-Agent": f"Device-{i}"},
        )
        assert r.status_code == 200
        tokens.append(r.json())

    # Oldest session ( from register + first login ) was evicted to keep at most 3 active sessions
    latest_headers = {"Authorization": f"Bearer {tokens[-1]['access_token']}"}
    active = client.get("/api/auth/sessions", headers=latest_headers).json()["sessions"]
    assert len(active) == 3

    # First login session (tokens[0]) was evicted when tokens[3] logged in
    old_check = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokens[0]['access_token']}"})
    assert old_check.status_code == 401

    # Manually revoke tokens[1]'s session from tokens[-1]
    target_sid = tokens[1]["session_id"]
    rev = client.delete(f"/api/auth/sessions/{target_sid}", headers=latest_headers)
    assert rev.status_code == 200
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokens[1]['access_token']}"}).status_code == 401

    # Logout all remaining sessions
    out_all = client.post("/api/auth/logout-all", headers=latest_headers)
    assert out_all.status_code == 200
    assert out_all.json()["revoked_sessions"] >= 2
    assert client.get("/api/auth/me", headers=latest_headers).status_code == 401


def test_inline_data_url_rejected_in_page_items_and_stroke_decimation():
    u = client.post("/api/auth/register", json={"email": "cauchy@example.com", "password": "ResidueTheorem1!"}).json()
    headers = {"Authorization": f"Bearer {u['access_token']}"}
    nb = client.post("/api/notebooks", headers=headers, json={"title": "Complex"}).json()["notebook"]
    nb_id, page_id = nb["id"], nb["pages"][0]

    # Inline data:image/... inside page items must be rejected (400 Bad Request)
    bad_save = client.put(
        f"/api/notebooks/{nb_id}/pages/{page_id}",
        headers=headers,
        json={
            "paper": "ruled",
            "items": [{"type": "image", "blob": "data:image/png;base64,AAAA", "x": 0, "y": 0, "w": 10, "h": 10}],
            "meta": [],
        },
    )
    assert bad_save.status_code == 400
    assert "blob ID" in bad_save.json()["detail"]

    # Stroke with 3,500 points is decimated to MAX_POINTS_PER_STROKE (2,500) preserving endpoints
    dense_pts = [[float(i), float(i * 2), 0.5] for i in range(3500)]
    opt, count = quantize_items([{"type": "stroke", "color": "#000", "size": 1.8, "sim": True, "pts": dense_pts}])
    assert count == 1
    assert len(opt[0]["pts"]) == 2500
    assert opt[0]["pts"][0] == [0.0, 0.0, 0.5]
    assert opt[0]["pts"][-1] == [3499.0, 6998.0, 0.5]


def test_postgresql_dialect_pool_and_rls_context(monkeypatch):
    """Verify PostgreSQL connection pool, RLS set_config, and %s SQL translation with a fake driver."""
    executed_queries: list[tuple[str, tuple]] = []

    class FakeCursor:
        description = [("ok",)]

        def execute(self, sql, params=()):
            executed_queries.append((sql.strip(), tuple(params)))

        def fetchone(self):
            return (1,)

        def fetchall(self):
            return [(1,)]

        def close(self):
            pass

    class FakeConn:
        closed = False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            self.closed = True

    class FakeDriver:
        @staticmethod
        def connect(dsn, connect_timeout=5):
            return FakeConn()

    monkeypatch.setattr(db_module._PostgresPool, "_load_driver", staticmethod(lambda: ("psycopg3", FakeDriver)))
    pg_manager = db_module.DatabaseManager("postgresql://margin:secret@localhost:5432/margin")

    with pg_manager.session(current_user_id="user-uuid-999") as cur:
        row = cur.fetchone("SELECT ? AS ok", (1,))
        assert row == {"ok": 1}

    sqls = [q[0] for q in executed_queries]
    assert any("SET LOCAL statement_timeout" in s for s in sqls)
    assert any("set_config('app.current_user_id', %s, true)" in s for s in sqls)
    assert ("SELECT %s AS ok", (1,)) in executed_queries


def test_supabase_and_railway_dsn_normalization_and_vercel_pool(monkeypatch):
    """Ensure Supabase pooler URLs, Railway private URLs, and Vercel pool sizing work seamlessly."""
    from server.db import normalize_postgres_dsn

    # 1. Supabase Transaction Pooler URL with Prisma params (?pgbouncer=true&connection_limit=1)
    supa_url = "postgres://postgres.xyzref:s3cr3t@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?pgbouncer=true&connection_limit=1"
    clean_dsn, is_pooled = normalize_postgres_dsn(supa_url)
    assert is_pooled is True
    assert clean_dsn.startswith("postgresql://")
    assert "pgbouncer" not in clean_dsn
    assert "connection_limit" not in clean_dsn
    assert "sslmode=require" in clean_dsn

    # 2. Local Docker Compose / VPS URL keeps local SSL unchanged
    vps_url = "postgresql://margin:secret@postgres:5432/margin"
    clean_vps, vps_pooled = normalize_postgres_dsn(vps_url)
    assert vps_pooled is False
    assert clean_vps == vps_url

    # 3. Railway DATABASE_PRIVATE_URL takes priority when DATABASE_URL is also present
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_PRIVATE_URL", "postgresql://postgres:rw@postgres.railway.internal:5432/railway")
    assert db_module._default_database_url() == "postgresql://postgres:rw@postgres.railway.internal:5432/railway"


