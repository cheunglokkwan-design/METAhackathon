"""Margin as an MCP server: any agent app (Claude, ChatGPT, Cursor, ...) can use Margin's
verified calculus checker.

Every tool runs the same code as the notebook: SymPy checks each line, so a tool never calls a
wrong step right. Hints and "why" notes are worded by Muse Spark when MODEL_API_KEY is set, and by
the built-in rules otherwise. `read_handwriting` and `ask_margin` (the full agent) need the key.

Run:
    python -m server.mcp_server              # stdio (Claude Desktop, Claude Code, Cursor)
    python -m server.mcp_server --http 8765  # streamable HTTP at http://127.0.0.1:8765/mcp

Lines are the student's working, one step per string, e.g.
    ["∫ 2x cos(x^2) dx", "= sin(x^2) + C"]
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import meta_client, skills

MAX_LINES = 40
MAX_LINE_CHARS = 500
MAX_IMAGE_BYTES = 3_000_000
GRAPH_POINTS = 60

mcp = FastMCP(
    "margin",
    instructions=(
        "Margin checks a student's calculus working line by line with SymPy. Pass the working as a "
        "list of lines, one step per line (∫, d/dx, ^ for powers, + C allowed). Use check_steps to "
        "find the first wrong line, give_hint for a nudge that never gives the answer away, "
        "next_step for one verified step, explain_rule for why a step works, finish_answer only "
        "when the student asks for the solution. Prefer hints over answers when tutoring."
    ),
)


# --------------------------------------------------------------------------- helpers


def _page(lines: list[str]) -> list[dict[str, Any]]:
    if not isinstance(lines, list) or not lines:
        raise ValueError("lines must be a non-empty list of strings, one step per line")
    if len(lines) > MAX_LINES:
        raise ValueError(f"at most {MAX_LINES} lines")
    out = []
    for i, t in enumerate(lines, 1):
        t = str(t)
        if len(t) > MAX_LINE_CHARS:
            raise ValueError(f"line {i} is longer than {MAX_LINE_CHARS} characters")
        if t.strip():
            out.append({"id": f"L{len(out) + 1}", "text": t.strip(), "bbox": None})
    if not out:
        raise ValueError("all lines are empty")
    return out


def _lines_out(box: skills.Toolbox) -> list[dict[str, Any]]:
    return [{"id": r.id, "text": r.text, "status": r.status, "latex": r.latex} for r in box.result.lines]


def _marks(box: skills.Toolbox) -> list[dict[str, Any]]:
    """The ink the notebook would draw, minus page-layout fields."""
    out = []
    for a in box.annotations:
        a = {k: v for k, v in a.items() if k not in ("side",)}
        if a.get("kind") == "graph":
            for key in ("points", "overlay"):
                pts = a.get(key)
                if pts and len(pts) > GRAPH_POINTS:
                    step = len(pts) / GRAPH_POINTS
                    a[key] = [pts[int(i * step)] for i in range(GRAPH_POINTS)] + [pts[-1]]
        out.append(a)
    return out


def _run(tool: str, lines: list[str], level: int = 1, args: dict[str, Any] | None = None) -> dict[str, Any]:
    box = skills.Toolbox(_page(lines), level)
    res = box.run(tool, args or {})
    notes = [a["text"] for a in box.annotations if a.get("kind") == "note" and a.get("text")]
    written = [a["text"] for a in box.annotations if a.get("kind") in ("write", "ghost") and a.get("text")]
    return {
        "summary": res.get("summary") or res.get("error", ""),
        "first_wrong": box.result.first_wrong,
        "verdict": box.result.verdict,
        "finished": box.result.finished,
        "notes": notes,
        "written_steps": written,
        "lines": _lines_out(box),
        "marks": _marks(box),
        "worded_by": "muse-spark" if not meta_client.is_offline() else "rules",
    }


# --------------------------------------------------------------------------- tools (no key needed)


@mcp.tool()
def check_steps(lines: list[str]) -> dict[str, Any]:
    """Check every line of the working with SymPy. Each line gets a status: ok, wrong, start (the
    problem itself) or unchecked (after a mistake). Returns the first wrong line, the referee's
    verdict and a short note on the mistake."""
    return _run("check_steps", lines)


@mcp.tool()
def give_hint(lines: list[str], hint_level: int = 1) -> dict[str, Any]:
    """A short hint on the first wrong line, or a nudge toward the next step if everything is right.
    hint_level 1 says where to look, 2 names the rule or mistake, 3 also shows the corrected line.
    Levels 1-2 never reveal the answer."""
    return _run("give_hint", lines, hint_level)


@mcp.tool()
def next_step(lines: list[str]) -> dict[str, Any]:
    """Exactly one next step after the last correct line, checked by SymPy before it is returned
    (see written_steps). If a line is wrong it returns a hint on that line instead."""
    return _run("next_step", lines)


@mcp.tool()
def explain_rule(lines: list[str]) -> dict[str, Any]:
    """Why the latest step works (or what went wrong at the first mistake): the rule's name, its
    formula, and how it applies here. Returned as up to three note lines plus LaTeX."""
    out = _run("explain_rule", lines)
    note = next((m for m in out["marks"] if m.get("kind") == "note"), None)
    if note:
        out["explanation"] = note["text"].split("\n")
        out["formula_latex"] = note.get("latex")
    return out


@mcp.tool()
def finish_answer(lines: list[str]) -> dict[str, Any]:
    """The remaining steps and the final answer, each checked by SymPy (see written_steps). Use
    only when the student asks for the solution; otherwise prefer give_hint or next_step."""
    return _run("finish_answer", lines, 2)


@mcp.tool()
def typeset_lines(lines: list[str]) -> dict[str, Any]:
    """A neat version of each line in plain text and LaTeX (SymPy's reading of the handwriting)."""
    out = _run("typeset_lines", lines)
    out["typeset"] = [{"id": m["target"], "text": m["text"], "latex": m.get("latex")}
                      for m in out["marks"] if m.get("kind") == "typeset"]
    return out


@mcp.tool()
def sketch_graph(lines: list[str], show_answer: bool = False) -> dict[str, Any]:
    """Points for a small graph of the function being integrated or differentiated (fn, xrange,
    yrange, points), plus the antiderivative as an overlay once the student has it, or when
    show_answer is true."""
    out = _run("sketch_graph", lines, args={"show_answer": show_answer})
    out["graph"] = next((m for m in out["marks"] if m.get("kind") == "graph"), None)
    return out


@mcp.tool()
def differentiate_check(lines: list[str], line_id: str | None = None) -> dict[str, Any]:
    """Check an antiderivative by differentiating it and comparing with the integrand. line_id
    (L1, L2, ...) picks the line; the default is the latest one."""
    return _run("differentiate_check", lines, args={"line_id": line_id})


# --------------------------------------------------------------------------- tools that use Meta


def _image_data_url(image: str) -> str:
    image = image.strip()
    if image.startswith("data:image/"):
        return image
    p = Path(image).expanduser()
    if not p.is_file():
        raise ValueError("image must be a data:image/... URL or the path of an image file")
    data = p.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image is larger than 3 MB")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    if not mime.startswith("image/"):
        raise ValueError("not an image file")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


@mcp.tool()
def read_handwriting(image: str) -> dict[str, Any]:
    """Read handwritten maths from a photo or screenshot of a page (a file path or a data:image URL)
    with Muse Spark's vision. Returns one string per line, ready for the other tools.
    Needs MODEL_API_KEY."""
    if meta_client.is_offline():
        return {"lines": [], "error": "MODEL_API_KEY is not set, so handwriting can't be read"}
    read = meta_client.read_page(_image_data_url(image))
    return {"lines": [l["text"] for l in read], "error": meta_client.last_read_error if not read else None}


@mcp.tool()
def ask_margin(lines: list[str], command: str = "help me", hint_level: int = 1) -> dict[str, Any]:
    """Margin's own agent: Muse Spark reads the request ("help me", "what's next?", "why?",
    "check my work", "graph it") and chains Margin's tools itself, up to four calls. Returns the
    tools it used (agent_trace) and the ink it chose. Without MODEL_API_KEY it routes by keywords."""
    page = _page(lines)
    box = skills.Toolbox(page, hint_level)
    out = None
    if not meta_client.is_offline():
        out = meta_client.run_agent(command[:500], page, None, hint_level=box.level, toolbox=box)
    if out is None:
        intent = meta_client.keyword_intent(command)
        out = box.route(intent, f"{'offline' if meta_client.is_offline() else 'agent unavailable'}: keywords -> {intent}", command)
    return {"mode": out["mode"], "agent_trace": out["agent_trace"], "first_wrong": box.result.first_wrong,
            "verdict": box.result.verdict, "lines": _lines_out(box), "marks": _marks(box),
            "notes": [a["text"] for a in box.annotations if a.get("kind") == "note"],
            "written_steps": [a["text"] for a in box.annotations if a.get("kind") in ("write", "ghost")]}


# --------------------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Margin MCP server")
    ap.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP on this port instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)
    if a.http:
        mcp.settings.host, mcp.settings.port = a.host, a.http
        mcp.run("streamable-http")
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
