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


class LineIn(BaseModel):
    id: str | None = None
    text: str = ""
    bbox: list[float] | None = None


class AssistRequest(BaseModel):
    image: str | None = None
    lines: list[LineIn] = Field(default_factory=list)
    command: str = ""
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
        return [{"id": f"L{i}", "text": l["text"], "bbox": l.get("bbox")} for i, l in enumerate(read, 1)], True
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
        trace.append({"tool": "read_page", "summary": f"read {len(lines)} line(s) from the page"})

    if not lines:
        mode = req.mode if req.mode != "auto" else meta_client.keyword_intent(req.command)
        mode = skills.TOOL_MODE.get(skills.INTENT_TOOL.get(mode, "give_hint"), "hint")
        text = ("I can't read ink offline: type the line (T tool)" if offline
                else "I couldn't find any math on this page")
        return {"mode": mode, "offline": offline, "lines": [], "first_wrong": None,
                "verdict": "no lines to check",
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
async def transcribe(audio: UploadFile = File(...)) -> dict[str, str]:
    data = await audio.read()
    try:
        return {"text": meta_client.transcribe(data, audio.filename or "audio.webm")}
    except OfflineError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# Static page last, so /api/* wins. Tolerate web/ not existing yet.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
else:
    @app.get("/")
    def no_web() -> JSONResponse:
        return JSONResponse({"ok": True, "detail": "web/ not built yet; API is under /api"})
