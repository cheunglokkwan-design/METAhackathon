"""Margin agent server: FastAPI app exposing /api/* and serving web/ at /.

Run: uvicorn server.app:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import base64
import logging
import os
from pathlib import Path
import time
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response as PlainResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db as db_module, meta_client, realtime, router, skills
from .auth import (
    AuthError,
    REFRESH_COOKIE_NAME,
    auth_rate_limiter,
    clear_auth_cookies,
    client_ip,
    get_current_user,
    get_optional_user,
    session_manager,
    set_auth_cookies,
)
from .db import (
    DatabaseUnavailableError,
    OptimisticLockError,
    StorageQuotaExceededError,
    db,
)
from .meta_client import CHAT_MODEL, OfflineError

logging.basicConfig(level=logging.INFO)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Warm up and auto-migrate the database schema on container startup (Railway / Docker)."""
    try:
        db._ensure_initialized()
        logging.getLogger("margin.app").info("Database initialized (dialect=%s)", db.dialect)
    except Exception as exc:
        logging.getLogger("margin.app").warning("Deferred DB initialization until first request: %s", exc)
    yield


app = FastAPI(title="Margin", version="0.3.0", lifespan=_lifespan)
# Multipart framing around the audio itself: boundaries, part headers, filename.
FORM_OVERHEAD = 64 * 1024


@app.middleware("http")
async def _cap_upload_size(request, call_next):
    """Refuse an oversized upload from its header, before the body is received.

    Registered before _security_headers so those headers still wrap this response.
    """
    if request.method == "POST" and request.url.path == "/api/transcribe":
        size = request.headers.get("content-length")
        # Proxies (Vercel) may forward the upload chunked with no Content-Length; the route
        # still reads at most MAX_AUDIO_BYTES + 1, so only a stated size is checked here.
        if size is not None and not size.isdigit():
            return JSONResponse({"detail": "bad Content-Length"}, status_code=400)
        if size is not None and int(size) > meta_client.MAX_AUDIO_BYTES + FORM_OVERHEAD:
            return JSONResponse({"detail": "audio too large"}, status_code=413)
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), payment=()")
    if request.url.path.startswith("/api/auth"):
        resp.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate, private")
        resp.headers.setdefault("Pragma", "no-cache")
    return resp


# --------------------------------------------------------------------------- request schemas


class LineIn(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    text: str = Field(default="", max_length=500)
    bbox: list[float] | None = None


class AssistRequest(BaseModel):
    image: str | None = Field(default=None, max_length=5_000_000)
    lines: list[LineIn] = Field(default_factory=list, max_length=100)
    command: str = Field(default="", max_length=500)
    # Boxes of the student's handwritten rows, top to bottom, measured on the page. More exact
    # than the boxes a vision model guesses, and a fallback when it gives none.
    ink_rows: list[list[float]] = Field(default_factory=list, max_length=100)
    mode: Literal["hint", "answer", "check", "typeset", "graph", "next", "why", "auto"] = "auto"
    hint_level: int = 1
    # Line history for the decision router (ROUTER_*); ignored when the router is off.
    hints_given: int = Field(default=0, ge=0, le=20)
    wrote_since_hint: bool = False
    last_command: str = Field(default="", max_length=500)
    notebook_id: str | None = Field(default=None, max_length=64)
    page_id: str | None = Field(default=None, max_length=64)
    use_cache: bool = False


class RegisterRequest(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=80)


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=512)


class NotebookCreateRequest(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    title: str = Field(default="Untitled", max_length=80)
    color: str = Field(default="#2c4a8a", max_length=32)
    paper: Literal["ruled", "grid", "blank"] = "ruled"
    initial_page_id: str | None = Field(default=None, max_length=64)
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)


class NotebookUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    color: str | None = Field(default=None, max_length=32)
    pages: list[str] | None = Field(default=None, max_length=500)
    is_archived: bool | None = None
    expected_version: int | None = None


class PageUpsertRequest(BaseModel):
    paper: Literal["ruled", "grid", "blank"] = "ruled"
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    meta: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    expected_version: int | None = None


class BlobUploadRequest(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    data_base64: str = Field(..., max_length=6_000_000)
    mime_type: Literal["image/png", "image/jpeg", "image/webp"] = "image/png"
    width: int = Field(default=0, ge=0, le=10000)
    height: int = Field(default=0, ge=0, le=10000)


# --------------------------------------------------------------------------- helpers


def _handle_db_exceptions(fn):
    """Map database circuit-breaker, quota, and optimistic concurrency errors to HTTP codes."""
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DatabaseUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail="Database unavailable or circuit breaker open",
                headers={"Retry-After": "30"},
            ) from exc
        except StorageQuotaExceededError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except OptimisticLockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return wrapper


def _gather_lines(req: AssistRequest) -> tuple[list[dict[str, Any]], bool]:
    """Typed lines from the page are authoritative; otherwise read the page image.

    Returns (lines, read_from_image).
    """
    typed = [l for l in req.lines if l.text.strip()]
    if typed:
        return [{"id": l.id or f"L{i}", "text": l.text, "bbox": l.bbox} for i, l in enumerate(typed, 1)], False
    if req.image:
        read = meta_client.read_page(req.image)
        rows = [r for r in req.ink_rows if len(r) == 4]
        out = []
        for i, l in enumerate(read, 1):
            bbox = l.get("bbox")
            if len(rows) == len(read) or (bbox is None and i <= len(rows)):
                bbox = rows[i - 1]
            out.append({"id": f"L{i}", "text": l["text"], "bbox": bbox})
        return out, True
    return [], False


def build_response(req: AssistRequest) -> dict[str, Any]:
    """The /api/assist pipeline: read -> check (SymPy) -> agent picks tools -> ink.

    mode "auto" + online: Muse Spark runs the tool loop (meta_client.run_agent).
    mode "auto" offline (or agent failure): keyword routing. An explicit mode runs its tool.
    mode "auto" with ROUTER_* set: the decision router first, then Spark, then keywords.
    """
    offline = meta_client.is_offline()
    level = max(1, min(3, int(req.hint_level or 1)))
    lines, from_image = _gather_lines(req)
    trace: list[dict[str, str]] = []
    if from_image:
        why = meta_client.last_read_error
        trace.append({"tool": "read_page", "summary": (f"read {len(lines)} line(s) from the page" if lines
                                                        else f"read nothing: {why}"[:90])})

    if not lines and not offline and req.mode == "auto" and meta_client.keyword_intent(req.command) == "picture":
        box = skills.Toolbox([], level)                 # a picture needs no maths on the page
        out = box.route("picture", "picture on an empty page", req.command)
        return {"mode": "picture", "offline": offline, "lines": [], "first_wrong": None, "verdict": "",
                "annotations": out["annotations"], "agent_trace": trace + out["agent_trace"]}
    if not lines:
        mode = req.mode if req.mode != "auto" else meta_client.keyword_intent(req.command)
        mode = skills.TOOL_MODE.get(skills.INTENT_TOOL.get(mode, "give_hint"), "hint")
        why = meta_client.last_read_error if from_image else None
        if offline:
            text = "I can't read ink offline: type the line (T tool)"
        elif why and why != "Meta saw no math on the page":
            text = f"I couldn't read the page: {meta_client.read_error_text(why)}. Try again?"
        else:
            text = "I couldn't find any math on this page"
        return {"mode": mode, "offline": offline, "lines": [], "first_wrong": None,
                "verdict": "no lines to check", "read_error": why,
                "annotations": [{"kind": "note", "target": None, "text": text, "side": "right"}],
                "agent_trace": trace + [{"tool": "none", "summary": "no lines to check"}]}

    box = skills.Toolbox(lines, level)
    box.trace.extend(trace)
    out = None
    if req.mode != "auto":
        out = box.route(req.mode, f"mode {req.mode} from the page")
    elif router.enabled():
        sent = {"hints_given", "wrote_since_hint", "last_command"} & req.model_fields_set
        session = (router.session_state(req.hints_given, req.wrote_since_hint, req.last_command) if sent
                   else router.session_from_level(level))
        out = router.route(req.command, box, req.image if from_image else None, session, spark=not offline)
    elif not offline:
        out = meta_client.run_agent(req.command, lines, req.image if from_image else None,
                                    hint_level=level, toolbox=box)
    if out is None:
        intent = meta_client.keyword_intent(req.command)
        why = "offline" if offline else "agent unavailable"
        out = box.route(intent, f"{why}: keywords -> {intent}", req.command)

    result = box.result
    bbox_by_id = {l["id"]: l.get("bbox") for l in lines}
    out_lines = [{"id": r.id, "text": r.text, "latex": r.latex, "bbox": bbox_by_id.get(r.id), "status": r.status}
                 for r in result.lines]
    resp = {"mode": out["mode"], "offline": offline, "lines": out_lines, "first_wrong": result.first_wrong,
            "verdict": result.verdict, "annotations": out["annotations"], "agent_trace": out["agent_trace"]}
    if "router" in out:
        resp["router"] = out["router"]  # router input + decision, logged with the interaction
    return resp


annotate = skills.annotate  # kept for callers of the old location


# --------------------------------------------------------------------------- core & health routes


@app.get("/api/health")
def health() -> dict[str, Any]:
    out = {"ok": True, "offline": meta_client.is_offline(), "model": CHAT_MODEL, "realtime": realtime.available()}
    if router.enabled():
        out["router"] = {"model": os.getenv("ROUTER_MODEL", ""), "circuit_breaker": router.circuit_breaker.state}
    return out


@app.websocket("/ws/voice")
async def voice_ws(ws: WebSocket) -> None:
    """Live voice: microphone audio in, Meta's words out as you speak (see server/realtime.py)."""
    await realtime.voice_socket(ws)


@app.get("/api/health/db")
def health_db() -> dict[str, Any]:
    db_status = db.health_check()
    return {
        "ok": db_status["ok"],
        "database": db_status,
        "meta_circuit_breaker": {
            "state": meta_client.circuit_breaker.state,
            "failure_count": meta_client.circuit_breaker.failure_count,
        },
    }


def _without_pictures(resp: dict[str, Any]) -> dict[str, Any]:
    """The response as stored: generated pictures (megabytes of base64) are left out."""
    anns = resp.get("annotations") or []
    if not any(a.get("kind") == "picture" for a in anns):
        return resp
    return {**resp, "annotations": [{**a, "image": "(picture not stored)"} if a.get("kind") == "picture" else a
                                    for a in anns]}


@app.post("/api/assist")
def assist(
    req: AssistRequest,
    user: dict[str, Any] | None = Depends(get_optional_user),
) -> dict[str, Any]:
    t0 = time.monotonic()
    typed_lines = [{"id": l.id or f"L{i}", "text": l.text, "bbox": l.bbox} for i, l in enumerate(req.lines, 1) if l.text.strip()]

    # Optional semantic cache lookup when requested
    if req.use_cache and typed_lines and not req.image:
        try:
            cached = db.find_cached_ai_response(
                lines=typed_lines,
                command=req.command,
                mode=req.mode,
                hint_level=req.hint_level,
                user_id=user["id"] if user else None,
            )
            if cached:
                return cached
        except Exception as exc:
            logging.getLogger("margin.app").warning("AI cache lookup skipped: %s", exc)

    resp = build_response(req)
    latency_ms = (time.monotonic() - t0) * 1000.0

    # Persist structured AI response for authenticated users (or when notebook_id/page_id is given)
    if user is not None:
        try:
            interaction_id = db.record_ai_interaction(
                user_id=user["id"],
                notebook_id=req.notebook_id,
                page_id=req.page_id,
                command=req.command,
                requested_mode=req.mode,
                hint_level=req.hint_level,
                image_dataurl=req.image,
                input_lines=typed_lines,
                response_data=_without_pictures(resp),
                latency_ms=latency_ms,
            )
            resp["interaction_id"] = interaction_id
        except Exception as exc:
            logging.getLogger("margin.app").warning("Failed to record AI interaction: %s", exc)

    return resp


class ReadRequest(BaseModel):
    image: str = Field(max_length=meta_client.MAX_IMAGE_CHARS)


@app.post("/api/read")
def read_text(req: ReadRequest) -> dict[str, Any]:
    """Lasso › To text: the selected handwriting as lines of typed text (Muse Spark vision)."""
    if meta_client.is_offline():
        raise HTTPException(status_code=503, detail="Meta is offline, so it can't read handwriting")
    return {"lines": meta_client.read_handwriting(req.image)}


@app.post("/api/transcribe")
def transcribe(audio: UploadFile = File(...)) -> dict[str, Any]:
    # Plain def: the upstream call blocks, so FastAPI runs this in its threadpool.
    data = audio.file.read(meta_client.MAX_AUDIO_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    if len(data) > meta_client.MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")
    try:
        return meta_client.transcribe_full(data, audio.filename or "command.wav")
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except meta_client.TranscribeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except OfflineError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# --------------------------------------------------------------------------- auth & session routes


@app.post("/api/auth/register", status_code=201)
@_handle_db_exceptions
def register(req: RegisterRequest, request: Request, response: Response) -> dict[str, Any]:
    ip = client_ip(request)
    if not auth_rate_limiter.allow(f"reg:{ip}"):
        raise HTTPException(status_code=429, detail="Too many registration attempts; try again later")
    ua = request.headers.get("user-agent", "")
    try:
        bundle = session_manager.register_user(
            email=req.email,
            password=req.password,
            display_name=req.display_name,
            ip_address=ip,
            user_agent=ua,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    set_auth_cookies(response, bundle, request)
    return bundle


@app.post("/api/auth/login")
@_handle_db_exceptions
def login(req: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    ip = client_ip(request)
    if not auth_rate_limiter.allow(f"login:{ip}"):
        raise HTTPException(status_code=429, detail="Too many login attempts from this IP; try again later")
    ua = request.headers.get("user-agent", "")
    try:
        bundle = session_manager.authenticate_user(
            email=req.email,
            password=req.password,
            ip_address=ip,
            user_agent=ua,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    set_auth_cookies(response, bundle, request)
    return bundle


@app.post("/api/auth/refresh")
@_handle_db_exceptions
def refresh_token(req: RefreshRequest, request: Request, response: Response) -> dict[str, Any]:
    ip = client_ip(request)
    if not auth_rate_limiter.allow(f"refresh:{ip}"):
        raise HTTPException(status_code=429, detail="Too many token refresh requests")
    raw_token = (req.refresh_token or request.cookies.get(REFRESH_COOKIE_NAME) or "").strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="Missing refresh token")
    ua = request.headers.get("user-agent", "")
    try:
        bundle = session_manager.refresh_session(raw_token, ip_address=ip, user_agent=ua)
    except AuthError as exc:
        clear_auth_cookies(response, request)
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    set_auth_cookies(response, bundle, request)
    return bundle


@app.post("/api/auth/logout")
@_handle_db_exceptions
def logout(
    request: Request,
    response: Response,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    session_manager.revoke_session(
        user_id=user["id"],
        session_id=user["session_id"],
        jti=user.get("jti"),
        jti_exp=user.get("exp"),
        reason="user_logout",
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    clear_auth_cookies(response, request)
    return {"ok": True}


@app.post("/api/auth/logout-all")
@_handle_db_exceptions
def logout_all(
    request: Request,
    response: Response,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    count = session_manager.revoke_all_sessions(
        user_id=user["id"],
        current_jti=user.get("jti"),
        current_jti_exp=user.get("exp"),
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    clear_auth_cookies(response, request)
    return {"ok": True, "revoked_sessions": count}


@app.get("/api/auth/me")
def me(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return {"user": user}


@app.get("/api/auth/sessions")
@_handle_db_exceptions
def list_sessions(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    sessions = session_manager.list_active_sessions(user["id"], current_session_id=user["session_id"])
    return {"sessions": sessions}


@app.delete("/api/auth/sessions/{session_id}")
@_handle_db_exceptions
def revoke_specific_session(
    session_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    ok = session_manager.revoke_session(
        user_id=user["id"],
        session_id=session_id,
        jti=user["jti"] if session_id == user["session_id"] else None,
        jti_exp=user["exp"] if session_id == user["session_id"] else None,
        reason="manual_session_revoke",
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True, "revoked_session_id": session_id}


@app.get("/api/auth/audit-logs")
@_handle_db_exceptions
def list_audit_logs(
    limit: int = Query(default=50, ge=1, le=100),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return {"audit_logs": session_manager.list_audit_logs(user["id"], limit=limit)}


# --------------------------------------------------------------------------- notebooks, pages & blobs routes


@app.get("/api/notebooks")
@_handle_db_exceptions
def list_notebooks(
    include_archived: bool = Query(default=False),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return {"notebooks": db.list_notebooks(user["id"], include_archived=include_archived)}


@app.post("/api/notebooks", status_code=201)
@_handle_db_exceptions
def create_notebook(
    req: NotebookCreateRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    nb = db.create_notebook(
        user_id=user["id"],
        title=req.title,
        color=req.color,
        paper=req.paper,
        notebook_id=req.id,
        initial_page_id=req.initial_page_id,
        initial_items=req.items,
    )
    return {"notebook": nb}


@app.get("/api/notebooks/{notebook_id}")
@_handle_db_exceptions
def get_notebook(
    notebook_id: str,
    include_pages: bool = Query(default=False),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    nb = db.get_notebook(user["id"], notebook_id, include_pages=include_pages)
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return {"notebook": nb}


@app.patch("/api/notebooks/{notebook_id}")
@_handle_db_exceptions
def update_notebook(
    notebook_id: str,
    req: NotebookUpdateRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    nb = db.update_notebook(
        user_id=user["id"],
        notebook_id=notebook_id,
        title=req.title,
        color=req.color,
        pages=req.pages,
        is_archived=req.is_archived,
        expected_version=req.expected_version,
    )
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return {"notebook": nb}


@app.delete("/api/notebooks/{notebook_id}")
@_handle_db_exceptions
def delete_notebook(
    notebook_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if not db.delete_notebook(user["id"], notebook_id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    return {"ok": True}


@app.put("/api/notebooks/{notebook_id}/pages/{page_id}")
@_handle_db_exceptions
def upsert_page(
    notebook_id: str,
    page_id: str,
    req: PageUpsertRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    page = db.upsert_page(
        user_id=user["id"],
        notebook_id=notebook_id,
        page_id=page_id,
        paper=req.paper,
        items=req.items,
        meta=req.meta,
        expected_version=req.expected_version,
    )
    if not page:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return {"page": page}


@app.get("/api/notebooks/{notebook_id}/pages/{page_id}")
@_handle_db_exceptions
def get_page(
    notebook_id: str,
    page_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    page = db.get_page(user["id"], notebook_id, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return {"page": page}


@app.delete("/api/notebooks/{notebook_id}/pages/{page_id}")
@_handle_db_exceptions
def delete_page(
    notebook_id: str,
    page_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if not db.delete_page(user["id"], notebook_id, page_id):
        raise HTTPException(status_code=404, detail="Page not found")
    return {"ok": True}


@app.post("/api/blobs", status_code=201)
@_handle_db_exceptions
def upload_blob(
    req: BlobUploadRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    raw_str = req.data_base64.strip()
    if "," in raw_str and raw_str.startswith("data:"):
        raw_str = raw_str.split(",", 1)[1]
    try:
        raw_bytes = base64.b64decode(raw_str, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 payload") from exc

    blob = db.put_blob(
        user_id=user["id"],
        raw_bytes=raw_bytes,
        mime_type=req.mime_type,
        width=req.width,
        height=req.height,
        blob_id=req.id,
    )
    return {"blob": blob}


@app.get("/api/blobs/{blob_id}")
@_handle_db_exceptions
def get_blob(
    blob_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    blob = db.get_blob(user["id"], blob_id)
    if not blob:
        raise HTTPException(status_code=404, detail="Blob not found")
    return {
        "blob": {
            "id": blob["id"],
            "sha256": blob["sha256"],
            "mime_type": blob["mime_type"],
            "width": blob["width"],
            "height": blob["height"],
            "byte_size": blob["byte_size"],
            "data_base64": base64.b64encode(blob["data"]).decode("ascii"),
        }
    }


# --------------------------------------------------------------------------- AI responses history routes


@app.get("/api/ai-responses")
@_handle_db_exceptions
def list_ai_responses(
    notebook_id: str | None = Query(default=None),
    page_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    items = db.list_ai_interactions(
        user_id=user["id"],
        notebook_id=notebook_id,
        page_id=page_id,
        limit=limit,
        offset=offset,
    )
    return {"ai_responses": items}


@app.get("/api/ai-responses/{interaction_id}")
@_handle_db_exceptions
def get_ai_response(
    interaction_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    item = db.get_ai_interaction(user["id"], interaction_id)
    if not item:
        raise HTTPException(status_code=404, detail="AI interaction not found")
    return {"ai_response": item}


# --------------------------------------------------------------------------- split hosting (Vercel page + Railway data)
#
# The page on Vercel can keep accounts and notebooks on another server (the Railway backend
# with the database). The page learns that server's address from /api/config.js, which reads
# MARGIN_API_URL (or NEXT_PUBLIC_API_URL, the name in the setup notes). Unset: same origin.

DEFAULT_CORS_ORIGINS = "https://met-ahackathon.vercel.app"


def data_api_url() -> str:
    url = (os.getenv("MARGIN_API_URL") or os.getenv("NEXT_PUBLIC_API_URL") or "").strip().rstrip("/")
    return url if url.startswith(("https://", "http://")) else ""


def cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip() and o.strip() != "*"]


@app.get("/api/config.js", include_in_schema=False)
def config_js() -> PlainResponse:
    import json
    body = f"window.MARGIN_API_URL = {json.dumps(data_api_url())};\n"
    return PlainResponse(body, media_type="application/javascript",
                         headers={"Cache-Control": "no-store"})


# Outermost middleware, so error responses carry CORS headers too. Exact origins only: with
# credentials allowed, a wildcard or loose pattern would let other sites read notebooks.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    max_age=600,
)


# Static page last, so /api/* wins. Tolerate web/ not existing yet.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
else:
    @app.get("/")
    def no_web() -> JSONResponse:
        return JSONResponse({"ok": True, "detail": "web/ not built yet; API is under /api"})
