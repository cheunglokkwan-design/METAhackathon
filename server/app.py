"""Margin agent server: FastAPI app exposing /api/* and serving web/ at /.

Run: uvicorn server.app:app --reload
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import meta_client, skills
from .meta_client import CHAT_MODEL, OfflineError

logging.basicConfig(level=logging.INFO)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Margin", version="0.2.0")
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
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


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
    mode: Literal["hint", "answer", "check", "typeset", "graph", "auto"] = "auto"
    hint_level: int = 1


# --------------------------------------------------------------------------- helpers


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
    """
    offline = meta_client.is_offline()
    level = max(1, min(3, int(req.hint_level or 1)))
    lines, from_image = _gather_lines(req)
    trace: list[dict[str, str]] = []
    if from_image:
        why = meta_client.last_read_error
        trace.append({"tool": "read_page", "summary": (f"read {len(lines)} line(s) from the page" if lines
                                                        else f"read nothing: {why}"[:90])})

    if not lines:
        mode = req.mode if req.mode != "auto" else meta_client.keyword_intent(req.command)
        mode = skills.TOOL_MODE.get(skills.INTENT_TOOL.get(mode, "give_hint"), "hint")
        why = meta_client.last_read_error if from_image else None
        if offline:
            text = "I can't read ink offline: type the line (T tool)"
        elif why and why != "Meta saw no math on the page":
            text = "I couldn't read the page just now. Try again?"
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
    elif not offline:
        out = meta_client.run_agent(req.command, lines, req.image if from_image else None,
                                    hint_level=level, toolbox=box)
    if out is None:
        intent = meta_client.keyword_intent(req.command)
        why = "offline" if offline else "agent unavailable"
        out = box.route(intent, f"{why}: keywords -> {intent}")

    result = box.result
    bbox_by_id = {l["id"]: l.get("bbox") for l in lines}
    out_lines = [{"id": r.id, "text": r.text, "latex": r.latex, "bbox": bbox_by_id.get(r.id), "status": r.status}
                 for r in result.lines]
    return {"mode": out["mode"], "offline": offline, "lines": out_lines, "first_wrong": result.first_wrong,
            "verdict": result.verdict, "annotations": out["annotations"], "agent_trace": out["agent_trace"]}


annotate = skills.annotate  # kept for callers of the old location


# --------------------------------------------------------------------------- routes


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "offline": meta_client.is_offline(), "model": CHAT_MODEL}


@app.post("/api/assist")
def assist(req: AssistRequest) -> dict[str, Any]:
    return build_response(req)


@app.post("/api/transcribe")
def transcribe(audio: UploadFile = File(...)) -> dict[str, str]:
    # Plain def: the upstream call blocks, so FastAPI runs this in its threadpool.
    data = audio.file.read(meta_client.MAX_AUDIO_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    if len(data) > meta_client.MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")
    try:
        return {"text": meta_client.transcribe(data, audio.filename or "command.wav")}
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except meta_client.TranscribeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except OfflineError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# Static page last, so /api/* wins. Tolerate web/ not existing yet.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
else:
    @app.get("/")
    def no_web() -> JSONResponse:
        return JSONResponse({"ok": True, "detail": "web/ not built yet; API is under /api"})
