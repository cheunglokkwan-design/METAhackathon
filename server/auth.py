"""Security, Authentication, Access Control, JWT & Stateful Session Manager.

Implements OWASP ASVS best practices:
1. Memory-hard password hashing (hashlib.scrypt with PBKDF2-HMAC-SHA256 fallback)
   + optional server-side HMAC pepper + timing-attack dummy verification.
2. RFC 7519 HS256 JWT Access Tokens with strict header/algorithm/claim validation
   (blocks 'alg: none', key confusion, expired/future tokens, and revoked JTIs).
3. Stateful Session Manager in PostgreSQL/SQLite with:
   - Cryptographic 256-bit Refresh Tokens (stored only as SHA-256 hashes)
   - Refresh Token Rotation & Reuse Detection (compromised token replay immediately
     revokes the entire session family and logs a security alert)
   - Concurrent device session cap (max 10 active sessions per user)
   - Immediate session & JTI revocation on logout / device removal
4. Brute-force account lockout (5 failed attempts -> 15m lockout) + per-IP sliding-window
   rate limiting.
5. Dual transport support: Authorization: Bearer <jwt> AND HttpOnly cookies with
   double-submit / session-bound X-CSRF-Token verification.
"""

from __future__ import annotations

import base64
from collections import deque
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Any
import uuid

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import DatabaseManager, DatabaseUnavailableError, db as default_db

log = logging.getLogger("margin.auth")

# --------------------------------------------------------------------------- constants

JWT_ISSUER = "margin-auth"
JWT_AUDIENCE = "margin-app"
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "900"))          # 15 minutes
REFRESH_TOKEN_TTL_SECONDS = int(os.getenv("REFRESH_TOKEN_TTL_SECONDS", "604800"))     # 7 days
MAX_SESSIONS_PER_USER = int(os.getenv("MAX_SESSIONS_PER_USER", "10"))
MAX_FAILED_LOGIN_ATTEMPTS = int(os.getenv("MAX_FAILED_LOGIN_ATTEMPTS", "5"))
ACCOUNT_LOCKOUT_SECONDS = int(os.getenv("ACCOUNT_LOCKOUT_SECONDS", "900"))            # 15 minutes

ACCESS_COOKIE_NAME = "margin_access"
REFRESH_COOKIE_NAME = "margin_refresh"

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}$")
_DEV_FALLBACK_SECRET = secrets.token_urlsafe(48)


class AuthError(Exception):
    """Base authentication / authorization error."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# --------------------------------------------------------------------------- keyed rate limiter


class KeyedRateLimiter:
    """Thread-safe per-key (IP or email) sliding-window rate limiter with bounded memory."""

    def __init__(self, max_calls: int = 10, period: float = 60.0, max_keys: int = 5000) -> None:
        self._default_max = max_calls
        self._default_period = period
        self._max_keys = max_keys
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, max_calls: int | None = None, period: float | None = None) -> bool:
        limit = max_calls if max_calls is not None else int(os.getenv("AUTH_RATE_LIMIT_CALLS", str(self._default_max)))
        window = period if period is not None else float(os.getenv("AUTH_RATE_LIMIT_PERIOD", str(self._default_period)))
        if limit <= 0:
            return False
        now = time.monotonic()
        norm_key = (key or "anon")[:128]
        with self._lock:
            if len(self._buckets) >= self._max_keys and norm_key not in self._buckets:
                # Evict expired or oldest bucket to prevent memory exhaustion under spoofed-IP floods
                oldest_k = next(iter(self._buckets))
                self._buckets.pop(oldest_k, None)
            q = self._buckets.setdefault(norm_key, deque())
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


auth_rate_limiter = KeyedRateLimiter(max_calls=15, period=60.0)
write_rate_limiter = KeyedRateLimiter(max_calls=120, period=60.0)


# --------------------------------------------------------------------------- password hashing


def _pepper_password(password: str) -> bytes:
    raw = password.encode("utf-8")
    pepper = os.getenv("PASSWORD_PEPPER", "").strip()
    if pepper:
        return hmac.new(pepper.encode("utf-8"), raw, hashlib.sha256).digest()
    return raw


def validate_password_strength(password: str) -> None:
    """Enforce OWASP password hygiene without arbitrary composition hostility."""
    if not isinstance(password, str) or "\x00" in password:
        raise AuthError("Invalid password format", status_code=400)
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters long", status_code=400)
    if len(password) > 128:
        raise AuthError("Password must not exceed 128 characters", status_code=400)
    has_letter = any(c.isalpha() for c in password)
    has_digit_or_sym = any(not c.isalpha() and not c.isspace() for c in password)
    if not (has_letter and has_digit_or_sym):
        raise AuthError("Password must contain at least one letter and one number or symbol", status_code=400)


def validate_email(email: str) -> str:
    """Normalize and validate an email address."""
    clean = (email or "").strip().lower()
    if not clean or len(clean) > 254 or not _EMAIL_RE.match(clean):
        raise AuthError("Please provide a valid email address", status_code=400)
    return clean


def hash_password(password: str) -> str:
    """Hash a password using memory-hard scrypt (or PBKDF2-SHA256 fallback)."""
    salt = secrets.token_bytes(16)
    peppered = _pepper_password(password)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    if hasattr(hashlib, "scrypt"):
        n, r, p = 16384, 8, 1
        dk = hashlib.scrypt(peppered, salt=salt, n=n, r=r, p=p, maxmem=32 * 1024 * 1024, dklen=32)
        dk_b64 = base64.urlsafe_b64encode(dk).decode("ascii").rstrip("=")
        return f"$scrypt$n={n},r={r},p={p}${salt_b64}${dk_b64}"
    iterations = 600_000
    dk = hashlib.pbkdf2_hmac("sha256", peppered, salt, iterations, dklen=32)
    dk_b64 = base64.urlsafe_b64encode(dk).decode("ascii").rstrip("=")
    return f"$pbkdf2-sha256$i={iterations}${salt_b64}${dk_b64}"


def _b64u_decode(data: str) -> bytes:
    pad = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + pad)


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time verification of a password against a PHC-formatted hash string."""
    if not password or not stored_hash or len(password) > 128 or "\x00" in password:
        return False
    try:
        parts = stored_hash.split("$")
        if len(parts) != 5 or parts[0] != "":
            return False
        _, alg, params_str, salt_b64, expected_b64 = parts
        salt = _b64u_decode(salt_b64)
        expected = _b64u_decode(expected_b64)
        peppered = _pepper_password(password)

        if alg == "scrypt":
            kv = dict(item.split("=") for item in params_str.split(","))
            n, r, p = int(kv["n"]), int(kv["r"]), int(kv["p"])
            dk = hashlib.scrypt(peppered, salt=salt, n=n, r=r, p=p, maxmem=32 * 1024 * 1024, dklen=len(expected))
            return hmac.compare_digest(dk, expected)
        if alg == "pbkdf2-sha256":
            kv = dict(item.split("=") for item in params_str.split(","))
            iterations = int(kv["i"])
            dk = hashlib.pbkdf2_hmac("sha256", peppered, salt, iterations, dklen=len(expected))
            return hmac.compare_digest(dk, expected)
        return False
    except Exception:
        return False


# Precomputed dummy hash so missing-user logins take the same CPU time as valid-user logins
_DUMMY_PASSWORD_HASH = hash_password("DummyTimingDefense#2026!")


def verify_dummy_password(password: str) -> None:
    """Run constant-time password KDF against dummy hash to prevent user enumeration timing."""
    verify_password(password[:128] if password else "dummy", _DUMMY_PASSWORD_HASH)


# --------------------------------------------------------------------------- RFC 7519 JWT (HS256)


def _jwt_secret() -> bytes:
    configured = os.getenv("JWT_SECRET_KEY", "").strip()
    if configured:
        if os.getenv("ENV", "").lower() == "production" and len(configured) < 32:
            raise RuntimeError("JWT_SECRET_KEY must be at least 32 characters in production")
        return configured.encode("utf-8")
    if os.getenv("ENV", "").lower() == "production":
        raise RuntimeError("JWT_SECRET_KEY environment variable is required in production")
    return _DEV_FALLBACK_SECRET.encode("utf-8")


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def create_access_token(
    *,
    user_id: str,
    session_id: str,
    role: str = "student",
    ttl_seconds: int | None = None,
    now: float | None = None,
) -> tuple[str, str, float]:
    """Mint a signed HS256 JWT access token. Returns (jwt_token, jti, expires_at)."""
    issued_at = float(now if now is not None else time.time())
    ttl = ttl_seconds if ttl_seconds is not None else ACCESS_TOKEN_TTL_SECONDS
    expires_at = issued_at + ttl
    jti = str(uuid.uuid4())

    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": user_id,
        "sid": session_id,
        "jti": jti,
        "role": role,
        "type": "access",
        "iat": int(issued_at),
        "nbf": int(issued_at),
        "exp": int(expires_at),
    }
    h_b64 = _b64u_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    p_b64 = _b64u_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = hmac.new(_jwt_secret(), signing_input, hashlib.sha256).digest()
    token = f"{h_b64}.{p_b64}.{_b64u_encode(sig)}"
    return token, jti, expires_at


def decode_and_verify_jwt(token: str, now: float | None = None) -> dict[str, Any]:
    """Verify HS256 JWT signature, algorithm header, and standard claims."""
    if not token or not isinstance(token, str) or len(token) > 4096:
        raise AuthError("Invalid authentication token")
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("Malformed JWT token")
    h_b64, p_b64, s_b64 = parts

    try:
        header = json.loads(_b64u_decode(h_b64).decode("utf-8"))
    except Exception as exc:
        raise AuthError("Malformed JWT header") from exc

    # Explicitly enforce HS256 to defeat 'alg: none' and algorithm confusion attacks
    if not isinstance(header, dict) or header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise AuthError("Unsupported JWT algorithm or header")

    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    expected_sig = hmac.new(_jwt_secret(), signing_input, hashlib.sha256).digest()
    try:
        actual_sig = _b64u_decode(s_b64)
    except Exception as exc:
        raise AuthError("Malformed JWT signature") from exc

    if not hmac.compare_digest(actual_sig, expected_sig):
        raise AuthError("Invalid JWT signature")

    try:
        claims = json.loads(_b64u_decode(p_b64).decode("utf-8"))
    except Exception as exc:
        raise AuthError("Malformed JWT claims") from exc

    if not isinstance(claims, dict):
        raise AuthError("Invalid JWT payload")

    current_time = float(now if now is not None else time.time())
    if claims.get("iss") != JWT_ISSUER or claims.get("aud") != JWT_AUDIENCE:
        raise AuthError("Invalid token issuer or audience")
    if claims.get("type") != "access":
        raise AuthError("Invalid token type")

    exp = claims.get("exp")
    nbf = claims.get("nbf", 0)
    if not isinstance(exp, (int, float)) or current_time >= float(exp):
        raise AuthError("Access token has expired")
    if isinstance(nbf, (int, float)) and current_time + 5 < float(nbf):
        raise AuthError("Token is not yet valid")

    if not claims.get("sub") or not claims.get("sid") or not claims.get("jti"):
        raise AuthError("Token missing required subject or session claims")

    return claims


# --------------------------------------------------------------------------- Session & User Manager


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class SessionManager:
    """Stateful session, user authentication, and refresh-token rotation manager."""

    def __init__(self, database: DatabaseManager | None = None) -> None:
        self._db = database

    @property
    def db(self) -> DatabaseManager:
        return self._db or default_db

    # ---- user registration & login -------------------------------------------------

    def register_user(
        self,
        *,
        email: str,
        password: str,
        display_name: str = "",
        ip_address: str = "",
        user_agent: str = "",
    ) -> dict[str, Any]:
        clean_email = validate_email(email)
        validate_password_strength(password)
        clean_name = (display_name or clean_email.split("@")[0]).strip()[:80]
        pw_hash = hash_password(password)
        user_id = str(uuid.uuid4())
        now = time.time()

        try:
            with self.db.session(current_user_id=user_id) as cur:
                existing = cur.fetchone("SELECT id FROM users WHERE LOWER(email) = ?", (clean_email,))
                if existing:
                    duplicate = True
                    bundle = {}
                else:
                    duplicate = False
                    cur.execute(
                        """
                        INSERT INTO users (
                            id, email, display_name, password_hash, role, is_active,
                            failed_login_attempts, locked_until, storage_bytes_used, storage_quota_bytes,
                            created_at, updated_at, last_login_at
                        )
                        VALUES (?, ?, ?, ?, 'student', ?, 0, 0, 0, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            clean_email,
                            clean_name,
                            pw_hash,
                            True if self.db.dialect == "postgresql" else 1,
                            50 * 1024 * 1024,
                            now,
                            now,
                            now,
                        ),
                    )
                    bundle = self._create_session_in_tx(
                        cur,
                        user_id=user_id,
                        role="student",
                        ip_address=ip_address,
                        user_agent=user_agent,
                        now=now,
                    )
                    self.db.log_audit_event(
                        "USER_REGISTERED",
                        user_id=user_id,
                        session_id=bundle["session_id"],
                        ip_address=ip_address,
                        user_agent=user_agent,
                        details={"email": clean_email},
                        cursor=cur,
                    )
        except sqlite3.IntegrityError as exc:
            raise AuthError("An account with this email already exists", status_code=409) from exc

        if duplicate:
            raise AuthError("An account with this email already exists", status_code=409)

        return {
            "user": {
                "id": user_id,
                "email": clean_email,
                "display_name": clean_name,
                "role": "student",
                "storage_bytes_used": 0,
                "storage_quota_bytes": 50 * 1024 * 1024,
            },
            **bundle,
        }

    def authenticate_user(
        self,
        *,
        email: str,
        password: str,
        ip_address: str = "",
        user_agent: str = "",
    ) -> dict[str, Any]:
        clean_email = validate_email(email)
        now = time.time()
        auth_err: AuthError | None = None
        bundle: dict[str, Any] = {}

        with self.db.session() as cur:
            user = cur.fetchone(
                """
                SELECT id, email, display_name, password_hash, role, is_active,
                       failed_login_attempts, locked_until, storage_bytes_used, storage_quota_bytes
                FROM users
                WHERE LOWER(email) = ?
                """,
                (clean_email,),
            )
            if not user:
                # Constant-time dummy KDF prevents email enumeration via timing side-channel
                verify_dummy_password(password)
                self.db.log_audit_event(
                    "LOGIN_FAILED_UNKNOWN_USER",
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details={"email": clean_email},
                    cursor=cur,
                )
                auth_err = AuthError("Invalid email or password", status_code=401)
            else:
                user_id = user["id"]
                if not bool(user["is_active"]):
                    auth_err = AuthError("Account is disabled", status_code=403)
                else:
                    locked_until = float(user.get("locked_until") or 0.0)
                    if locked_until > now:
                        remaining = int(locked_until - now)
                        self.db.log_audit_event(
                            "LOGIN_BLOCKED_LOCKED",
                            user_id=user_id,
                            ip_address=ip_address,
                            user_agent=user_agent,
                            details={"remaining_seconds": remaining},
                            cursor=cur,
                        )
                        auth_err = AuthError(
                            f"Account is temporarily locked due to failed login attempts. Try again in {remaining}s.",
                            status_code=429,
                        )
                    elif not verify_password(password, user["password_hash"]):
                        attempts = int(user.get("failed_login_attempts") or 0) + 1
                        new_lock = (now + ACCOUNT_LOCKOUT_SECONDS) if attempts >= MAX_FAILED_LOGIN_ATTEMPTS else 0.0
                        cur.execute(
                            "UPDATE users SET failed_login_attempts = ?, locked_until = ?, updated_at = ? WHERE id = ?",
                            (attempts, new_lock, now, user_id),
                        )
                        event = "ACCOUNT_LOCKED" if new_lock > 0 else "LOGIN_FAILED_BAD_PASSWORD"
                        self.db.log_audit_event(
                            event,
                            user_id=user_id,
                            ip_address=ip_address,
                            user_agent=user_agent,
                            details={"attempts": attempts},
                            cursor=cur,
                        )
                        if new_lock > 0:
                            auth_err = AuthError(
                                f"Too many failed attempts. Account locked for {ACCOUNT_LOCKOUT_SECONDS // 60} minutes.",
                                status_code=429,
                            )
                        else:
                            auth_err = AuthError("Invalid email or password", status_code=401)
                    else:
                        # Successful login: reset lockout counters & create new session
                        cur.execute(
                            "UPDATE users SET failed_login_attempts = 0, locked_until = 0, last_login_at = ?, updated_at = ? WHERE id = ?",
                            (now, now, user_id),
                        )
                        bundle = self._create_session_in_tx(
                            cur,
                            user_id=user_id,
                            role=user["role"],
                            ip_address=ip_address,
                            user_agent=user_agent,
                            now=now,
                        )
                        self.db.log_audit_event(
                            "LOGIN_SUCCESS",
                            user_id=user_id,
                            session_id=bundle["session_id"],
                            ip_address=ip_address,
                            user_agent=user_agent,
                            cursor=cur,
                        )

        if auth_err is not None:
            raise auth_err
        assert user is not None

        return {
            "user": {
                "id": user["id"],
                "email": user["email"],
                "display_name": user["display_name"],
                "role": user["role"],
                "storage_bytes_used": int(user.get("storage_bytes_used") or 0),
                "storage_quota_bytes": int(user.get("storage_quota_bytes") or 50 * 1024 * 1024),
            },
            **bundle,
        }

    # ---- session creation, rotation & reuse detection ------------------------------

    @staticmethod
    def _infer_device_name(user_agent: str) -> str:
        ua = (user_agent or "").lower()
        if "ipad" in ua:
            return "iPad"
        if "iphone" in ua:
            return "iPhone"
        if "macintosh" in ua or "mac os" in ua:
            return "Mac"
        if "android" in ua:
            return "Android"
        if "windows" in ua:
            return "Windows PC"
        if "linux" in ua:
            return "Linux"
        return "Web Browser"

    def _create_session_in_tx(
        self,
        cur: Any,
        *,
        user_id: str,
        role: str,
        ip_address: str,
        user_agent: str,
        now: float,
    ) -> dict[str, Any]:
        # Prune expired revoked_tokens and enforce max concurrent sessions per user
        cur.execute("DELETE FROM revoked_tokens WHERE expires_at < ?", (now,))
        active_rows = cur.fetchall(
            """
            SELECT id FROM user_sessions
            WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
            ORDER BY last_active_at ASC
            """,
            (user_id, now),
        )
        if len(active_rows) >= MAX_SESSIONS_PER_USER:
            to_evict = active_rows[: len(active_rows) - MAX_SESSIONS_PER_USER + 1]
            for r in to_evict:
                cur.execute(
                    "UPDATE user_sessions SET revoked_at = ?, revoke_reason = 'max_sessions_exceeded' WHERE id = ?",
                    (now, r["id"]),
                )

        session_id = str(uuid.uuid4())
        family_id = str(uuid.uuid4())
        raw_refresh = f"{session_id}.{secrets.token_urlsafe(32)}"
        refresh_hash = _hash_token(raw_refresh)
        csrf_token = secrets.token_urlsafe(24)
        csrf_hash = _hash_token(csrf_token)
        expires_at = now + REFRESH_TOKEN_TTL_SECONDS
        device_name = self._infer_device_name(user_agent)

        cur.execute(
            """
            INSERT INTO user_sessions (
                id, user_id, family_id, refresh_token_hash, previous_token_hash, csrf_token_hash,
                device_name, ip_address, user_agent, created_at, last_active_at, expires_at
            )
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                family_id,
                refresh_hash,
                csrf_hash,
                device_name,
                (ip_address or "")[:64],
                (user_agent or "")[:256],
                now,
                now,
                expires_at,
            ),
        )
        access_token, jti, access_exp = create_access_token(
            user_id=user_id,
            session_id=session_id,
            role=role,
            now=now,
        )
        return {
            "session_id": session_id,
            "access_token": access_token,
            "access_expires_at": int(access_exp),
            "refresh_token": raw_refresh,
            "refresh_expires_at": int(expires_at),
            "csrf_token": csrf_token,
            "jti": jti,
        }

    def refresh_session(
        self,
        raw_refresh_token: str,
        *,
        ip_address: str = "",
        user_agent: str = "",
    ) -> dict[str, Any]:
        """Rotate a refresh token and issue a fresh JWT access token.

        If an already-rotated refresh token is presented (Token Reuse Detection),
        immediately revokes the entire session family to protect the user!
        """
        if not raw_refresh_token or len(raw_refresh_token) > 512:
            raise AuthError("Invalid refresh token")
        token_hash = _hash_token(raw_refresh_token)
        now = time.time()
        auth_err: AuthError | None = None
        result_bundle: dict[str, Any] = {}

        with self.db.session() as cur:
            session = cur.fetchone(
                """
                SELECT s.id, s.user_id, s.family_id, s.refresh_token_hash, s.previous_token_hash,
                       s.expires_at, s.revoked_at, u.role, u.is_active, u.email, u.display_name
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.refresh_token_hash = ?
                """,
                (token_hash,),
            )

            if not session:
                # Check if this token was ALREADY rotated (Token Reuse Detection!)
                reused = cur.fetchone(
                    "SELECT id, user_id, family_id FROM user_sessions WHERE previous_token_hash = ?",
                    (token_hash,),
                )
                if reused:
                    cur.execute(
                        """
                        UPDATE user_sessions
                        SET revoked_at = ?, revoke_reason = 'token_reuse_detected'
                        WHERE family_id = ? AND revoked_at IS NULL
                        """,
                        (now, reused["family_id"]),
                    )
                    self.db.log_audit_event(
                        "TOKEN_REUSE_DETECTED",
                        user_id=reused["user_id"],
                        session_id=reused["id"],
                        ip_address=ip_address,
                        user_agent=user_agent,
                        details={"family_id": reused["family_id"]},
                        cursor=cur,
                    )
                    log.warning("Refresh token reuse detected for user %s; revoked family %s", reused["user_id"], reused["family_id"])
                    auth_err = AuthError("Compromised refresh token detected; session family revoked", status_code=401)
                else:
                    auth_err = AuthError("Invalid or expired refresh token", status_code=401)
            elif session["revoked_at"] is not None or float(session["expires_at"]) <= now:
                auth_err = AuthError("Session has expired or been revoked", status_code=401)
            elif not bool(session["is_active"]):
                auth_err = AuthError("User account is disabled", status_code=403)
            else:
                session_id = session["id"]
                user_id = session["user_id"]
                new_raw_refresh = f"{session_id}.{secrets.token_urlsafe(32)}"
                new_refresh_hash = _hash_token(new_raw_refresh)
                new_csrf = secrets.token_urlsafe(24)
                new_csrf_hash = _hash_token(new_csrf)
                new_expires_at = now + REFRESH_TOKEN_TTL_SECONDS

                cur.execute(
                    """
                    UPDATE user_sessions
                    SET previous_token_hash = refresh_token_hash,
                        refresh_token_hash = ?,
                        csrf_token_hash = ?,
                        last_active_at = ?,
                        expires_at = ?,
                        ip_address = ?,
                        user_agent = ?
                    WHERE id = ?
                    """,
                    (
                        new_refresh_hash,
                        new_csrf_hash,
                        now,
                        new_expires_at,
                        (ip_address or "")[:64],
                        (user_agent or "")[:256],
                        session_id,
                    ),
                )
                access_token, jti, access_exp = create_access_token(
                    user_id=user_id,
                    session_id=session_id,
                    role=session["role"],
                    now=now,
                )
                self.db.log_audit_event(
                    "TOKEN_REFRESHED",
                    user_id=user_id,
                    session_id=session_id,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    cursor=cur,
                )
                result_bundle = {
                    "session_id": session_id,
                    "user_id": user_id,
                    "access_token": access_token,
                    "access_expires_at": int(access_exp),
                    "refresh_token": new_raw_refresh,
                    "refresh_expires_at": int(new_expires_at),
                    "csrf_token": new_csrf,
                    "jti": jti,
                }

        if auth_err is not None:
            raise auth_err
        return result_bundle

    # ---- token & session verification ----------------------------------------------

    def verify_access_request(
        self,
        token: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> dict[str, Any]:
        """Validate JWT access token, check JTI blocklist, and verify active session in DB."""
        claims = decode_and_verify_jwt(token)
        user_id = str(claims["sub"])
        session_id = str(claims["sid"])
        jti = str(claims["jti"])
        now = time.time()

        with self.db.session(current_user_id=user_id) as cur:
            revoked_jti = cur.fetchone("SELECT jti FROM revoked_tokens WHERE jti = ?", (jti,))
            row = (
                None
                if revoked_jti
                else cur.fetchone(
                    """
                    SELECT s.id AS session_id, s.csrf_token_hash, s.revoked_at, s.expires_at,
                           u.id AS user_id, u.email, u.display_name, u.role, u.is_active,
                           u.storage_bytes_used, u.storage_quota_bytes
                    FROM user_sessions s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.id = ? AND s.user_id = ?
                    """,
                    (session_id, user_id),
                )
            )

        if revoked_jti:
            raise AuthError("Access token has been revoked", status_code=401)
        if not row:
            raise AuthError("Session not found", status_code=401)
        if row["revoked_at"] is not None or float(row["expires_at"]) <= now:
            raise AuthError("Session has been revoked or expired", status_code=401)
        if not bool(row["is_active"]):
            raise AuthError("User account is disabled", status_code=403)

        if require_csrf:
            if not csrf_token:
                raise AuthError("Missing X-CSRF-Token header for cookie-authenticated write request", status_code=403)
            expected_csrf_hash = row["csrf_token_hash"]
            if not hmac.compare_digest(_hash_token(csrf_token), expected_csrf_hash):
                raise AuthError("Invalid CSRF token", status_code=403)

        return {
            "id": row["user_id"],
            "email": row["email"],
            "display_name": row["display_name"],
            "role": row["role"],
            "session_id": row["session_id"],
            "jti": jti,
            "exp": int(claims["exp"]),
            "storage_bytes_used": int(row.get("storage_bytes_used") or 0),
            "storage_quota_bytes": int(row.get("storage_quota_bytes") or 50 * 1024 * 1024),
        }

    # ---- session listing & revocation ----------------------------------------------

    def revoke_session(
        self,
        *,
        user_id: str,
        session_id: str,
        jti: str | None = None,
        jti_exp: float | None = None,
        reason: str = "user_logout",
        ip_address: str = "",
        user_agent: str = "",
    ) -> bool:
        now = time.time()
        with self.db.session(current_user_id=user_id) as cur:
            row = cur.fetchone(
                "SELECT id FROM user_sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            )
            if not row:
                return False
            cur.execute(
                "UPDATE user_sessions SET revoked_at = ?, revoke_reason = ? WHERE id = ? AND user_id = ?",
                (now, reason[:64], session_id, user_id),
            )
            if jti:
                cur.execute(
                    """
                    INSERT INTO revoked_tokens (jti, user_id, expires_at, revoked_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (jti) DO NOTHING
                    """,
                    (jti, user_id, float(jti_exp or (now + ACCESS_TOKEN_TTL_SECONDS)), now),
                )
            self.db.log_audit_event(
                "SESSION_REVOKED",
                user_id=user_id,
                session_id=session_id,
                ip_address=ip_address,
                user_agent=user_agent,
                details={"reason": reason},
                cursor=cur,
            )
            return True

    def revoke_all_sessions(
        self,
        *,
        user_id: str,
        current_jti: str | None = None,
        current_jti_exp: float | None = None,
        ip_address: str = "",
        user_agent: str = "",
    ) -> int:
        now = time.time()
        with self.db.session(current_user_id=user_id) as cur:
            active = cur.fetchall(
                "SELECT id FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL",
                (user_id,),
            )
            cur.execute(
                "UPDATE user_sessions SET revoked_at = ?, revoke_reason = 'logout_all' WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            if current_jti:
                cur.execute(
                    """
                    INSERT INTO revoked_tokens (jti, user_id, expires_at, revoked_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (jti) DO NOTHING
                    """,
                    (current_jti, user_id, float(current_jti_exp or (now + ACCESS_TOKEN_TTL_SECONDS)), now),
                )
            self.db.log_audit_event(
                "ALL_SESSIONS_REVOKED",
                user_id=user_id,
                ip_address=ip_address,
                user_agent=user_agent,
                details={"revoked_count": len(active)},
                cursor=cur,
            )
            return len(active)

    def list_active_sessions(self, user_id: str, current_session_id: str | None = None) -> list[dict[str, Any]]:
        now = time.time()
        with self.db.session(current_user_id=user_id) as cur:
            rows = cur.fetchall(
                """
                SELECT id, device_name, ip_address, user_agent, created_at, last_active_at, expires_at
                FROM user_sessions
                WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
                ORDER BY last_active_at DESC
                """,
                (user_id, now),
            )
        return [
            {
                "id": r["id"],
                "device_name": r["device_name"],
                "ip_address": r["ip_address"],
                "user_agent": r["user_agent"],
                "is_current": r["id"] == current_session_id,
                "created": int(float(r["created_at"]) * 1000),
                "last_active": int(float(r["last_active_at"]) * 1000),
                "expires": int(float(r["expires_at"]) * 1000),
            }
            for r in rows
        ]

    def list_audit_logs(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(100, int(limit)))
        with self.db.session(current_user_id=user_id) as cur:
            rows = cur.fetchall(
                """
                SELECT id, session_id, event_type, ip_address, user_agent, details, created_at
                FROM security_audit_logs
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, safe_limit),
            )
        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "event_type": r["event_type"],
                "ip_address": r["ip_address"],
                "user_agent": r["user_agent"],
                "details": json.loads(r.get("details") or "{}"),
                "created": int(float(r["created_at"]) * 1000),
            }
            for r in rows
        ]


session_manager = SessionManager()
_bearer_scheme = HTTPBearer(auto_error=False)


# --------------------------------------------------------------------------- FastAPI dependencies & cookie helpers


def client_ip(request: Request) -> str:
    """Extract client IP safely (respecting Vercel / proxy X-Forwarded-For first hop)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return "127.0.0.1"


def set_auth_cookies(response: Response, bundle: dict[str, Any], request: Request | None = None) -> None:
    """Set hardened HttpOnly session cookies for browser clients."""
    is_https = (
        os.getenv("ENV", "").lower() == "production"
        or (request is not None and request.url.scheme == "https")
        or (request is not None and request.headers.get("x-forwarded-proto") == "https")
    )
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=bundle["access_token"],
        max_age=ACCESS_TOKEN_TTL_SECONDS,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=bundle["refresh_token"],
        max_age=REFRESH_TOKEN_TTL_SECONDS,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/api/auth",
    )


def clear_auth_cookies(response: Response) -> None:
    """Clear authentication cookies on logout."""
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/")
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/auth")


def _extract_user_from_request(
    request: Request,
    creds: HTTPAuthorizationCredentials | None,
    *,
    optional: bool = False,
) -> dict[str, Any] | None:
    token: str | None = None
    via_cookie = False
    if creds and creds.scheme.lower() == "bearer" and creds.credentials:
        token = creds.credentials.strip()
    elif ACCESS_COOKIE_NAME in request.cookies:
        token = request.cookies.get(ACCESS_COOKIE_NAME, "").strip()
        via_cookie = True

    if not token:
        if optional:
            return None
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"})

    require_csrf = via_cookie and request.method.upper() in ("POST", "PUT", "PATCH", "DELETE")
    csrf_header = request.headers.get("x-csrf-token")

    try:
        return session_manager.verify_access_request(
            token,
            csrf_token=csrf_header,
            require_csrf=require_csrf,
        )
    except AuthError as exc:
        if optional:
            return None
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except DatabaseUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable (circuit breaker open)",
            headers={"Retry-After": "30"},
        ) from exc


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict[str, Any]:
    """FastAPI dependency requiring a valid JWT access token and active session."""
    user = _extract_user_from_request(request, creds, optional=False)
    assert user is not None
    if request.method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        if not write_rate_limiter.allow(f"user:{user['id']}"):
            raise HTTPException(status_code=429, detail="Write rate limit exceeded; slow down")
    return user


def get_optional_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict[str, Any] | None:
    """FastAPI dependency returning the authenticated user if present, or None for guest requests."""
    return _extract_user_from_request(request, creds, optional=True)


def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """FastAPI dependency requiring admin role."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user
