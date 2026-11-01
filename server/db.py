"""Production Relational Database Engine, Circuit Breaker & Optimal Storage Layer.

Supports:
- PostgreSQL 16+ (primary production & Docker target; Vercel/Neon/Supabase pooled connections)
- SQLite 3 with WAL & Foreign Keys (automatic zero-config fallback for local dev & CI tests)

Key resilience & anti-overload features:
1. Thread-safe DBCircuitBreaker (closed -> open -> half_open -> closed) with latency telemetry.
2. Bounded connection pool with health checks and statement timeouts.
3. Stroke quantization (1-decimal px, 2-decimal pressure) + point-count bounds.
4. Zlib compression with decompression-bomb protection (max 5 MiB uncompressed).
5. SHA-256 content-hash deduplication on page autosaves (no-op writes when unchanged).
6. Content-addressable media blob deduplication (per-user SHA-256) + storage quota enforcement.
7. Lightweight AI response persistence (hashes 5 MB images to 64-byte SHA-256; bounded history).
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import uuid
import zlib

log = logging.getLogger("margin.db")

# --------------------------------------------------------------------------- limits & constants

DEFAULT_STORAGE_QUOTA_BYTES = 50 * 1024 * 1024        # 50 MiB per user
MAX_PAGE_COMPRESSED_BYTES = 1 * 1024 * 1024           # 1 MiB compressed page cap
MAX_BLOB_BYTES = 4 * 1024 * 1024                      # 4 MiB per media blob
MAX_DECOMPRESSED_BYTES = 5 * 1024 * 1024              # 5 MiB decompression bomb guard
MAX_ITEMS_PER_PAGE = 2000
MAX_POINTS_PER_STROKE = 2500
MAX_TEXT_LEN_PER_ITEM = 10_000
MAX_AI_HISTORY_PER_USER = 500


class DatabaseError(RuntimeError):
    """Base database operational error."""


class DatabaseUnavailableError(DatabaseError):
    """Raised when the database circuit breaker is OPEN or connection fails."""


class StorageQuotaExceededError(DatabaseError):
    """Raised when a user exceeds their allocated storage quota."""


class OptimisticLockError(DatabaseError):
    """Raised when a concurrent update conflict is detected via version mismatch."""


# --------------------------------------------------------------------------- circuit breaker


class DBCircuitBreaker:
    """Thread-safe circuit breaker protecting the relational database from overload."""

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(
        self,
        failure_threshold: int | None = None,
        recovery_timeout: float | None = None,
        slow_query_ms: float | None = None,
        clock: Any = None,
    ) -> None:
        self._threshold = failure_threshold
        self._timeout = recovery_timeout
        self._slow_ms = slow_query_ms
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self.reset()

    def _cfg(self) -> tuple[int, float, float]:
        t = self._threshold if self._threshold is not None else max(1, int(os.getenv("DB_CB_FAILURE_THRESHOLD", "4")))
        r = self._timeout if self._timeout is not None else float(os.getenv("DB_CB_RECOVERY_TIMEOUT", "30.0"))
        s = self._slow_ms if self._slow_ms is not None else float(os.getenv("DB_SLOW_QUERY_MS", "500.0"))
        return t, r, s

    @property
    def state(self) -> str:
        _, timeout, _ = self._cfg()
        with self._lock:
            if self._state == self.OPEN and (self._clock() - self._opened_at) >= timeout:
                return self.HALF_OPEN
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def allow_request(self) -> bool:
        _, timeout, _ = self._cfg()
        now = self._clock()
        with self._lock:
            if self._state == self.CLOSED:
                return True
            if self._state == self.OPEN:
                if (now - self._opened_at) < timeout:
                    return False
                self._state, self._probe = self.HALF_OPEN, True
                return True
            if not self._probe:
                self._probe = True
                return True
            return False

    def record_success(self, latency_ms: float = 0.0) -> None:
        _, _, slow_ms = self._cfg()
        with self._lock:
            self._failure_count = 0
            self._state = self.CLOSED
            self._probe = False
            self._total_queries += 1
            self._latencies.append(latency_ms)
            if latency_ms >= slow_ms:
                self._slow_queries += 1

    def record_failure(self, err: Exception | str | None = None) -> None:
        threshold, _, _ = self._cfg()
        now = self._clock()
        with self._lock:
            self._failure_count += 1
            self._total_failures += 1
            self._last_error = str(err)[:200] if err else "unknown error"
            if self._state == self.HALF_OPEN or self._failure_count >= threshold:
                self._state = self.OPEN
                self._opened_at = now
                self._probe = False
                log.error("DB circuit breaker tripped to OPEN (failures=%d): %s", self._failure_count, self._last_error)

    def metrics(self) -> dict[str, Any]:
        cur_state = self.state
        with self._lock:
            lats = list(self._latencies)
            avg_ms = round(sum(lats) / len(lats), 2) if lats else 0.0
            return {
                "state": cur_state,
                "failure_count": self._failure_count,
                "total_failures": self._total_failures,
                "total_queries": self._total_queries,
                "slow_queries": self._slow_queries,
                "avg_latency_ms": avg_ms,
                "last_error": self._last_error,
            }

    def reset(self) -> None:
        with self._lock:
            self._state = self.CLOSED
            self._failure_count = 0
            self._total_failures = 0
            self._total_queries = 0
            self._slow_queries = 0
            self._opened_at = 0.0
            self._probe = False
            self._last_error: str | None = None
            self._latencies: deque[float] = deque(maxlen=200)


# --------------------------------------------------------------------------- compression & optimization


def compress_bytes(raw: bytes) -> bytes:
    """Zlib-compress raw bytes at balanced level 6."""
    return zlib.compress(raw, level=6)


def decompress_bytes(compressed: bytes | memoryview, max_bytes: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """Safely decompress zlib data with strict decompression-bomb protection."""
    if isinstance(compressed, memoryview):
        compressed = bytes(compressed)
    if not compressed:
        return b""
    decomp = zlib.decompressobj()
    out = decomp.decompress(compressed, max_bytes + 1)
    if len(out) > max_bytes or decomp.unconsumed_tail:
        raise ValueError(f"Decompressed payload exceeds safety limit of {max_bytes} bytes")
    out += decomp.flush()
    if len(out) > max_bytes:
        raise ValueError(f"Decompressed payload exceeds safety limit of {max_bytes} bytes")
    return out


def compress_json(data: Any) -> bytes:
    """Serialize data to compact JSON and zlib-compress it."""
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return compress_bytes(raw)


def decompress_json(compressed: bytes | memoryview | str | None, default: Any = None) -> Any:
    """Decompress zlib JSON payload safely."""
    if not compressed:
        return [] if default is None else default
    if isinstance(compressed, str):
        return json.loads(compressed)
    raw = decompress_bytes(compressed)
    if not raw:
        return [] if default is None else default
    return json.loads(raw.decode("utf-8"))


def _decimate_points(pts: list[Any], max_pts: int = MAX_POINTS_PER_STROKE) -> list[list[float]]:
    """Quantize stroke points [x, y, p] and bound count to prevent stroke-flood DoS."""
    if not isinstance(pts, list) or not pts:
        return []
    if len(pts) > max_pts:
        step = (len(pts) - 1) / float(max_pts - 1)
        sampled = [pts[int(round(i * step))] for i in range(max_pts - 1)] + [pts[-1]]
    else:
        sampled = pts
    out: list[list[float]] = []
    for pt in sampled:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        x = round(float(pt[0]), 1)
        y = round(float(pt[1]), 1)
        p = round(float(pt[2]), 2) if len(pt) >= 3 else 0.5
        out.append([x, y, p])
    return out


def quantize_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Optimize page items for database storage without visual degradation.

    - Quantizes stroke coordinates to 0.1px and pressure to 0.01.
    - Enforces MAX_ITEMS_PER_PAGE and MAX_POINTS_PER_STROKE.
    - Rejects raw inline base64 blobs inside items (must reference media_blobs by id).
    Returns (optimized_items, stroke_count).
    """
    if not isinstance(items, list):
        return [], 0
    if len(items) > MAX_ITEMS_PER_PAGE:
        raise ValueError(f"Page exceeds maximum of {MAX_ITEMS_PER_PAGE} items")

    optimized: list[dict[str, Any]] = []
    stroke_count = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("type", "")).strip()
        if kind == "stroke":
            pts = _decimate_points(it.get("pts") or [])
            if not pts:
                continue
            stroke_count += 1
            entry: dict[str, Any] = {
                "type": "stroke",
                "color": str(it.get("color", "#1f3a7a"))[:32],
                "size": round(float(it.get("size", 1.8)), 2),
                "sim": bool(it.get("sim", True)),
                "pts": pts,
            }
            if it.get("hl"):
                entry["hl"] = True
            optimized.append(entry)
        elif kind == "textbox":
            text = str(it.get("text", ""))[:MAX_TEXT_LEN_PER_ITEM]
            if not text.strip():
                continue
            optimized.append({
                "type": "textbox",
                "x": round(float(it.get("x", 150.0)), 1),
                "y": round(float(it.get("y", 88.0)), 1),
                "w": round(float(it.get("w", 820.0)), 1),
                "text": text,
                "color": str(it.get("color", "#1f3a7a"))[:32],
            })
        elif kind == "image":
            blob_id = str(it.get("blob", "")).strip()
            if not blob_id or blob_id.startswith("data:") or len(blob_id) > 128:
                raise ValueError("Image items must reference a stored blob ID, not inline data URLs")
            img_entry: dict[str, Any] = {
                "type": "image",
                "blob": blob_id,
                "x": round(float(it.get("x", 0.0)), 1),
                "y": round(float(it.get("y", 0.0)), 1),
                "w": round(float(it.get("w", 100.0)), 1),
                "h": round(float(it.get("h", 100.0)), 1),
            }
            if it.get("bg"):
                img_entry["bg"] = True
            optimized.append(img_entry)
    return optimized, stroke_count


def extract_lines_from_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract lightweight searchable math lines from page textboxes."""
    lines: list[dict[str, Any]] = []
    idx = 1
    for it in items:
        if it.get("type") != "textbox":
            continue
        x = float(it.get("x", 150.0))
        y = float(it.get("y", 88.0))
        w = float(it.get("w", 820.0))
        for row_idx, raw_line in enumerate(str(it.get("text", "")).splitlines()):
            t = raw_line.strip()
            if not t:
                continue
            lines.append({
                "id": f"L{idx}",
                "text": t[:500],
                "bbox": [round(x, 1), round(y + row_idx * 44.0, 1), round(w, 1), 44.0],
            })
            idx += 1
    return lines


def compute_content_hash(paper: str, items: list[dict[str, Any]], meta: list[dict[str, Any]]) -> str:
    """Compute canonical SHA-256 digest of a page's state for no-op save deduplication."""
    canonical = json.dumps(
        {"paper": paper, "items": items, "meta": meta},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_prompt_hash(lines: list[dict[str, Any]], command: str, mode: str, hint_level: int) -> str:
    """Compute canonical SHA-256 digest for an AI assist request."""
    norm_lines = [str(l.get("text", "")).strip() for l in lines if str(l.get("text", "")).strip()]
    canonical = json.dumps(
        {
            "lines": norm_lines,
            "command": (command or "").strip().lower(),
            "mode": (mode or "auto").strip().lower(),
            "hint_level": int(hint_level or 1),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# --------------------------------------------------------------------------- schema DDL (SQLite & Postgres)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                      TEXT PRIMARY KEY,
    email                   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name            TEXT NOT NULL DEFAULT '',
    password_hash           TEXT NOT NULL,
    role                    TEXT NOT NULL DEFAULT 'student' CHECK (role IN ('student', 'admin')),
    is_active               INTEGER NOT NULL DEFAULT 1,
    failed_login_attempts   INTEGER NOT NULL DEFAULT 0,
    locked_until            REAL NOT NULL DEFAULT 0,
    storage_bytes_used      INTEGER NOT NULL DEFAULT 0,
    storage_quota_bytes     INTEGER NOT NULL DEFAULT 52428800,
    created_at              REAL NOT NULL,
    updated_at              REAL NOT NULL,
    last_login_at           REAL
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id               TEXT NOT NULL,
    refresh_token_hash      TEXT NOT NULL UNIQUE,
    previous_token_hash     TEXT,
    csrf_token_hash         TEXT NOT NULL,
    device_name             TEXT NOT NULL DEFAULT 'Browser',
    ip_address              TEXT NOT NULL DEFAULT '',
    user_agent              TEXT NOT NULL DEFAULT '',
    created_at              REAL NOT NULL,
    last_active_at          REAL NOT NULL,
    expires_at              REAL NOT NULL,
    revoked_at              REAL,
    revoke_reason           TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_active ON user_sessions (user_id, revoked_at, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_family ON user_sessions (family_id);
CREATE INDEX IF NOT EXISTS idx_sessions_prev_hash ON user_sessions (previous_token_hash);

CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti                     TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at              REAL NOT NULL,
    revoked_at              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires ON revoked_tokens (expires_at);

CREATE TABLE IF NOT EXISTS notebooks (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title                   TEXT NOT NULL,
    color                   TEXT NOT NULL DEFAULT '#2c4a8a',
    page_order              TEXT NOT NULL DEFAULT '[]',
    page_count              INTEGER NOT NULL DEFAULT 0,
    version                 INTEGER NOT NULL DEFAULT 1,
    is_archived             INTEGER NOT NULL DEFAULT 0,
    created_at              REAL NOT NULL,
    updated_at              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notebooks_user_updated ON notebooks (user_id, is_archived, updated_at DESC);

CREATE TABLE IF NOT EXISTS notebook_pages (
    id                      TEXT PRIMARY KEY,
    notebook_id             TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    paper                   TEXT NOT NULL DEFAULT 'ruled' CHECK (paper IN ('ruled', 'grid', 'blank')),
    extracted_lines         TEXT NOT NULL DEFAULT '[]',
    items_compressed        BLOB NOT NULL,
    meta_compressed         BLOB NOT NULL,
    stroke_count            INTEGER NOT NULL DEFAULT 0,
    item_count              INTEGER NOT NULL DEFAULT 0,
    byte_size               INTEGER NOT NULL DEFAULT 0,
    content_hash            TEXT NOT NULL,
    version                 INTEGER NOT NULL DEFAULT 1,
    created_at              REAL NOT NULL,
    updated_at              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pages_notebook ON notebook_pages (notebook_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pages_user ON notebook_pages (user_id);

CREATE TABLE IF NOT EXISTS media_blobs (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sha256_hash             TEXT NOT NULL,
    mime_type               TEXT NOT NULL DEFAULT 'image/png',
    width                   INTEGER NOT NULL DEFAULT 0,
    height                  INTEGER NOT NULL DEFAULT 0,
    byte_size               INTEGER NOT NULL DEFAULT 0,
    data_compressed         BLOB NOT NULL,
    created_at              REAL NOT NULL,
    UNIQUE (user_id, sha256_hash)
);

CREATE INDEX IF NOT EXISTS idx_blobs_user_hash ON media_blobs (user_id, sha256_hash);

CREATE TABLE IF NOT EXISTS ai_interactions (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT REFERENCES users(id) ON DELETE SET NULL,
    notebook_id             TEXT REFERENCES notebooks(id) ON DELETE SET NULL,
    page_id                 TEXT REFERENCES notebook_pages(id) ON DELETE SET NULL,
    prompt_hash             TEXT NOT NULL,
    command                 TEXT NOT NULL DEFAULT '',
    requested_mode          TEXT NOT NULL DEFAULT 'auto',
    resolved_mode           TEXT NOT NULL DEFAULT 'hint',
    hint_level              INTEGER NOT NULL DEFAULT 1,
    offline                 INTEGER NOT NULL DEFAULT 0,
    had_image               INTEGER NOT NULL DEFAULT 0,
    image_sha256            TEXT,
    input_lines             TEXT NOT NULL DEFAULT '[]',
    output_lines            TEXT NOT NULL DEFAULT '[]',
    first_wrong             TEXT,
    verdict                 TEXT NOT NULL DEFAULT '',
    annotations             TEXT NOT NULL DEFAULT '[]',
    agent_trace             TEXT NOT NULL DEFAULT '[]',
    latency_ms              REAL NOT NULL DEFAULT 0,
    created_at              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_user_created ON ai_interactions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_page_created ON ai_interactions (page_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_prompt_hash ON ai_interactions (prompt_hash, created_at DESC);

CREATE TABLE IF NOT EXISTS security_audit_logs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 TEXT REFERENCES users(id) ON DELETE SET NULL,
    session_id              TEXT,
    event_type              TEXT NOT NULL,
    ip_address              TEXT NOT NULL DEFAULT '',
    user_agent              TEXT NOT NULL DEFAULT '',
    details                 TEXT NOT NULL DEFAULT '{}',
    created_at              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_user_created ON security_audit_logs (user_id, created_at DESC);
"""


# --------------------------------------------------------------------------- PostgreSQL connection pool

_PRISMA_ONLY_PARAMS = {"pgbouncer", "connection_limit", "pool_timeout", "supa", "schema"}
_SSL_REQUIRED_SUFFIXES = (".supabase.co", ".supabase.com", ".neon.tech")


def normalize_postgres_dsn(dsn: str) -> tuple[str, bool]:
    """Sanitize PostgreSQL URLs for libpq (Supabase, Neon, Railway, VPS).

    - Strips Prisma-only query params (?pgbouncer=true, ?connection_limit=...) that crash libpq.
    - Detects transaction-mode connection poolers (Supabase Supavisor :6543, Neon -pooler).
    - Automatically adds sslmode=require for cloud hosts (Supabase/Neon) if omitted.
    Returns (clean_dsn, is_pooled).
    """
    parsed = urlparse(dsn.strip())
    scheme = "postgresql" if parsed.scheme == "postgres" else parsed.scheme
    host = (parsed.hostname or "").lower()
    port = parsed.port

    raw_params = parse_qsl(parsed.query, keep_blank_values=True)
    is_pooled = (
        port == 6543
        or "-pooler." in host
        or ".pooler.supabase." in host
        or any(k.lower() == "pgbouncer" and v.lower() in ("true", "1", "yes") for k, v in raw_params)
    )

    clean_params: list[tuple[str, str]] = [
        (k, v) for k, v in raw_params if k.lower() not in _PRISMA_ONLY_PARAMS
    ]
    has_sslmode = any(k.lower() == "sslmode" for k, _ in clean_params)
    if not has_sslmode and any(host.endswith(sfx) for sfx in _SSL_REQUIRED_SUFFIXES):
        clean_params.append(("sslmode", "require"))

    clean_dsn = urlunparse((
        scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        urlencode(clean_params),
        parsed.fragment,
    ))
    return clean_dsn, is_pooled


class _PostgresPool:
    """Bounded thread-safe PostgreSQL connection pool supporting psycopg (v3) and psycopg2."""

    def __init__(self, dsn: str, max_size: int = 10) -> None:
        self.dsn, self.is_pooled = normalize_postgres_dsn(dsn)
        self.max_size = max(1, max_size)
        self._pool: queue.LifoQueue[Any] = queue.LifoQueue(maxsize=self.max_size)
        self._created = 0
        self._lock = threading.Lock()
        self._driver_name, self._driver = self._load_driver()

    @staticmethod
    def _load_driver() -> tuple[str, Any]:
        try:
            import psycopg  # type: ignore
            return "psycopg3", psycopg
        except ImportError:
            pass
        try:
            import psycopg2  # type: ignore
            return "psycopg2", psycopg2
        except ImportError as exc:
            raise DatabaseError(
                "PostgreSQL DATABASE_URL configured, but neither 'psycopg' nor 'psycopg2' is installed. "
                "Install with: .venv/bin/pip install 'psycopg[binary]'"
            ) from exc

    def _new_conn(self) -> Any:
        connect_timeout = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))
        if self._driver_name == "psycopg3":
            # Disable server-side prepared statements for compatibility with Supabase Supavisor / PgBouncer (:6543)
            try:
                return self._driver.connect(self.dsn, connect_timeout=connect_timeout, prepare_threshold=None)
            except TypeError:
                return self._driver.connect(self.dsn, connect_timeout=connect_timeout)
        return self._driver.connect(self.dsn, connect_timeout=connect_timeout)

    def acquire(self) -> Any:
        try:
            conn = self._pool.get_nowait()
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
                return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                with self._lock:
                    self._created = max(0, self._created - 1)
        except queue.Empty:
            pass

        with self._lock:
            if self._created < self.max_size:
                self._created += 1
                create_new = True
            else:
                create_new = False

        if create_new:
            try:
                return self._new_conn()
            except Exception:
                with self._lock:
                    self._created = max(0, self._created - 1)
                raise

        timeout = float(os.getenv("DB_POOL_ACQUIRE_TIMEOUT", "5.0"))
        try:
            return self._pool.get(timeout=timeout)
        except queue.Empty as exc:
            raise DatabaseUnavailableError("Database connection pool exhausted") from exc

    def release(self, conn: Any, discard: bool = False) -> None:
        if conn is None:
            return
        if discard:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created = max(0, self._created - 1)
            return
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created = max(0, self._created - 1)

    def close_all(self) -> None:
        while True:
            try:
                conn = self._pool.get_nowait()
                try:
                    conn.close()
                except Exception:
                    pass
            except queue.Empty:
                break
        with self._lock:
            self._created = 0


# --------------------------------------------------------------------------- unified cursor wrapper


class _SessionCursor:
    """Normalizes SQLite and PostgreSQL parameter binding and dict-row fetching."""

    def __init__(self, conn: Any, dialect: str) -> None:
        self._conn = conn
        self.dialect = dialect

    def _translate(self, sql: str) -> str:
        if self.dialect == "postgresql":
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        cur = self._conn.cursor()
        cur.execute(self._translate(sql), tuple(params))
        return cur

    def fetchone(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> dict[str, Any] | None:
        cur = self.execute(sql, params)
        try:
            row = cur.fetchone()
            if row is None:
                return None
            if isinstance(row, sqlite3.Row):
                return dict(row)
            cols = [col[0] for col in (cur.description or [])]
            return dict(zip(cols, row))
        finally:
            cur.close()

    def fetchall(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[dict[str, Any]]:
        cur = self.execute(sql, params)
        try:
            rows = cur.fetchall()
            if not rows:
                return []
            if isinstance(rows[0], sqlite3.Row):
                return [dict(r) for r in rows]
            cols = [col[0] for col in (cur.description or [])]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            cur.close()


# --------------------------------------------------------------------------- database manager


def _default_database_url() -> str:
    url = (
        os.getenv("DATABASE_PRIVATE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("SUPABASE_DB_URL")
        or os.getenv("POSTGRES_URL")
        or os.getenv("POSTGRES_PRISMA_URL")
        or ""
    ).strip()
    if url:
        return url
    default_path = Path(__file__).resolve().parent.parent / ".margin_data" / "margin.sqlite3"
    return f"sqlite:///{default_path}"


class DatabaseManager:
    """Thread-safe relational database manager with circuit breaker and dual-dialect support."""

    def __init__(self, database_url: str | None = None, breaker: DBCircuitBreaker | None = None) -> None:
        self._explicit_url = database_url
        self.circuit_breaker = breaker or DBCircuitBreaker()
        self._lock = threading.Lock()
        self._initialized_url: str | None = None
        self._pg_pool: _PostgresPool | None = None
        self._sqlite_mem_conn: sqlite3.Connection | None = None

    @property
    def url(self) -> str:
        return self._explicit_url or _default_database_url()

    @property
    def dialect(self) -> str:
        u = self.url.lower()
        if u.startswith(("postgres://", "postgresql://")):
            return "postgresql"
        if u.startswith("sqlite:"):
            return "sqlite"
        raise ValueError("DATABASE_URL must start with postgresql://, postgres://, or sqlite:///")

    def _sqlite_path(self) -> str:
        u = self.url
        if u in ("sqlite://", "sqlite:///:memory:"):
            return ":memory:"
        parsed = urlparse(u)
        path = parsed.path or ""
        if os.name != "nt" and u.startswith("sqlite:////"):
            path = "/" + path.lstrip("/")
        elif u.startswith("sqlite:///"):
            path = u[len("sqlite:///"):]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return path or ":memory:"

    def _ensure_initialized(self) -> None:
        current_url = self.url
        if self._initialized_url == current_url:
            return
        with self._lock:
            if self._initialized_url == current_url:
                return
            if self._pg_pool is not None:
                self._pg_pool.close_all()
                self._pg_pool = None
            if self._sqlite_mem_conn is not None:
                try:
                    self._sqlite_mem_conn.close()
                except Exception:
                    pass
                self._sqlite_mem_conn = None

            if self.dialect == "postgresql":
                default_pool = "2" if os.getenv("VERCEL") else "10"
                max_pool = int(os.getenv("DB_POOL_MAX_SIZE", default_pool))
                self._pg_pool = _PostgresPool(current_url, max_size=max_pool)
                init_sql_path = Path(__file__).resolve().parent.parent / "docker" / "postgres" / "init.sql"
                if init_sql_path.is_file():
                    conn = self._pg_pool.acquire()
                    try:
                        cur = conn.cursor()
                        cur.execute(init_sql_path.read_text(encoding="utf-8"))
                        conn.commit()
                        cur.close()
                    except Exception:
                        conn.rollback()
                        self._pg_pool.release(conn, discard=True)
                        raise
                    else:
                        self._pg_pool.release(conn)
            else:
                path = self._sqlite_path()
                if path == ":memory:":
                    conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=5.0)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.executescript(SQLITE_SCHEMA)
                    conn.commit()
                    self._sqlite_mem_conn = conn
                else:
                    conn = sqlite3.connect(path, timeout=5.0)
                    try:
                        conn.execute("PRAGMA journal_mode = WAL")
                        conn.execute("PRAGMA synchronous = NORMAL")
                        conn.execute("PRAGMA foreign_keys = ON")
                        conn.execute("PRAGMA busy_timeout = 5000")
                        conn.executescript(SQLITE_SCHEMA)
                        conn.commit()
                    finally:
                        conn.close()

            self._initialized_url = current_url

    @contextmanager
    def session(self, current_user_id: str | None = None) -> Iterator[_SessionCursor]:
        """Execute a transactional unit of work protected by the DB circuit breaker."""
        if not self.circuit_breaker.allow_request():
            raise DatabaseUnavailableError("Database circuit breaker is OPEN; failing fast to prevent overload")

        t0 = time.monotonic()
        try:
            self._ensure_initialized()
        except (DatabaseError, sqlite3.Error, OSError) as exc:
            self.circuit_breaker.record_failure(exc)
            raise DatabaseUnavailableError("Database initialization failed") from exc

        if self.dialect == "postgresql":
            assert self._pg_pool is not None
            try:
                conn = self._pg_pool.acquire()
            except Exception as exc:
                self.circuit_breaker.record_failure(exc)
                raise DatabaseUnavailableError("Could not acquire PostgreSQL connection") from exc

            discard = False
            try:
                cur = conn.cursor()
                cur.execute("SET LOCAL statement_timeout = '10000'")
                cur.execute("SET LOCAL application_name = 'margin_api'")
                cur.execute("SELECT set_config('app.current_user_id', %s, true)", (current_user_id or "",))
                cur.close()
                yield _SessionCursor(conn, "postgresql")
                conn.commit()
                self.circuit_breaker.record_success((time.monotonic() - t0) * 1000.0)
            except (StorageQuotaExceededError, OptimisticLockError, ValueError):
                conn.rollback()
                self.circuit_breaker.record_success((time.monotonic() - t0) * 1000.0)
                raise
            except Exception as exc:
                discard = True
                try:
                    conn.rollback()
                except Exception:
                    pass
                self.circuit_breaker.record_failure(exc)
                raise
            finally:
                self._pg_pool.release(conn, discard=discard)
        else:
            path = self._sqlite_path()
            if path == ":memory:":
                with self._lock:
                    conn = self._sqlite_mem_conn
                    assert conn is not None
                    try:
                        conn.execute("PRAGMA foreign_keys = ON")
                        yield _SessionCursor(conn, "sqlite")
                        conn.commit()
                        self.circuit_breaker.record_success((time.monotonic() - t0) * 1000.0)
                    except (StorageQuotaExceededError, OptimisticLockError, ValueError):
                        conn.rollback()
                        self.circuit_breaker.record_success((time.monotonic() - t0) * 1000.0)
                        raise
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        self.circuit_breaker.record_success((time.monotonic() - t0) * 1000.0)
                        raise
                    except Exception as exc:
                        conn.rollback()
                        self.circuit_breaker.record_failure(exc)
                        raise
            else:
                try:
                    conn = sqlite3.connect(path, timeout=5.0)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.execute("PRAGMA busy_timeout = 5000")
                except Exception as exc:
                    self.circuit_breaker.record_failure(exc)
                    raise DatabaseUnavailableError("Could not open SQLite connection") from exc

                try:
                    yield _SessionCursor(conn, "sqlite")
                    conn.commit()
                    self.circuit_breaker.record_success((time.monotonic() - t0) * 1000.0)
                except (StorageQuotaExceededError, OptimisticLockError, ValueError, sqlite3.IntegrityError):
                    conn.rollback()
                    self.circuit_breaker.record_success((time.monotonic() - t0) * 1000.0)
                    raise
                except Exception as exc:
                    conn.rollback()
                    self.circuit_breaker.record_failure(exc)
                    raise
                finally:
                    conn.close()

    def health_check(self) -> dict[str, Any]:
        """Verify database connectivity and return circuit breaker + dialect diagnostics."""
        try:
            with self.session() as cur:
                row = cur.fetchone("SELECT 1 AS ok")
            ok = bool(row and row.get("ok") == 1)
        except Exception as exc:
            log.warning("DB health check failed: %s", exc)
            ok = False
        return {
            "ok": ok,
            "dialect": self.dialect,
            "circuit_breaker": self.circuit_breaker.metrics(),
        }

    def reset_for_tests(self, database_url: str | None = None) -> None:
        """Reset connection pools and circuit breaker (used in test fixtures)."""
        with self._lock:
            if self._pg_pool is not None:
                self._pg_pool.close_all()
                self._pg_pool = None
            if self._sqlite_mem_conn is not None:
                try:
                    self._sqlite_mem_conn.close()
                except Exception:
                    pass
                self._sqlite_mem_conn = None
            self._explicit_url = database_url
            self._initialized_url = None
        self.circuit_breaker.reset()

    # ----------------------------------------------------------------------- audit logging

    def log_audit_event(
        self,
        event_type: str,
        user_id: str | None = None,
        session_id: str | None = None,
        ip_address: str = "",
        user_agent: str = "",
        details: dict[str, Any] | None = None,
        cursor: _SessionCursor | None = None,
    ) -> None:
        payload = json.dumps(details or {}, separators=(",", ":"))
        now = time.time()
        sql = """
            INSERT INTO security_audit_logs (user_id, session_id, event_type, ip_address, user_agent, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        params = (user_id, session_id, event_type[:64], ip_address[:64], user_agent[:256], payload, now)
        if cursor is not None:
            cursor.execute(sql, params)
        else:
            with self.session(current_user_id=user_id) as cur:
                cur.execute(sql, params)

    # ----------------------------------------------------------------------- notebooks & pages

    def list_notebooks(self, user_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
        """Return lightweight shelf metadata for a user's notebooks (never loads stroke blobs)."""
        with self.session(current_user_id=user_id) as cur:
            if include_archived:
                rows = cur.fetchall(
                    """
                    SELECT id, title, color, page_order, page_count, version, is_archived, created_at, updated_at
                    FROM notebooks
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                    """,
                    (user_id,),
                )
            else:
                rows = cur.fetchall(
                    """
                    SELECT id, title, color, page_order, page_count, version, is_archived, created_at, updated_at
                    FROM notebooks
                    WHERE user_id = ? AND is_archived = ?
                    ORDER BY updated_at DESC
                    """,
                    (user_id, False if self.dialect == "postgresql" else 0),
                )
        return [self._format_notebook_row(r) for r in rows]

    @staticmethod
    def _format_notebook_row(r: dict[str, Any]) -> dict[str, Any]:
        try:
            pages = json.loads(r.get("page_order") or "[]")
        except ValueError:
            pages = []
        return {
            "id": r["id"],
            "title": r["title"],
            "color": r["color"],
            "pages": pages if isinstance(pages, list) else [],
            "page_count": int(r.get("page_count") or len(pages)),
            "version": int(r.get("version") or 1),
            "is_archived": bool(r.get("is_archived")),
            "created": int(float(r["created_at"]) * 1000),
            "updated": int(float(r["updated_at"]) * 1000),
        }

    def create_notebook(
        self,
        user_id: str,
        title: str,
        color: str = "#2c4a8a",
        paper: str = "ruled",
        notebook_id: str | None = None,
        initial_page_id: str | None = None,
        initial_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a notebook and its initial page atomically."""
        nb_id = (notebook_id or str(uuid.uuid4())).strip()[:64]
        page_id = (initial_page_id or str(uuid.uuid4())).strip()[:64]
        clean_title = (title or "Untitled").strip()[:80] or "Untitled"
        clean_color = (color or "#2c4a8a").strip()[:32]
        if paper not in ("ruled", "grid", "blank"):
            paper = "ruled"

        now = time.time()
        page_order = [page_id]

        with self.session(current_user_id=user_id) as cur:
            cur.execute(
                """
                INSERT INTO notebooks (id, user_id, title, color, page_order, page_count, version, is_archived, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
                """,
                (
                    nb_id,
                    user_id,
                    clean_title,
                    clean_color,
                    json.dumps(page_order),
                    False if self.dialect == "postgresql" else 0,
                    now,
                    now,
                ),
            )
            self._upsert_page_in_tx(
                cur,
                user_id=user_id,
                notebook_id=nb_id,
                page_id=page_id,
                paper=paper,
                items=initial_items or [],
                meta=[],
                now=now,
            )

        return {
            "id": nb_id,
            "title": clean_title,
            "color": clean_color,
            "pages": page_order,
            "page_count": 1,
            "version": 1,
            "is_archived": False,
            "created": int(now * 1000),
            "updated": int(now * 1000),
        }

    def get_notebook(self, user_id: str, notebook_id: str, include_pages: bool = False) -> dict[str, Any] | None:
        """Fetch a single notebook owned by user_id, optionally with its decompressed pages."""
        with self.session(current_user_id=user_id) as cur:
            row = cur.fetchone(
                """
                SELECT id, title, color, page_order, page_count, version, is_archived, created_at, updated_at
                FROM notebooks
                WHERE id = ? AND user_id = ?
                """,
                (notebook_id, user_id),
            )
            if not row:
                return None
            nb = self._format_notebook_row(row)
            if include_pages:
                page_rows = cur.fetchall(
                    """
                    SELECT id, notebook_id, paper, extracted_lines, items_compressed, meta_compressed,
                           stroke_count, item_count, byte_size, content_hash, version, updated_at
                    FROM notebook_pages
                    WHERE notebook_id = ? AND user_id = ?
                    """,
                    (notebook_id, user_id),
                )
                by_id = {p["id"]: self._format_page_row(p) for p in page_rows}
                ordered = [by_id[pid] for pid in nb["pages"] if pid in by_id]
                for pid, pdata in by_id.items():
                    if pid not in nb["pages"]:
                        ordered.append(pdata)
                nb["page_records"] = ordered
            return nb

    def update_notebook(
        self,
        user_id: str,
        notebook_id: str,
        title: str | None = None,
        color: str | None = None,
        pages: list[str] | None = None,
        is_archived: bool | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        """Update notebook metadata or page order with optional optimistic concurrency check."""
        now = time.time()
        with self.session(current_user_id=user_id) as cur:
            existing = cur.fetchone(
                "SELECT id, title, color, page_order, page_count, version, is_archived, created_at FROM notebooks WHERE id = ? AND user_id = ?",
                (notebook_id, user_id),
            )
            if not existing:
                return None
            cur_ver = int(existing["version"] or 1)
            if expected_version is not None and expected_version != cur_ver:
                raise OptimisticLockError(f"Notebook version conflict: expected {expected_version}, found {cur_ver}")

            new_title = (title.strip()[:80] or "Untitled") if title is not None else existing["title"]
            new_color = color.strip()[:32] if color is not None else existing["color"]
            if pages is not None:
                clean_pages = [str(p).strip()[:64] for p in pages if str(p).strip()][:500]
                new_order = json.dumps(clean_pages)
                new_count = len(clean_pages)
            else:
                new_order = existing["page_order"]
                new_count = int(existing["page_count"] or 0)

            new_arch = (
                (bool(is_archived) if self.dialect == "postgresql" else int(bool(is_archived)))
                if is_archived is not None
                else existing["is_archived"]
            )
            new_ver = cur_ver + 1

            cur.execute(
                """
                UPDATE notebooks
                SET title = ?, color = ?, page_order = ?, page_count = ?, version = ?, is_archived = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (new_title, new_color, new_order, new_count, new_ver, new_arch, now, notebook_id, user_id),
            )
            row = cur.fetchone(
                "SELECT id, title, color, page_order, page_count, version, is_archived, created_at, updated_at FROM notebooks WHERE id = ? AND user_id = ?",
                (notebook_id, user_id),
            )
            return self._format_notebook_row(row) if row else None

    def delete_notebook(self, user_id: str, notebook_id: str) -> bool:
        """Delete a notebook and all its pages, reclaiming user storage bytes."""
        with self.session(current_user_id=user_id) as cur:
            existing = cur.fetchone(
                "SELECT id FROM notebooks WHERE id = ? AND user_id = ?",
                (notebook_id, user_id),
            )
            if not existing:
                return False
            size_row = cur.fetchone(
                "SELECT COALESCE(SUM(byte_size), 0) AS total_bytes FROM notebook_pages WHERE notebook_id = ? AND user_id = ?",
                (notebook_id, user_id),
            )
            reclaimed = int((size_row or {}).get("total_bytes") or 0)
            cur.execute("DELETE FROM notebooks WHERE id = ? AND user_id = ?", (notebook_id, user_id))
            if reclaimed > 0:
                self._adjust_storage_bytes(cur, user_id, -reclaimed)
            return True

    def _adjust_storage_bytes(self, cur: _SessionCursor, user_id: str, delta: int) -> int:
        user_row = cur.fetchone(
            "SELECT storage_bytes_used, storage_quota_bytes FROM users WHERE id = ?",
            (user_id,),
        )
        if not user_row:
            return 0
        used = int(user_row.get("storage_bytes_used") or 0)
        quota = int(user_row.get("storage_quota_bytes") or DEFAULT_STORAGE_QUOTA_BYTES)
        new_used = max(0, used + delta)
        if delta > 0 and new_used > quota:
            raise StorageQuotaExceededError(
                f"Storage quota exceeded ({new_used} / {quota} bytes). Delete old pages or images to free space."
            )
        cur.execute(
            "UPDATE users SET storage_bytes_used = ?, updated_at = ? WHERE id = ?",
            (new_used, time.time(), user_id),
        )
        return new_used

    def _upsert_page_in_tx(
        self,
        cur: _SessionCursor,
        user_id: str,
        notebook_id: str,
        page_id: str,
        paper: str,
        items: list[dict[str, Any]],
        meta: list[dict[str, Any]],
        now: float,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if paper not in ("ruled", "grid", "blank"):
            paper = "ruled"

        opt_items, stroke_count = quantize_items(items)
        clean_meta = meta if isinstance(meta, list) else []
        content_hash = compute_content_hash(paper, opt_items, clean_meta)

        existing = cur.fetchone(
            """
            SELECT id, notebook_id, content_hash, byte_size, version, stroke_count, item_count, updated_at
            FROM notebook_pages
            WHERE id = ? AND user_id = ?
            """,
            (page_id, user_id),
        )
        if existing and existing["notebook_id"] != notebook_id:
            raise ValueError("Page ID belongs to a different notebook")

        # Fast-path deduplication: if content_hash is identical, skip heavy compression & write!
        if existing and existing["content_hash"] == content_hash:
            return {
                "id": page_id,
                "notebookId": notebook_id,
                "paper": paper,
                "content_hash": content_hash,
                "byte_size": int(existing["byte_size"]),
                "stroke_count": int(existing["stroke_count"]),
                "item_count": int(existing["item_count"]),
                "version": int(existing["version"]),
                "deduplicated": True,
                "updated": int(float(existing["updated_at"]) * 1000),
            }

        if existing and expected_version is not None and int(existing["version"]) != expected_version:
            raise OptimisticLockError(
                f"Page version conflict: expected {expected_version}, found {existing['version']}"
            )

        items_comp = compress_json(opt_items)
        meta_comp = compress_json(clean_meta)
        new_byte_size = len(items_comp) + len(meta_comp)
        if new_byte_size > MAX_PAGE_COMPRESSED_BYTES:
            raise ValueError(f"Compressed page size ({new_byte_size} B) exceeds maximum of {MAX_PAGE_COMPRESSED_BYTES} B")

        extracted_lines = json.dumps(extract_lines_from_items(opt_items), separators=(",", ":"), ensure_ascii=False)
        old_byte_size = int(existing["byte_size"]) if existing else 0
        self._adjust_storage_bytes(cur, user_id, new_byte_size - old_byte_size)

        if existing:
            new_version = int(existing["version"]) + 1
            cur.execute(
                """
                UPDATE notebook_pages
                SET paper = ?, extracted_lines = ?, items_compressed = ?, meta_compressed = ?,
                    stroke_count = ?, item_count = ?, byte_size = ?, content_hash = ?, version = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    paper,
                    extracted_lines,
                    items_comp,
                    meta_comp,
                    stroke_count,
                    len(opt_items),
                    new_byte_size,
                    content_hash,
                    new_version,
                    now,
                    page_id,
                    user_id,
                ),
            )
        else:
            new_version = 1
            cur.execute(
                """
                INSERT INTO notebook_pages (
                    id, notebook_id, user_id, paper, extracted_lines, items_compressed, meta_compressed,
                    stroke_count, item_count, byte_size, content_hash, version, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    page_id,
                    notebook_id,
                    user_id,
                    paper,
                    extracted_lines,
                    items_comp,
                    meta_comp,
                    stroke_count,
                    len(opt_items),
                    new_byte_size,
                    content_hash,
                    now,
                    now,
                ),
            )
            # Ensure page_id is registered in notebook.page_order
            nb_row = cur.fetchone(
                "SELECT page_order FROM notebooks WHERE id = ? AND user_id = ?",
                (notebook_id, user_id),
            )
            if nb_row:
                try:
                    order = json.loads(nb_row["page_order"] or "[]")
                except ValueError:
                    order = []
                if page_id not in order:
                    order.append(page_id)
                    cur.execute(
                        "UPDATE notebooks SET page_order = ?, page_count = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                        (json.dumps(order), len(order), now, notebook_id, user_id),
                    )

        cur.execute(
            "UPDATE notebooks SET updated_at = ? WHERE id = ? AND user_id = ?",
            (now, notebook_id, user_id),
        )
        return {
            "id": page_id,
            "notebookId": notebook_id,
            "paper": paper,
            "content_hash": content_hash,
            "byte_size": new_byte_size,
            "stroke_count": stroke_count,
            "item_count": len(opt_items),
            "version": new_version,
            "deduplicated": False,
            "updated": int(now * 1000),
        }

    def upsert_page(
        self,
        user_id: str,
        notebook_id: str,
        page_id: str,
        paper: str = "ruled",
        items: list[dict[str, Any]] | None = None,
        meta: list[dict[str, Any]] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        """Save or update a page with stroke quantization, zlib compression & SHA-256 deduplication."""
        now = time.time()
        with self.session(current_user_id=user_id) as cur:
            nb = cur.fetchone(
                "SELECT id FROM notebooks WHERE id = ? AND user_id = ?",
                (notebook_id, user_id),
            )
            if not nb:
                return None
            return self._upsert_page_in_tx(
                cur,
                user_id=user_id,
                notebook_id=notebook_id,
                page_id=page_id.strip()[:64],
                paper=paper,
                items=items or [],
                meta=meta or [],
                now=now,
                expected_version=expected_version,
            )

    @staticmethod
    def _format_page_row(row: dict[str, Any]) -> dict[str, Any]:
        items = decompress_json(row.get("items_compressed"), default=[])
        meta = decompress_json(row.get("meta_compressed"), default=[])
        try:
            lines = json.loads(row.get("extracted_lines") or "[]")
        except ValueError:
            lines = []
        return {
            "id": row["id"],
            "notebookId": row["notebook_id"],
            "paper": row["paper"],
            "items": items,
            "meta": meta,
            "extracted_lines": lines,
            "stroke_count": int(row.get("stroke_count") or 0),
            "item_count": int(row.get("item_count") or 0),
            "byte_size": int(row.get("byte_size") or 0),
            "content_hash": row["content_hash"],
            "version": int(row.get("version") or 1),
            "updated": int(float(row["updated_at"]) * 1000),
        }

    def get_page(self, user_id: str, notebook_id: str, page_id: str) -> dict[str, Any] | None:
        """Retrieve and decompress a single page."""
        with self.session(current_user_id=user_id) as cur:
            row = cur.fetchone(
                """
                SELECT id, notebook_id, paper, extracted_lines, items_compressed, meta_compressed,
                       stroke_count, item_count, byte_size, content_hash, version, updated_at
                FROM notebook_pages
                WHERE id = ? AND notebook_id = ? AND user_id = ?
                """,
                (page_id, notebook_id, user_id),
            )
            return self._format_page_row(row) if row else None

    def delete_page(self, user_id: str, notebook_id: str, page_id: str) -> bool:
        """Delete a page, remove it from notebook.page_order, and reclaim storage quota."""
        now = time.time()
        with self.session(current_user_id=user_id) as cur:
            row = cur.fetchone(
                "SELECT byte_size FROM notebook_pages WHERE id = ? AND notebook_id = ? AND user_id = ?",
                (page_id, notebook_id, user_id),
            )
            if not row:
                return False
            reclaimed = int(row.get("byte_size") or 0)
            cur.execute(
                "DELETE FROM notebook_pages WHERE id = ? AND notebook_id = ? AND user_id = ?",
                (page_id, notebook_id, user_id),
            )
            if reclaimed > 0:
                self._adjust_storage_bytes(cur, user_id, -reclaimed)

            nb_row = cur.fetchone(
                "SELECT page_order FROM notebooks WHERE id = ? AND user_id = ?",
                (notebook_id, user_id),
            )
            if nb_row:
                try:
                    order = [pid for pid in json.loads(nb_row["page_order"] or "[]") if pid != page_id]
                except ValueError:
                    order = []
                cur.execute(
                    "UPDATE notebooks SET page_order = ?, page_count = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (json.dumps(order), len(order), now, notebook_id, user_id),
                )
            return True

    # ----------------------------------------------------------------------- media blobs (deduplicated)

    def put_blob(
        self,
        user_id: str,
        raw_bytes: bytes,
        mime_type: str = "image/png",
        width: int = 0,
        height: int = 0,
        blob_id: str | None = None,
    ) -> dict[str, Any]:
        """Store a binary media blob with per-user SHA-256 content-addressable deduplication."""
        if not raw_bytes:
            raise ValueError("Blob payload cannot be empty")
        if len(raw_bytes) > MAX_BLOB_BYTES:
            raise ValueError(f"Blob exceeds maximum size of {MAX_BLOB_BYTES} bytes")
        if mime_type not in ("image/png", "image/jpeg", "image/webp"):
            raise ValueError("Unsupported blob mime_type; allowed: image/png, image/jpeg, image/webp")

        sha256_hash = hashlib.sha256(raw_bytes).hexdigest()
        now = time.time()

        with self.session(current_user_id=user_id) as cur:
            existing = cur.fetchone(
                """
                SELECT id, sha256_hash, mime_type, width, height, byte_size, created_at
                FROM media_blobs
                WHERE user_id = ? AND sha256_hash = ?
                """,
                (user_id, sha256_hash),
            )
            if existing:
                return {
                    "id": existing["id"],
                    "sha256": existing["sha256_hash"],
                    "mime_type": existing["mime_type"],
                    "width": int(existing["width"]),
                    "height": int(existing["height"]),
                    "byte_size": int(existing["byte_size"]),
                    "deduplicated": True,
                }

            compressed = compress_bytes(raw_bytes)
            byte_size = len(compressed)
            self._adjust_storage_bytes(cur, user_id, byte_size)
            bid = (blob_id or str(uuid.uuid4())).strip()[:64]
            cur.execute(
                """
                INSERT INTO media_blobs (id, user_id, sha256_hash, mime_type, width, height, byte_size, data_compressed, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (bid, user_id, sha256_hash, mime_type, int(width), int(height), byte_size, compressed, now),
            )
            return {
                "id": bid,
                "sha256": sha256_hash,
                "mime_type": mime_type,
                "width": int(width),
                "height": int(height),
                "byte_size": byte_size,
                "deduplicated": False,
            }

    def get_blob(self, user_id: str, blob_id: str) -> dict[str, Any] | None:
        """Retrieve and decompress a user's media blob."""
        with self.session(current_user_id=user_id) as cur:
            row = cur.fetchone(
                """
                SELECT id, sha256_hash, mime_type, width, height, byte_size, data_compressed, created_at
                FROM media_blobs
                WHERE id = ? AND user_id = ?
                """,
                (blob_id, user_id),
            )
            if not row:
                return None
            raw = decompress_bytes(row["data_compressed"], max_bytes=MAX_BLOB_BYTES)
            return {
                "id": row["id"],
                "sha256": row["sha256_hash"],
                "mime_type": row["mime_type"],
                "width": int(row["width"]),
                "height": int(row["height"]),
                "byte_size": int(row["byte_size"]),
                "data": raw,
            }

    # ----------------------------------------------------------------------- AI interactions & responses

    def record_ai_interaction(
        self,
        *,
        user_id: str | None,
        notebook_id: str | None,
        page_id: str | None,
        command: str,
        requested_mode: str,
        hint_level: int,
        image_dataurl: str | None,
        input_lines: list[dict[str, Any]],
        response_data: dict[str, Any],
        latency_ms: float,
    ) -> str:
        """Persist an AI assist request & response without storing raw multi-megabyte images."""
        interaction_id = str(uuid.uuid4())
        had_image = bool(image_dataurl and image_dataurl.strip())
        image_sha256 = (
            hashlib.sha256(image_dataurl.strip().encode("utf-8")).hexdigest()
            if had_image and image_dataurl
            else None
        )
        effective_lines = input_lines or response_data.get("lines") or []
        prompt_hash = compute_prompt_hash(effective_lines, command, requested_mode, hint_level)
        now = time.time()
        bool_offline = bool(response_data.get("offline", False))

        with self.session(current_user_id=user_id) as cur:
            # Verify notebook_id / page_id ownership if provided so FK constraints never fail
            valid_nb_id = None
            valid_page_id = None
            if notebook_id and user_id:
                nb_row = cur.fetchone("SELECT id FROM notebooks WHERE id = ? AND user_id = ?", (notebook_id, user_id))
                if nb_row:
                    valid_nb_id = notebook_id
            if page_id and user_id:
                pg_row = cur.fetchone("SELECT id FROM notebook_pages WHERE id = ? AND user_id = ?", (page_id, user_id))
                if pg_row:
                    valid_page_id = page_id

            cur.execute(
                """
                INSERT INTO ai_interactions (
                    id, user_id, notebook_id, page_id, prompt_hash, command, requested_mode, resolved_mode,
                    hint_level, offline, had_image, image_sha256, input_lines, output_lines, first_wrong,
                    verdict, annotations, agent_trace, latency_ms, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction_id,
                    user_id,
                    valid_nb_id,
                    valid_page_id,
                    prompt_hash,
                    (command or "")[:500],
                    (requested_mode or "auto")[:32],
                    str(response_data.get("mode", "hint"))[:32],
                    int(hint_level or 1),
                    bool_offline if self.dialect == "postgresql" else int(bool_offline),
                    had_image if self.dialect == "postgresql" else int(had_image),
                    image_sha256,
                    json.dumps(input_lines or [], separators=(",", ":"), ensure_ascii=False),
                    json.dumps(response_data.get("lines") or [], separators=(",", ":"), ensure_ascii=False),
                    response_data.get("first_wrong"),
                    str(response_data.get("verdict") or "")[:500],
                    json.dumps(response_data.get("annotations") or [], separators=(",", ":"), ensure_ascii=False),
                    json.dumps(response_data.get("agent_trace") or [], separators=(",", ":"), ensure_ascii=False),
                    round(float(latency_ms), 2),
                    now,
                ),
            )

            # Bounded retention per user: prevent unbounded table bloat
            if user_id:
                count_row = cur.fetchone(
                    "SELECT COUNT(*) AS cnt FROM ai_interactions WHERE user_id = ?",
                    (user_id,),
                )
                cnt = int((count_row or {}).get("cnt") or 0)
                if cnt > MAX_AI_HISTORY_PER_USER:
                    excess = cnt - MAX_AI_HISTORY_PER_USER
                    cur.execute(
                        """
                        DELETE FROM ai_interactions
                        WHERE id IN (
                            SELECT id FROM ai_interactions
                            WHERE user_id = ?
                            ORDER BY created_at ASC
                            LIMIT ?
                        )
                        """,
                        (user_id, excess),
                    )
        return interaction_id

    def find_cached_ai_response(
        self,
        *,
        lines: list[dict[str, Any]],
        command: str,
        mode: str,
        hint_level: int,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Look up a recent identical AI assist response by prompt_hash."""
        if not lines:
            return None
        prompt_hash = compute_prompt_hash(lines, command, mode, hint_level)
        with self.session(current_user_id=user_id) as cur:
            if user_id:
                row = cur.fetchone(
                    """
                    SELECT resolved_mode, offline, output_lines, first_wrong, verdict, annotations, agent_trace
                    FROM ai_interactions
                    WHERE prompt_hash = ? AND user_id = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (prompt_hash, user_id),
                )
            else:
                row = cur.fetchone(
                    """
                    SELECT resolved_mode, offline, output_lines, first_wrong, verdict, annotations, agent_trace
                    FROM ai_interactions
                    WHERE prompt_hash = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (prompt_hash,),
                )
        if not row:
            return None
        return {
            "mode": row["resolved_mode"],
            "offline": bool(row["offline"]),
            "lines": json.loads(row["output_lines"] or "[]"),
            "first_wrong": row["first_wrong"],
            "verdict": row["verdict"],
            "annotations": json.loads(row["annotations"] or "[]"),
            "agent_trace": json.loads(row["agent_trace"] or "[]"),
            "cached": True,
        }

    def list_ai_interactions(
        self,
        user_id: str,
        notebook_id: str | None = None,
        page_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List stored AI responses for a user, optionally filtered by notebook or page."""
        safe_limit = max(1, min(100, int(limit)))
        safe_offset = max(0, int(offset))
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if notebook_id:
            clauses.append("notebook_id = ?")
            params.append(notebook_id)
        if page_id:
            clauses.append("page_id = ?")
            params.append(page_id)
        where_sql = " AND ".join(clauses)
        params.extend([safe_limit, safe_offset])

        with self.session(current_user_id=user_id) as cur:
            rows = cur.fetchall(
                f"""
                SELECT id, notebook_id, page_id, prompt_hash, command, requested_mode, resolved_mode,
                       hint_level, offline, had_image, image_sha256, input_lines, output_lines,
                       first_wrong, verdict, annotations, agent_trace, latency_ms, created_at
                FROM ai_interactions
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            )
        return [self._format_ai_row(r) for r in rows]

    def get_ai_interaction(self, user_id: str, interaction_id: str) -> dict[str, Any] | None:
        """Retrieve a single stored AI interaction owned by user_id."""
        with self.session(current_user_id=user_id) as cur:
            row = cur.fetchone(
                """
                SELECT id, notebook_id, page_id, prompt_hash, command, requested_mode, resolved_mode,
                       hint_level, offline, had_image, image_sha256, input_lines, output_lines,
                       first_wrong, verdict, annotations, agent_trace, latency_ms, created_at
                FROM ai_interactions
                WHERE id = ? AND user_id = ?
                """,
                (interaction_id, user_id),
            )
        return self._format_ai_row(row) if row else None

    @staticmethod
    def _format_ai_row(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": r["id"],
            "notebook_id": r["notebook_id"],
            "page_id": r["page_id"],
            "prompt_hash": r["prompt_hash"],
            "command": r["command"],
            "requested_mode": r["requested_mode"],
            "mode": r["resolved_mode"],
            "hint_level": int(r["hint_level"]),
            "offline": bool(r["offline"]),
            "had_image": bool(r["had_image"]),
            "image_sha256": r["image_sha256"],
            "input_lines": json.loads(r["input_lines"] or "[]"),
            "lines": json.loads(r["output_lines"] or "[]"),
            "first_wrong": r["first_wrong"],
            "verdict": r["verdict"],
            "annotations": json.loads(r["annotations"] or "[]"),
            "agent_trace": json.loads(r["agent_trace"] or "[]"),
            "latency_ms": float(r["latency_ms"]),
            "created": int(float(r["created_at"]) * 1000),
        }


# Singleton database manager instance used by the server
db = DatabaseManager()
