-- =============================================================================
-- Margin: Production PostgreSQL 16+ Schema & Security Policies
-- =============================================================================
-- Designed for:
--   1. Docker Compose (postgres:16-alpine) via /docker-entrypoint-initdb.d/
--   2. Managed Serverless Postgres on Vercel (Neon / Vercel Postgres / Supabase)
--
-- Features:
--   - UUID & cryptographic primitives (pgcrypto)
--   - Hardened Users & Stateful Session Manager with Refresh Token Reuse Detection
--   - Immediate JWT JTI Revocation Blocklist
--   - Optimal Notebook & Page Storage (Zlib-compressed BYTEA + SHA-256 content_hash
--     deduplication + lightweight JSONB extracted_lines)
--   - Content-Addressable Media Blobs (SHA-256 deduplicated per user + quota tracking)
--   - Lightweight AI Interaction & Response Ledger (never stores raw 5MB base64 images)
--   - Security Audit Log & Row-Level Security (RLS) defense-in-depth policies
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------------
-- 1. USERS
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                       TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    email                   TEXT NOT NULL UNIQUE,
    display_name            TEXT NOT NULL DEFAULT '',
    password_hash           TEXT NOT NULL,
    role                    TEXT NOT NULL DEFAULT 'student' CHECK (role IN ('student', 'admin')),
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    failed_login_attempts   INTEGER NOT NULL DEFAULT 0,
    locked_until            DOUBLE PRECISION NOT NULL DEFAULT 0,
    storage_bytes_used      BIGINT NOT NULL DEFAULT 0 CHECK (storage_bytes_used >= 0),
    storage_quota_bytes     BIGINT NOT NULL DEFAULT 52428800 CHECK (storage_quota_bytes > 0), -- 50 MiB default
    created_at              DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
    updated_at              DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
    last_login_at           DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_users_email_lower ON users (LOWER(email));

-- -----------------------------------------------------------------------------
-- 2. USER SESSIONS (Stateful Session Manager & Refresh Token Rotation)
-- -----------------------------------------------------------------------------
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
    created_at              DOUBLE PRECISION NOT NULL,
    last_active_at          DOUBLE PRECISION NOT NULL,
    expires_at              DOUBLE PRECISION NOT NULL,
    revoked_at              DOUBLE PRECISION,
    revoke_reason           TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_active
    ON user_sessions (user_id, revoked_at, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_family
    ON user_sessions (family_id);
CREATE INDEX IF NOT EXISTS idx_sessions_prev_hash
    ON user_sessions (previous_token_hash) WHERE previous_token_hash IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 3. REVOKED JWT ACCESS TOKENS (JTI Blocklist for immediate revocation)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti                     TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at              DOUBLE PRECISION NOT NULL,
    revoked_at              DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires
    ON revoked_tokens (expires_at);

-- -----------------------------------------------------------------------------
-- 4. NOTEBOOKS (Shelf metadata — kept lightweight for O(1) shelf loads)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notebooks (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title                   TEXT NOT NULL,
    color                   TEXT NOT NULL DEFAULT '#2c4a8a',
    page_order              TEXT NOT NULL DEFAULT '[]', -- JSON array of page IDs
    page_count              INTEGER NOT NULL DEFAULT 0,
    version                 INTEGER NOT NULL DEFAULT 1,
    is_archived             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              DOUBLE PRECISION NOT NULL,
    updated_at              DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notebooks_user_updated
    ON notebooks (user_id, is_archived, updated_at DESC);

-- -----------------------------------------------------------------------------
-- 5. NOTEBOOK PAGES (Compressed vector ink + SHA-256 content deduplication)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notebook_pages (
    id                      TEXT PRIMARY KEY,
    notebook_id             TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    paper                   TEXT NOT NULL DEFAULT 'ruled' CHECK (paper IN ('ruled', 'grid', 'blank')),
    extracted_lines         TEXT NOT NULL DEFAULT '[]', -- JSON array of {id, text, bbox}
    items_compressed        BYTEA NOT NULL,             -- zlib-compressed quantized strokes/textboxes
    meta_compressed         BYTEA NOT NULL,             -- zlib-compressed MetaInk SVG annotations
    stroke_count            INTEGER NOT NULL DEFAULT 0,
    item_count              INTEGER NOT NULL DEFAULT 0,
    byte_size               INTEGER NOT NULL DEFAULT 0,
    content_hash            TEXT NOT NULL,              -- SHA-256 of canonical page state
    version                 INTEGER NOT NULL DEFAULT 1,
    created_at              DOUBLE PRECISION NOT NULL,
    updated_at              DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pages_notebook
    ON notebook_pages (notebook_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pages_user
    ON notebook_pages (user_id);

-- -----------------------------------------------------------------------------
-- 6. MEDIA BLOBS (Content-Addressable Deduplicated Images / PDF Backgrounds)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_blobs (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sha256_hash             TEXT NOT NULL,
    mime_type               TEXT NOT NULL DEFAULT 'image/png',
    width                   INTEGER NOT NULL DEFAULT 0,
    height                  INTEGER NOT NULL DEFAULT 0,
    byte_size               INTEGER NOT NULL DEFAULT 0,
    data_compressed         BYTEA NOT NULL,
    created_at              DOUBLE PRECISION NOT NULL,
    UNIQUE (user_id, sha256_hash)
);

CREATE INDEX IF NOT EXISTS idx_blobs_user_hash
    ON media_blobs (user_id, sha256_hash);

-- -----------------------------------------------------------------------------
-- 7. AI INTERACTIONS & RESPONSES (Optimal telemetry & semantic cache)
--    Never stores raw 5MB base64 images; stores 64-char SHA-256 + structured ink
-- -----------------------------------------------------------------------------
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
    offline                 BOOLEAN NOT NULL DEFAULT FALSE,
    had_image               BOOLEAN NOT NULL DEFAULT FALSE,
    image_sha256            TEXT,
    input_lines             TEXT NOT NULL DEFAULT '[]',
    output_lines            TEXT NOT NULL DEFAULT '[]',
    first_wrong             TEXT,
    verdict                 TEXT NOT NULL DEFAULT '',
    annotations             TEXT NOT NULL DEFAULT '[]',
    agent_trace             TEXT NOT NULL DEFAULT '[]',
    latency_ms              DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at              DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_user_created
    ON ai_interactions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_page_created
    ON ai_interactions (page_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_prompt_hash
    ON ai_interactions (prompt_hash, created_at DESC);

-- -----------------------------------------------------------------------------
-- 8. SECURITY AUDIT LOGS
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS security_audit_logs (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 TEXT REFERENCES users(id) ON DELETE SET NULL,
    session_id              TEXT,
    event_type              TEXT NOT NULL,
    ip_address              TEXT NOT NULL DEFAULT '',
    user_agent              TEXT NOT NULL DEFAULT '',
    details                 TEXT NOT NULL DEFAULT '{}',
    created_at              DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_user_created
    ON security_audit_logs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_event_created
    ON security_audit_logs (event_type, created_at DESC);

-- -----------------------------------------------------------------------------
-- 9. ROW-LEVEL SECURITY (Defense-in-Depth for Multi-Tenant Isolation)
-- -----------------------------------------------------------------------------
ALTER TABLE notebooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE notebook_pages ENABLE ROW LEVEL SECURITY;
ALTER TABLE media_blobs ENABLE ROW LEVEL SECURITY;

-- When app.current_user_id is set on the transaction, restrict rows to that user;
-- when empty/unset (admin or service migration role), allow service-level query
-- which already filters by WHERE user_id = ? in application SQL.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'notebooks' AND policyname = 'notebooks_isolation_policy'
    ) THEN
        CREATE POLICY notebooks_isolation_policy ON notebooks
            USING (
                COALESCE(current_setting('app.current_user_id', true), '') = ''
                OR user_id = current_setting('app.current_user_id', true)
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'notebook_pages' AND policyname = 'pages_isolation_policy'
    ) THEN
        CREATE POLICY pages_isolation_policy ON notebook_pages
            USING (
                COALESCE(current_setting('app.current_user_id', true), '') = ''
                OR user_id = current_setting('app.current_user_id', true)
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'media_blobs' AND policyname = 'blobs_isolation_policy'
    ) THEN
        CREATE POLICY blobs_isolation_policy ON media_blobs
            USING (
                COALESCE(current_setting('app.current_user_id', true), '') = ''
                OR user_id = current_setting('app.current_user_id', true)
            );
    END IF;
END
$$;
