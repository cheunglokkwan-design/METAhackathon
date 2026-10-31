# Margin: API contract between the notebook page (web/) and the agent server (server/)

Product: a calculus assistant that lives inside a paper-like notebook. The student writes
with a pen. When they call it ("Hey Meta, help me with this integral"), the assistant
reads the page, checks every step with SymPy, and **writes a short hint in handwriting
ink right next to the wrong line** (with a circle or arrow on it). If the student says
"just give me the answer", it writes the remaining steps on the page.
No chat box anywhere: all output is ink on the page.

## Layout

- `server/` Python 3.11+, FastAPI, SymPy, `openai` SDK pointed at the Meta Model API.
  Serves `web/` as static files at `/`, API under `/api`. Run: `uvicorn server.app:app`.
- `web/` plain HTML/CSS/JS (ES modules, no build step). Canvas pen input.

## Meta models

- Muse Spark 1.3 (`muse-spark-1.3`) via `https://api.meta.ai/v1`, OpenAI SDK, key in
  `MODEL_API_KEY`: reads the page image into math lines, runs the **agent loop** (picks
  tools, see below) and phrases hints. It never decides correctness and never writes maths
  on the page: SymPy does.
- Muse Voice Transcribe (`muse-voice-transcribe-1.0`): voice commands. Browser Web
  Speech API is the fallback.
- With no `MODEL_API_KEY`, the server runs in **offline mode**: no image reading (the page
  must contain typed lines, see `lines` below), rule-based hints, keyword routing.

## Agent loop (server/skills.py, meta_client.run_agent)

Tools, each executed locally by the SymPy referee; every annotation comes from a tool result:

| tool | does | response `mode` |
|------|------|-----------------|
| `check_steps` | ticks, circle + note on the first wrong line | `check` |
| `give_hint` | hint at `hint_level` (or a next-step nudge) | `hint` |
| `finish_answer` | `write`s the remaining steps | `answer` |
| `typeset_lines` | a `typeset` annotation per readable line | `typeset` |
| `sketch_graph` | one `graph` annotation | `graph` |
| `differentiate_check` | d/dx of the student's antiderivative vs the integrand (derivative pages: same as check_steps) | `check` |

Online + `mode: "auto"`: Muse Spark gets the command, the page lines and the tool
definitions (`tool_choice: "auto"`), at most 4 model calls; a plain-text reply with no tool
call is routed by keywords. Offline, an explicit `mode`, or an API failure: keyword routing,
`tidy`/`typeset`/`neat` → typeset, `graph`/`plot`/`sketch` → graph, `check … differentiat…`
→ differentiate_check, `answer`/`finish`/`just give` → answer, `check`/`is this right` → check,
anything else → hint. Annotations from several tools are merged: exact duplicates dropped, at
most one `note` per line (first tool wins). `mode` is that of the first tool that ran.

## What the referee understands

- Indefinite integrals `∫ f dx` (checked up to a constant, `+ C` reminder on the last line).
- Definite integrals `∫_0^1 x^2 dx`, `∫_{0}^{π}`, `int from 0 to 1 x^2 dx`, with an
  evaluation step `[x^3/3]_0^1` or `x^3/3 |_0^1` (its antiderivative is checked by
  differentiating) and the final number. No `+ C` on these.
- Derivatives `d/dx (x^2 sin x)`, `d/dx [..]`, `\frac{d}{dx}`, `(x^2 sin x)'`, and
  `f'(x)` / `y'` / `dy/dx` after a definition line `f(x) = …` or `y = …`. Intermediate lines
  such as `= (x^2)' sin x + x^2 (sin x)'` are checked too. Mistakes named: product rule
  (forgot a term, multiplied derivatives), chain rule (missing inner derivative), power rule,
  sign slips on sin/cos derivatives, integrated instead of differentiated.

## POST /api/assist

Request JSON:
```json
{
  "image": "data:image/png;base64,...",   // optional, full page snapshot
  "lines": [                               // optional; lines the page already knows as text
    {"id": "L1", "text": "∫ x cos(x) dx", "bbox": [x, y, w, h]}
  ],
  "command": "hey meta help me with this integral",   // raw words from voice/typed
  "mode": "auto",          // "hint" | "answer" | "check" | "typeset" | "graph" | "auto" (derive from command)
  "hint_level": 1          // 1 gentle nudge, 2 name the rule, 3 show the fix
}
```
`bbox` is in page pixels (CSS px of the page, top-left origin).

Response JSON:
```json
{
  "mode": "hint",
  "offline": false,
  "lines": [ {"id": "L1", "text": "∫ x cos(x) dx", "latex": "...", "bbox": [..], "status": "ok|wrong|unchecked|start"} ],
  "first_wrong": "L3",            // id or null
  "verdict": "short machine reason from SymPy, e.g. 'derivative of line 3 is -x sin x, expected x cos x'",
  "annotations": [
    {"kind": "circle", "target": "L3"},
    {"kind": "note",   "target": "L3", "text": "You dropped a minus sign here", "side": "right"},
    {"kind": "write",  "after": "L4", "text": "= x sin(x) + cos(x) + C"},   // answer mode: new lines appended below
    {"kind": "tick",   "target": "L2"},
    {"kind": "underline", "target": "L4"},                     // later lines that inherit the error
    {"kind": "ghost",  "after": "L3", "text": "= x sin(x) + cos(x) + C"},  // hint_level 3 (hint mode)
    {"kind": "typeset", "target": "L1", "text": "∫ x·cos x dx", "latex": "\\int x \\cos{\\left(x \\right)}\\, dx"},
    {"kind": "graph", "after": "L3", "fn": "x sin x + cos x", "points": [[-6.28, 1.0], ...],
     "xrange": [-6.283, 6.283], "yrange": [-5.9, 4.5],
     "overlay": [[-6.28, -6.28], ...], "overlay_fn": "x cos x"}
  ],
  "agent_trace": [                 // the agent's steps, for a tiny log on the page
    {"tool": "route", "summary": "offline: keywords -> hint"},
    {"tool": "give_hint", "summary": "hint level 1: L3 wrong (sign)"}
  ]
}
```
`mode` is one of `hint | answer | check | typeset | graph` (request `mode` accepts the same
plus `auto`). `agent_trace` is always present (may hold just one entry); each `summary` is
≤ 90 chars. Trace tools: the six tools above, plus `read_page` (lines read from the image),
`route` (keyword/explicit routing), `muse-spark` (a model reply that was not a tool call),
`none` (no lines).

Annotation kinds:

| kind | fields | when |
|------|--------|------|
| `tick` | `target` | each `ok` line before the first wrong one |
| `circle` | `target` | the first wrong line |
| `note` | `target`, `text` (≤ ~8 words), `side` | hint next to a line. `target` may be `null` (no lines at all, e.g. offline with an image only): render it at the top of the page |
| `underline` | `target` | every later line after the first wrong one (it inherits the error; those lines have status `unchecked`) |
| `ghost` | `after`, `text` | hint_level 3 in hint mode: the corrected version of the wrong line (or the next step when nothing is wrong), drawn faintly under `after` instead of revealing the whole answer |
| `write` | `after`, `text` | answer mode: remaining steps. Several writes share the same `after` (the last line): stack them in array order |
| `typeset` | `target`, `text`, `latex` | typeset mode: the neat typed version of that line. `text` is unicode in the student's own order (`∫ x·cos x dx`, `x²`, `∫₀¹`, `[x³/3]₀¹`, `−`); `latex` is SymPy's reading of the line (may reorder terms). Unreadable lines get none |
| `graph` | `after`, `fn`, `points`, `xrange`, `yrange`; optional `overlay`, `overlay_fn`, `shade` | graph mode: a small plot under `after` (the last line). `points` are ~120 `[x, y]` samples of `fn` (non-finite values skipped, so there can be gaps; points far outside `yrange` are dropped: clip to `yrange`). Indefinite integral: `fn` is the integrand until the student's answer is correct, then `fn` is the antiderivative and `overlay` the integrand. Derivative: `fn` is the function, `overlay` its derivative once finished. Definite integral: `fn` is the integrand and `shade: [a, b]` is the area to fill. Nothing graphable → a `note` instead |

Server notes: if `lines` has any non-empty text, those lines are used as is and the
image is ignored. Otherwise the image is read by Muse Spark (online only), and the server
assigns ids `L1..Ln` with bboxes in **image pixels** (send the snapshot at CSS-pixel
scale, or scale them on the page).
The page renders every annotation in handwriting style (font "Caveat", ink colour
distinct from the student's), animated as if being written.

## POST /api/transcribe

multipart `audio` file (webm/ogg/wav). Returns `{"text": "..."}`; 503 when offline so the
page falls back to Web Speech API.

## GET /api/health

`{"ok": true, "offline": bool, "model": "muse-spark-1.3"}`
