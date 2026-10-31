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

from . import checker, meta_client
from .meta_client import CHAT_MODEL, OfflineError

logging.basicConfig(level=logging.INFO)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Margin", version="0.1.0")


class LineIn(BaseModel):
    id: str | None = None
    text: str = ""
    bbox: list[float] | None = None


class AssistRequest(BaseModel):
    image: str | None = None
    lines: list[LineIn] = Field(default_factory=list)
    command: str = ""
    mode: Literal["hint", "answer", "check", "auto"] = "auto"
    hint_level: int = 1


# --------------------------------------------------------------------------- helpers


def _gather_lines(req: AssistRequest) -> list[dict[str, Any]]:
    """Typed lines from the page are authoritative; otherwise read the page image."""
    typed = [l for l in req.lines if l.text.strip()]
    if typed:
        return [{"id": l.id or f"L{i}", "text": l.text, "bbox": l.bbox} for i, l in enumerate(typed, 1)]
    if req.image:
        read = meta_client.read_page(req.image)
        return [{"id": f"L{i}", "text": l["text"], "bbox": l.get("bbox")} for i, l in enumerate(read, 1)]
    return []


def build_response(req: AssistRequest) -> dict[str, Any]:
    """The /api/assist pipeline: read -> check (SymPy) -> annotate."""
    offline = meta_client.is_offline()
    mode = req.mode if req.mode != "auto" else meta_client.classify_intent(req.command)
    level = max(1, min(3, int(req.hint_level or 1)))
    lines = _gather_lines(req)

    if not lines:
        text = ("I can't read ink offline: type the line (T tool)" if offline
                else "I couldn't find any math on this page")
        return {"mode": mode, "offline": offline, "lines": [], "first_wrong": None,
                "verdict": "no lines to check",
                "annotations": [{"kind": "note", "target": None, "text": text, "side": "right"}]}

    result = checker.check_lines(lines)
    bbox_by_id = {l["id"]: l.get("bbox") for l in lines}
    out_lines = [{"id": r.id, "text": r.text, "latex": r.latex, "bbox": bbox_by_id.get(r.id), "status": r.status}
                 for r in result.lines]
    return {"mode": mode, "offline": offline, "lines": out_lines, "first_wrong": result.first_wrong,
            "verdict": result.verdict, "annotations": annotate(result, mode, level)}


def annotate(result: checker.CheckResult, mode: str, level: int) -> list[dict[str, Any]]:
    """Turn a CheckResult into ink annotations per docs/CONTRACT.md."""
    ann: list[dict[str, Any]] = []
    wrong = result.wrong
    wrong_idx = result.lines.index(wrong) if wrong else len(result.lines)
    last = result.lines[-1]

    for r in result.lines[:wrong_idx]:
        if r.status == "ok":
            ann.append({"kind": "tick", "target": r.id})

    if wrong:
        d = wrong.diagnosis
        note_level = {"check": 1, "answer": 2}.get(mode, min(level, 2))
        hint = meta_client.phrase_hint(d.verdict, wrong.text, note_level, d.hints[note_level])
        ann.append({"kind": "circle", "target": wrong.id})
        ann.append({"kind": "note", "target": wrong.id, "text": hint, "side": "right"})
        for r in result.lines[wrong_idx + 1:]:
            if r.status == "unchecked" and r.parsed.ok:
                ann.append({"kind": "underline", "target": r.id})
        if mode == "hint" and level >= 3 and wrong.corrected():
            ann.append({"kind": "ghost", "after": wrong.id, "text": wrong.corrected()})
    elif mode != "answer":
        ann.extend(_all_correct_notes(result, mode, level, last))

    if mode == "answer":
        steps = checker.solve_remaining(result)
        for step in steps:
            ann.append({"kind": "write", "after": last.id, "text": step})
        if not steps and not wrong:
            text = last.note or ("That's the answer!" if result.finished else "Nothing left to solve")
            ann.append({"kind": "note", "target": last.id, "text": text, "side": "right"})
    return ann


def _all_correct_notes(result: checker.CheckResult, mode: str, level: int,
                       last: checker.LineResult) -> list[dict[str, Any]]:
    """Notes when no step is wrong: + C reminder, praise, or a nudge to the next step."""
    if last.note:
        return [{"kind": "note", "target": last.id, "text": last.note, "side": "right"}]
    if not any(r.status in ("ok", "start") for r in result.lines):
        return [{"kind": "note", "target": last.id, "text": "I can't read this line yet", "side": "right"}]
    nudges = checker.next_step_hints(result) if mode == "hint" else None
    if not nudges:
        text = "All correct!" if (result.finished or not result.integral_chain) else "Correct so far"
        return [{"kind": "note", "target": last.id, "text": text, "side": "right"}]
    text = meta_client.phrase_hint("all steps correct; next step needed", last.text, level, nudges[level])
    ann = [{"kind": "note", "target": last.id, "text": text if level < 3 else nudges[1], "side": "right"}]
    if level >= 3 and nudges[3].startswith("Next: "):
        ann.append({"kind": "ghost", "after": last.id, "text": nudges[3][len("Next: "):]})
    return ann


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
