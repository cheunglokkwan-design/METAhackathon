"""The agent's tools. Each one runs locally (SymPy referee) and returns ink annotations.

The Muse Spark agent (meta_client.run_agent) only chooses which tools to call; every
annotation on the page comes out of a tool here, never out of the model's own text.
Offline, `Toolbox.route(intent)` picks the tool from keywords.

Tools: check_steps, give_hint, finish_answer, next_step, explain_rule, typeset_lines,
sketch_graph, differentiate_check. See docs/CONTRACT.md for the annotation kinds.
Every `write`, `ghost` and `typeset` annotation carries `latex` (SymPy's reading of its text).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import sympy as sp

from . import checker, explain, meta_client, render

# Which response "mode" a tool stands for.
TOOL_MODE = {
    "check_steps": "check", "give_hint": "hint", "finish_answer": "answer",
    "typeset_lines": "typeset", "sketch_graph": "graph", "differentiate_check": "check",
    "next_step": "next", "explain_rule": "why", "sketch_3d": "graph", "draw_picture": "picture",
}
# Offline / explicit intent -> tool.
INTENT_TOOL = {
    "hint": "give_hint", "answer": "finish_answer", "check": "check_steps",
    "typeset": "typeset_lines", "graph": "sketch_graph", "verify": "differentiate_check",
    "next": "next_step", "why": "explain_rule", "picture": "draw_picture",
}

# OpenAI-style tool definitions sent to Muse Spark (chat completions `tools`).
TOOL_SPECS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "check_steps",
        "description": "Check every line on the page with the SymPy referee: ticks on correct lines, "
                       "a circle and a short note on the first wrong line. Use when the student asks "
                       "whether their work is right.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "give_hint",
        "description": "Write one short hint in ink next to the first wrong line (or a nudge toward the "
                       "next step if everything is right). Never reveals the answer below hint level 3. "
                       "Default tool when the student asks for help.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "finish_answer",
        "description": "Write the remaining steps and the final answer below the last line. Only when "
                       "the student explicitly asks for the answer or the solution.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "next_step",
        "description": "Write ONLY the next single step after the student's last correct line (checked "
                       "by the referee first). If a line is wrong it gives a hint on that line instead. "
                       "Use when the student asks what's next, for the next step, or to continue.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "explain_rule",
        "description": "Write a short explanation (the rule and its formula) beside the latest step, or "
                       "beside the first mistake if there is one. Use when the student asks why, asks "
                       "you to explain, or asks how a step works.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "typeset_lines",
        "description": "Write a neat typed version of each line (x², ∫, ·) beside the handwriting. Use "
                       "when the student asks to tidy, typeset or neaten the page.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "sketch_graph",
        "description": "Sketch a small graph under the work: the function being integrated or "
                       "differentiated, plus the antiderivative once it is known. Use when the student "
                       "asks to graph, plot or sketch.",
        "parameters": {"type": "object", "properties": {
            "show_answer": {"type": "boolean",
                            "description": "Also draw the antiderivative/derivative even if the student "
                                           "has not finished. Only if they asked for the answer."}}}}},
    {"type": "function", "function": {
        "name": "sketch_3d",
        "description": "Sketch a 3D surface z = f(x, y) as a wireframe on the page. Use when the student "
                       "asks for a 3D graph or surface, or the function has two variables (x and y).",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string",
                           "description": "f(x, y) to draw, e.g. 'x^2 - y^2'. Leave out to use the page."}}}}},
    {"type": "function", "function": {
        "name": "draw_picture",
        "description": "Draw a picture or diagram on the page with Muse Image (a pencil sketch, no text). "
                       "Use when the student asks for a picture, image or diagram of a situation, e.g. a "
                       "related-rates ladder or a solid of revolution. Not for graphs of functions.",
        "parameters": {"type": "object", "properties": {
            "description": {"type": "string",
                            "description": "What to draw, in a short phrase, e.g. 'a cone filling with water'."}},
            "required": ["description"]}}},
    {"type": "function", "function": {
        "name": "differentiate_check",
        "description": "Verify by differentiating: d/dx of the student's antiderivative is compared "
                       "with the integrand (or derivative steps are checked). Use when the student asks "
                       "to check by differentiating.",
        "parameters": {"type": "object", "properties": {
            "line_id": {"type": "string", "description": "Line to differentiate, e.g. L3. Default: last."}}}}},
]


def _note(target: str | None, text: str) -> dict[str, Any]:
    return {"kind": "note", "target": target, "text": text, "side": "right"}


MATH_KINDS = ("write", "ghost", "typeset")


def with_latex(a: dict[str, Any]) -> dict[str, Any]:
    """Give a maths-bearing annotation (write/ghost/typeset) its `latex`, if it has none."""
    if a.get("kind") in MATH_KINDS and not a.get("latex") and a.get("text"):
        a["latex"] = checker.latex_for_text(a["text"])
    return a


# --------------------------------------------------------------------------- annotate


def annotate(result: checker.CheckResult, mode: str, level: int) -> list[dict[str, Any]]:
    """Turn a CheckResult into ink annotations per docs/CONTRACT.md (hint/check/answer)."""
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
        ann.append(_note(wrong.id, hint))
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
        if steps and not wrong:
            ann.extend(_degree_note(result, last))
        if not steps and not wrong:
            text = last.note or ("That's the answer!" if result.finished else "Nothing left to solve")
            ann.append(_note(last.id, text))
    return [with_latex(a) for a in ann]


def _all_correct_notes(result: checker.CheckResult, mode: str, level: int,
                       last: checker.LineResult) -> list[dict[str, Any]]:
    """Notes when no step is wrong: + C reminder, praise, or a nudge to the next step."""
    if last.note:
        return [_note(last.id, last.note)]
    if last.parsed.asks_value and last.status in ("ok", "start") and result.base_value is not None:
        return _value_notes(result, mode, level, last)
    if not any(r.status in ("ok", "start") for r in result.lines):
        return [_note(last.id, "I can't read this line yet")]
    nudges = checker.next_step_hints(result) if mode == "hint" else None
    if not nudges:
        text = "All correct!" if (result.finished or result.chain == "algebra") else "Correct so far"
        return [_note(last.id, text)]
    text = meta_client.phrase_hint("all steps correct; next step needed", last.text, level, nudges[level])
    ann = [_note(last.id, text if level < 3 else nudges[1])]
    if level >= 3 and nudges[3].startswith("Next: "):
        ann.append({"kind": "ghost", "after": last.id, "text": nudges[3][len("Next: "):]})
    return ann


def _value_notes(result: checker.CheckResult, mode: str, level: int,
                 last: checker.LineResult) -> list[dict[str, Any]]:
    """The line ends in a bare '=' ("30 sin 30 ="): help the student work out its value."""
    ev = checker.evaluate_value(result.base_value, True)
    if ev is None:
        return [_note(last.id, "I can't work this one out")]
    value, why = ev
    if mode != "hint":
        return [_note(last.id, "Readable: now work out what it equals")]
    if level == 1:
        return [_note(last.id, f"{why}: work out each trig value first" if why else "Simplify it to one value")]
    if level == 2:
        trig = checker.trig_values(result.base_value)
        return [_note(last.id, "; ".join(trig[:2]) if trig else "Combine the terms into one value")]
    return [_note(last.id, why or "Here's the value"), {"kind": "ghost", "after": last.id, "text": value}]


def _degree_note(result: checker.CheckResult, last: checker.LineResult) -> list[dict[str, Any]]:
    if not last.parsed.asks_value or result.base_value is None:
        return []
    ev = checker.evaluate_value(result.base_value, True)
    return [_note(last.id, ev[1])] if ev and ev[1] else []


# --------------------------------------------------------------------------- toolbox


class Toolbox:
    """One page, checked once; tools read from it and add annotations (deduplicated)."""

    def __init__(self, lines: list[dict[str, Any]], level: int = 1):
        self.lines = lines
        self.level = max(1, min(3, int(level or 1)))
        self.result = checker.check_lines(lines)
        self.annotations: list[dict[str, Any]] = []
        self.trace: list[dict[str, str]] = []
        self.modes: list[str] = []
        self._noted: set[Any] = set()
        self.tools: dict[str, Callable[..., tuple[list[dict[str, Any]], str]]] = {
            "check_steps": self.check_steps, "give_hint": self.give_hint,
            "finish_answer": self.finish_answer, "typeset_lines": self.typeset_lines,
            "sketch_graph": self.sketch_graph, "differentiate_check": self.differentiate_check,
            "next_step": self.next_step, "explain_rule": self.explain_rule,
            "sketch_3d": self.sketch_3d, "draw_picture": self.draw_picture,
        }

    # ------------------------------------------------------------------ running

    def run(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute one tool; returns a compact result for the model (and the trace)."""
        fn = self.tools.get(name)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        mode = TOOL_MODE.get(name)
        try:
            out = fn(**_clean_args(fn, args or {}))
            ann, summary = out[0], out[1]
            mode = out[2] if len(out) > 2 else mode  # a tool may answer in another mode
        except Exception as exc:  # a tool must never take the page down
            ann, summary = [], f"failed: {type(exc).__name__}"
        added = self._add([with_latex(a) for a in ann])
        self.trace.append({"tool": name, "summary": summary[:90]})
        if mode:
            self.modes.append(mode)
        return {"summary": summary, "annotations_added": added, "first_wrong": self.result.first_wrong,
                "finished": self.result.finished}

    def route(self, intent: str, why: str, command: str = "") -> dict[str, Any]:
        """Offline or explicit-mode path: one tool picked by intent."""
        tool = INTENT_TOOL.get(intent, "give_hint")
        self.trace.append({"tool": "route", "summary": why[:90]})
        self.run(tool, {"description": picture_subject(command)} if tool == "draw_picture" else {})
        return self.output()

    def output(self) -> dict[str, Any]:
        mode = self.modes[0] if self.modes else "hint"
        return {"mode": mode, "annotations": self.annotations, "agent_trace": self.trace}

    def _add(self, ann: list[dict[str, Any]]) -> int:
        n = 0
        for a in ann:
            key = json.dumps(a, sort_keys=True)
            note_key = ("note", a.get("target")) if a["kind"] == "note" else None
            if key in self._noted or (note_key and note_key in self._noted):
                continue  # one note per line; first tool wins
            self._noted.add(key)
            if note_key:
                self._noted.add(note_key)
            self.annotations.append(a)
            n += 1
        return n

    # ------------------------------------------------------------------ tools

    def _verdict_summary(self) -> str:
        r = self.result
        if r.first_wrong:
            kind = r.wrong.diagnosis.kind.replace("_", " ")
            return f"{r.first_wrong} wrong ({kind})"
        good = sum(1 for l in r.lines if l.status == "ok")
        return f"{good} step(s) ok" + (", finished" if r.finished else "")

    def check_steps(self) -> tuple[list[dict[str, Any]], str]:
        return annotate(self.result, "check", 1), "checked: " + self._verdict_summary()

    def give_hint(self) -> tuple[list[dict[str, Any]], str]:
        ann = annotate(self.result, "hint", self.level)
        return ann, f"hint level {self.level}: " + self._verdict_summary()

    def finish_answer(self) -> tuple[list[dict[str, Any]], str]:
        ann = annotate(self.result, "answer", self.level)
        n = sum(1 for a in ann if a["kind"] == "write")
        return ann, f"wrote {n} step(s)" + (f"; {self.result.first_wrong} wrong" if self.result.first_wrong else "")

    def next_step(self) -> tuple[list[dict[str, Any]], str, str]:
        """One step past the last correct line, verified by the referee; a hint if a line is wrong."""
        r = self.result
        if r.wrong is not None:
            ann = annotate(r, "hint", self.level)
            return ann, f"next: {r.first_wrong} wrong, hint instead", "hint"
        good = [l for l in r.lines if l.status in ("ok", "start")]
        if not good:
            last = r.lines[-1].id if r.lines else None
            return [_note(last, "I can't read this line yet")], "next: nothing readable", "next"
        after = good[-1]
        if after.note:  # e.g. "Don't forget + C": the only thing left
            return [_note(after.id, after.note)], "next: " + after.note, "next"
        steps = checker.solve_remaining(r)
        if not steps:
            text = "That's the answer!" if r.finished else "Nothing left to solve"
            return [_note(after.id, text)], "next: nothing left", "next"
        step = steps[0]
        if not self._step_checks(step):
            nudges = checker.next_step_hints(r)
            text = nudges[1] if nudges else "Try the next step yourself"
            return [_note(after.id, text)], "next: step failed the referee; nudge", "next"
        return ([{"kind": "write", "after": after.id, "text": step,
                  "latex": checker.latex_for_text(step)}] + _degree_note(r, after), f"next step: {step}", "next")

    def _step_checks(self, step: str) -> bool:
        """The referee accepts `step` written under the page's lines."""
        page = [{"id": str(l.get("id") or f"L{i}"), "text": str(l.get("text") or "")}
                for i, l in enumerate(self.lines, 1)]
        res = checker.check_lines(page + [{"id": "_next", "text": step}])
        return res.first_wrong is None and res.lines[-1].status == "ok"

    def explain_rule(self) -> tuple[list[dict[str, Any]], str]:
        """A short why-note (rule + formula) beside the latest step or the first mistake."""
        ex = explain.explanation(self.result, reword=meta_client.phrase_why)
        note = _note(ex["target"], "\n".join(ex["lines"]))
        if ex["latex"]:
            note["latex"] = ex["latex"]
            note["latex_line"] = ex["latex_line"]
        ann = [{"kind": "circle", "target": ex["target"]}] if ex["mistake"] and self.result.wrong else []
        what = "mistake" if ex["mistake"] else "step"
        return ann + [note], f"why: {ex['topic'].replace('_', ' ')} ({what} at {ex['target']})"

    def typeset_lines(self) -> tuple[list[dict[str, Any]], str]:
        ann = []
        for r in self.result.lines:
            t = render.typeset_line(r.parsed)
            if t:
                ann.append({"kind": "typeset", "target": r.id, "text": t[0], "latex": t[1]})
        skipped = len(self.result.lines) - len(ann)
        return ann, f"typeset {len(ann)} line(s)" + (f", {skipped} unreadable" if skipped else "")

    def sketch_graph(self, show_answer: bool = False) -> tuple[list[dict[str, Any]], str]:
        g = render.graph_for(self.result, reveal=bool(show_answer))
        last = self.result.lines[-1].id if self.result.lines else None
        if g is None:
            if render.surface_expr(self.result) is not None:      # two variables: a 3D surface instead
                return self.sketch_3d()
            return [_note(last, "I can't graph this one")], "nothing to graph"
        g = {"kind": "graph", "after": last, **{k: v for k, v in g.items() if k != "kind"}}
        extra = f" with {g['overlay_fn']}" if "overlay_fn" in g else ""
        return [g], f"graphed {g['fn']}{extra}"

    def sketch_3d(self, expression: str | None = None) -> tuple[list[dict[str, Any]], str]:
        last = self.result.lines[-1].id if self.result.lines else None
        expr = render.surface_expr(self.result, expression)
        g = render.graph3d_for(expr) if expr is not None else None
        if g is None:
            return [_note(last, "I need a function of x and y for 3D")], "nothing to draw in 3D"
        return [{**g, "after": last}], f"3D surface {g['fn']}"

    def draw_picture(self, description: str = "") -> tuple[list[dict[str, Any]], str]:
        last = self.result.lines[-1].id if self.result.lines else None
        description = " ".join(str(description or "").split())[:300]
        if not description:
            return [_note(last, "Tell me what to draw")], "no description"
        if meta_client.is_offline():
            return [_note(last, "Pictures need Meta online")], "offline: no picture"
        url = meta_client.generate_picture(description)
        if url is None:
            return [_note(last, "I couldn't draw that one")], "picture failed"
        return [{"kind": "picture", "after": last, "image": url, "caption": description[:80]}], f"drew: {description[:60]}"

    def differentiate_check(self, line_id: str | None = None) -> tuple[list[dict[str, Any]], str]:
        r = self.result
        if r.chain == "derivative":
            ann, summary = self.check_steps()
            return ann, "derivative " + summary
        if r.chain != "integral":
            return [_note(r.lines[-1].id if r.lines else None, "Nothing to differentiate here")], "no integral"
        start = next((l for l in reversed(r.lines) if l.status == "start"), None)
        f = checker._bare_integrand(start.parsed.parts[0]) if start else None
        cands = [l for l in r.lines if l.parsed.ok and l.status in ("ok", "wrong")
                 and not l.parsed.parts[-1].has(sp.Integral)]
        target = next((l for l in cands if l.id == line_id), cands[-1] if cands else None)
        if f is None or target is None:
            last = r.lines[-1].id
            return [_note(last, "Finish the integral, then differentiate")], "no antiderivative yet"
        F = target.parsed.parts[-1]
        dF = sp.simplify(sp.diff(checker._plain(F), r.var))
        ok = checker._is_zero(dF - f, r.var)
        if ok:
            return ([{"kind": "tick", "target": target.id},
                      _note(target.id, f"d/dx gives back {checker.pretty(f)}")],
                     f"d/dx({target.id}) = integrand")
        return ([{"kind": "circle", "target": target.id},
                  _note(target.id, f"d/dx gives {checker.pretty(dF)}, not {checker.pretty(f)}")],
                 f"d/dx({target.id}) != integrand")


_PICTURE_ASK = re.compile(r"^(?:hey|hi|ok)?\W*(?:meta)?\W*(?:can you |could you |please )*"
                          r"(?:draw|make|show|sketch|give)?\s*(?:me\s+)?(?:a |an |the )?"
                          r"(?:picture|image|diagram|illustration|drawing)?\s*(?:of|showing|for)?\s*", re.I)


def picture_subject(command: str) -> str:
    """"Hey Meta, draw me a picture of a ladder on a wall" -> "a ladder on a wall"."""
    c = " ".join(str(command or "").split())
    rest = _PICTURE_ASK.sub("", c, count=1).strip(" .?!")
    return rest or c


def _clean_args(fn: Callable, args: dict[str, Any]) -> dict[str, Any]:
    """Keep only the keyword arguments the tool accepts (models add extras)."""
    params = fn.__code__.co_varnames[1:fn.__code__.co_argcount]
    return {k: v for k, v in args.items() if k in params}
