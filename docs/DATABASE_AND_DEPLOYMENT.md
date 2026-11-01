# Margin Database, Security, Session Manager & Deployment Architecture

## 1. Is a Docker Container on Vercel the Best Option for PostgreSQL?

**Short Answer:** **No — you cannot run a stateful PostgreSQL Docker container inside Vercel.**

### Why Docker on Vercel Doesn't Work for a Relational Database
1. **Stateless Ephemeral Compute:** Vercel deploys Python (`@vercel/python`) as **stateless serverless functions** (AWS Lambda / Firecracker micro-VMs under the hood). They spin up on demand for an HTTP request and freeze/terminate seconds later.
2. **No Persistent Disk (`PGDATA`):** The filesystem inside Vercel functions is read-only (except `/tmp`, which is wiped across cold starts and never shared across concurrent instances). Any data written to a containerized database inside a serverless function would be **permanently lost** on the next cold start.
3. **No Persistent TCP Listener (`:5432`):** Serverless functions only handle incoming HTTP/HTTPS requests; they cannot keep a background PostgreSQL daemon listening on port `5432`.

### What IS the Best Production Architecture?

You have **two gold-standard options**, and this repository is now engineered to support **both out of the box**:

| Deployment Target | How the App Runs | How PostgreSQL Runs | When to Choose |
|---|---|---|---|
| **Option A (Supabase + Vercel)** | FastAPI runs serverless on **Vercel** (`vercel.json` -> `api/index.py`) | **Supabase PostgreSQL** connected via Supavisor Pooler (`:6543` / `DATABASE_URL`) | Best when you want zero-ops Vercel hosting paired with Supabase's managed Postgres. |
| **Option B1 (Railway)** | Hardened `Dockerfile` + `railway.json` on **Railway** | **Railway PostgreSQL** service linked via internal private network (`DATABASE_PRIVATE_URL` / `DATABASE_URL`) | Best for 1-click container + managed Postgres deployment with persistent connections. |
| **Option B2 (VPS + Docker Compose)** | `docker-compose.yml` (`margin-app` + `postgres:16-alpine`) | Self-hosted **PostgreSQL 16 Docker container** with persistent volume (`margin_pgdata`) | Best when deploying to a Linux VPS (Hetzner, DigitalOcean, AWS EC2) or local dev. |

---

## 1.5 Step-by-Step Deployment Playbooks for Your Team

### Playbook 1: Deploying on Railway (Option B1 — You)
Railway natively reads [railway.json](file:///home/saswatdev/My%20Project/METAhackathon/railway.json) and [Dockerfile](file:///home/saswatdev/My%20Project/METAhackathon/Dockerfile).
1. In your Railway Project, click **New** $\rightarrow$ **Database** $\rightarrow$ **Add PostgreSQL**.
2. Click **New** $\rightarrow$ **GitHub Repo** and select this repository.
3. Under your app service's **Variables** tab in Railway, add:
   ```env
   ENV=production
   DATABASE_URL=${{Postgres.DATABASE_PRIVATE_URL}}
   JWT_SECRET_KEY=<generate 64-char hex via: python3 -c "import secrets; print(secrets.token_hex(32))">
   PASSWORD_PEPPER=<generate 64-char hex via: python3 -c "import secrets; print(secrets.token_hex(32))">
   MODEL_API_KEY=<your Meta AI key>
   ```
4. Railway automatically builds the `Dockerfile`, runs `/docker/postgres/init.sql` on first connection, and monitors `/api/health`.

### Playbook 2: Deploying on a Linux VPS (Option B2 — Friend 1)
The entire stack (`postgres:16-alpine` + `margin-app`) is orchestrated via [docker-compose.yml](file:///home/saswatdev/My%20Project/METAhackathon/docker-compose.yml).
1. SSH into the VPS, clone the repo, and create `.env`:
   ```bash
   cp .env.example .env
   ```
2. Set strong secrets in `.env`:
   ```env
   ENV=production
   POSTGRES_USER=margin
   POSTGRES_PASSWORD=<strong_random_db_password>
   POSTGRES_DB=margin
   JWT_SECRET_KEY=<64_char_random_hex>
   PASSWORD_PEPPER=<64_char_random_hex>
   MODEL_API_KEY=<your_meta_api_key>
   APP_PORT=8000
   ```
3. Launch the stack:
   ```bash
   docker compose up --build -d
   docker compose ps
   ```
   *Note:* PostgreSQL binds only to `127.0.0.1:5432` on the host (never exposed to the public internet) and persists data in the `margin_pgdata` Docker volume. Put Caddy or Nginx in front of `127.0.0.1:8000` for automatic HTTPS.

### Playbook 3: Deploying on Supabase + Vercel (Option A — Friend 2)
[`normalize_postgres_dsn`](file:///home/saswatdev/My%20Project/METAhackathon/server/db.py#L497-L534) is specifically hardened for **Supabase Supavisor (`:6543`)**:
* It automatically strips Prisma-only query parameters (`?pgbouncer=true&connection_limit=1`) that would otherwise crash Python's `libpq`/`psycopg`.
* It automatically enforces `sslmode=require` on `*.supabase.com` / `*.supabase.co`.
* It passes `prepare_threshold=None` to `psycopg` v3 so Supabase's Transaction Pooler (`:6543`) never raises `DuplicatePreparedStatement`.
* When `VERCEL=1` is present, it automatically caps `DB_POOL_MAX_SIZE=2` per serverless function instance so concurrent cold starts never exhaust Supabase connections.

**Steps:**
1. In **Supabase Dashboard** $\rightarrow$ **SQL Editor**, paste and run [docker/postgres/init.sql](file:///home/saswatdev/My%20Project/METAhackathon/docker/postgres/init.sql) (or let the app auto-run it on first boot).
2. In **Supabase Dashboard** $\rightarrow$ **Connect** $\rightarrow$ copy the **Transaction Pooler** URI (port `6543`).
3. In **Vercel Project Settings** $\rightarrow$ **Environment Variables**, set:
   ```env
   ENV=production
   DATABASE_URL=postgresql://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
   JWT_SECRET_KEY=<64_char_random_hex>
   PASSWORD_PEPPER=<64_char_random_hex>
   MODEL_API_KEY=<your_meta_api_key>
   ```
4. Deploy to Vercel (`vercel --prod`).

---

## 2. Database Schema & Anti-Overload Optimizations

Relational databases in handwriting/AI notebook apps typically get overloaded for four reasons:
1. Storing multi-megabyte base64 page snapshots inside database rows.
2. Storing thousands of unquantized floating-point pen coordinates (`[142.39481239, 581.9283712, 0.5192837]`) as raw JSON on every 400ms autosave.
3. Loading full page strokes when rendering the notebook shelf (`N+1` or heavy row scans).
4. Unbounded connection spikes from serverless functions hanging when the DB is slow.

### How Margin Solves Every One of These (`server/db.py` & `docker/postgres/init.sql`)

1. **SHA-256 Content-Hash Deduplication (`content_hash`)**
   - Every page save computes a canonical SHA-256 hash over `(paper, quantized_items, meta)`.
   - If the frontend autosaves when nothing actually changed, the database detects `existing.content_hash == new_content_hash` and **returns immediately (`deduplicated: true`) without rewriting the compressed `BYTEA` columns or bumping the row version**.
2. **Stroke Coordinate Quantization & Point Decimation (`quantize_items`)**
   - Pen coordinates `[x, y, pressure]` are quantized to `[round(x, 1), round(y, 1), round(p, 2)]` (0.1px spatial accuracy, 1% pressure accuracy) and capped at `2,500` points per stroke / `2,000` items per page.
3. **Zlib Binary Compression + Decompression-Bomb Guard (`compress_json` / `decompress_bytes`)**
   - Vector strokes (`items_compressed`) and Meta's SVG ink (`meta_compressed`) are stored as zlib-compressed `BYTEA` blobs, reducing storage size by **75–85%**.
   - Decompression uses streaming `zlib.decompressobj().decompress(data, MAX_DECOMPRESSED_BYTES + 1)` capped at `5 MiB` to defeat zip-bomb memory exhaustion attacks.
4. **Content-Addressable Media Blobs (`media_blobs`) & Per-User Storage Quotas**
   - Inline `data:image/...` strings inside page items are rejected. Images are uploaded to `/api/blobs`, hashed with SHA-256, compressed, and deduplicated per `(user_id, sha256_hash)`.
   - Every user has a strict `storage_quota_bytes` (default `50 MiB`) tracked atomically in `users.storage_bytes_used`.
5. **Lightweight AI Interaction Ledger & Semantic Cache (`ai_interactions`)**
   - When `/api/assist` is called with a 5 MB handwriting snapshot (`image`), the database **never stores the raw base64 image**. Instead, it stores `had_image = true` and the 64-character `image_sha256` alongside the transcribed `input_lines`, `output_lines` (with SymPy status & LaTeX), `first_wrong`, `verdict`, `annotations`, `agent_trace`, and `latency_ms`.
   - Requests are indexed by `prompt_hash` for optional O(1) semantic caching (`use_cache: true`), and retention is automatically bounded per user (`MAX_AI_HISTORY_PER_USER = 500`).

---

## 3. Security, Authentication, JWT & Stateful Session Manager (`server/auth.py`)

### Password Security
- **Memory-Hard Hashing:** Uses `hashlib.scrypt` (`n=16384, r=8, p=1`, 16-byte cryptographic salt, 32-byte derived key) with automatic fallback to `PBKDF2-HMAC-SHA256` (`600,000` iterations) and optional server-side HMAC-SHA256 `PASSWORD_PEPPER`.
- **Timing-Attack Mitigation:** Login requests for non-existent emails execute a full scrypt verification against a precomputed dummy hash (`verify_dummy_password`) so response times never leak whether an email is registered.
- **Brute-Force Lockout:** 5 consecutive failed password attempts lock the account for 15 minutes (`HTTP 429`) and record an `ACCOUNT_LOCKED` audit event.

### JWT & Stateful Session Manager (`user_sessions` + `revoked_tokens`)
- **Short-Lived HS256 Access JWTs (15 min):**
  - Strictly validates `alg == "HS256"` and `typ == "JWT"` (explicitly blocks `alg: "none"` and key-confusion attacks).
  - Validates `iss` (`margin-auth`), `aud` (`margin-app`), `sub` (`user_id`), `sid` (`session_id`), `jti` (unique token ID), `exp`, and `nbf`.
- **Rotating Refresh Tokens & Reuse Detection (7 days):**
  - Refresh tokens are 256-bit cryptographic secrets stored **only as SHA-256 hashes** (`refresh_token_hash`) in `user_sessions`.
  - On `POST /api/auth/refresh`, the old hash moves to `previous_token_hash` and a new refresh token + new JWT are issued.
  - **Compromised Token Reuse Detection:** If an already-rotated `previous_token_hash` is replayed (e.g., by a token thief), the Session Manager immediately **revokes the entire `family_id`**, logs `TOKEN_REUSE_DETECTED` in `security_audit_logs`, and invalidates all tokens in that session family.
- **Immediate Revocation & Device Management:**
  - `GET /api/auth/sessions` lists active devices; `DELETE /api/auth/sessions/{id}`, `POST /api/auth/logout`, and `POST /api/auth/logout-all` immediately revoke sessions and add active JWT `jti`s to `revoked_tokens`.
- **Dual Auth Transport (Bearer + Hardened Cookies with CSRF):**
  - Supports `Authorization: Bearer <access_token>` AND `HttpOnly; SameSite=Lax; Secure` cookies (`margin_access`, `margin_refresh`).
  - Cookie-authenticated write requests (`POST`, `PUT`, `PATCH`, `DELETE`) enforce session-bound `X-CSRF-Token` verification.

### Database & AI Circuit Breakers
- **`DBCircuitBreaker` (`server/db.py`):** Thread-safe `closed -> open -> half_open -> closed` state machine with single-flight half-open probing and rolling query latency telemetry (`/api/health/db`). When tripped `open`, fails fast with `HTTP 503` (`Retry-After: 30`) instead of exhausting worker threads.
- **`CircuitBreaker` (`server/meta_client.py`):** Protects outbound Meta Model API calls and gracefully degrades to SymPy offline rule-based checking.
- **PostgreSQL Row-Level Security (RLS):** `notebooks`, `notebook_pages`, and `media_blobs` enforce RLS policies bound to `current_setting('app.current_user_id', true)` on every transaction in addition to application-level `WHERE user_id = ?` filters.
