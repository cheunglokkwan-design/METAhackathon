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
- Muse Voice Transcribe (`muse-voice-transcribe-1.0`): voice commands when the browser's
  Web Speech API is missing or fails (offline, speech service disabled). Web Speech is
  tried first.
- With no `MODEL_API_KEY`, the server runs in **offline mode**: no image reading (the page
  must contain typed lines, see `lines` below), rule-based hints, keyword routing.
- Optional **decision router** (Muse Glimmer or any small model behind an OpenAI-compatible
  endpoint, `ROUTER_BASE_URL` / `ROUTER_MODEL` / `ROUTER_API_KEY`): off unless configured.
  When on, one short call picks intent, tool and hint level for `mode: "auto"`; Spark's agent
  loop and then keywords are the fallbacks. See [ROUTER.md](ROUTER.md).

## Agent loop (server/skills.py, meta_client.run_agent)

Tools, each executed locally by the SymPy referee; every annotation comes from a tool result:

| tool | does | response `mode` |
|------|------|-----------------|
| `check_steps` | ticks, circle + note on the first wrong line | `check` |
| `give_hint` | hint at `hint_level` (or a next-step nudge) | `hint` |
| `finish_answer` | `write`s the remaining steps | `answer` |
| `next_step` | `write`s ONLY the next single step after the last correct line (verified by the referee first); if a line is wrong, hint behaviour instead (circle + note) | `next` (`hint` when a line is wrong) |
| `explain_rule` | a short "why" `note` (rule + formula) beside the latest step, or beside the first mistake (+ `circle`) | `why` |
| `typeset_lines` | a `typeset` annotation per readable line | `typeset` |
| `sketch_graph` | one `graph` annotation (a `graph3d` when the function has two variables) | `graph` |
| `sketch_3d` | one `graph3d` annotation: `fn`, `xs`, `ys`, `z` (rows over `ys`, `null` where undefined), `zrange` | `graph` |
| `draw_picture` | one `picture` annotation: `image` (a `data:image/...;base64` URL from Muse Image), `caption` | `picture` |
| `differentiate_check` | d/dx of the student's antiderivative vs the integrand (derivative pages: same as check_steps) | `check` |

Online + `mode: "auto"`: Muse Spark gets the command, the page lines and the tool
definitions (`tool_choice: "auto"`), at most 4 model calls; a plain-text reply with no tool
call is routed by keywords. Offline, an explicit `mode`, or an API failure: keyword routing,
in this order: `tidy`/`typeset`/`neat` → typeset, `graph`/`plot`/`sketch` → graph,
`check … differentiat…` → differentiate_check, `what's next`/`next step`/`what now`/`continue`/
`keep going` → next_step, `why`/`explain`/`how does this work` → explain_rule,
`answer`/`finish`/`just give` → answer, `check`/`is this right` → check, anything else → hint. Annotations from several tools are merged: exact duplicates dropped, at
most one `note` per line (first tool wins). `mode` is that of the first tool that ran.

**next** (Writey's "what's next?"): the first step of the referee's solution from the last
correct expression, e.g. `∫ x cos x dx` → `= x sin x - ∫ sin x dx` (by parts), `∫ x^2 dx` →
`= x^3/3 + C`, `d/dx (x^2 sin x)` → `= (x^2)' sin x + x^2 (sin x)'`, `∫_0^1 x^2 dx` →
`= [x^3/3]_0^1` → `= 1/3`. Exactly one `write` (with `latex`), `after` = the last `ok`/`start`
line. Nothing left: a `note` ("That's the answer!", or "Don't forget + C"). A step the referee
does not accept is never written (a nudge `note` instead).

**why** (Writey's "why does this work?"): one `note` with up to 3 lines (each ≤ 12 words,
joined by `"\n"` in `text`): the idea, the formula, and a context line from SymPy (`Here u = x,
dv = cos x dx`) or a tip. Topic from the referee: the first mistake's diagnosis (product rule,
chain rule, power rule, trig derivative sign, sign of ∫ sin, constant factor, missing + C,
definite-integral slips → FTC, differentiated instead of integrated, …), else the technique of
the latest step (by parts, u-sub, power rule, chain/product rule, FTC). Formulas always come
from fixed templates; online, Muse Spark may reword only the plain-word lines (rewrites with
maths, digits or > 12 words are dropped).

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
  "mode": "auto",          // "hint" | "answer" | "check" | "typeset" | "graph" | "next" | "why" | "auto" (derive from command)
  "hint_level": 1,         // 1 gentle nudge, 2 name the rule, 3 show the fix
  // Optional, used only when the decision router is on (then hint_level is the highest level allowed):
  "hints_given": 0,        // hints already given on the current line
  "wrote_since_hint": false,   // the student wrote something after the last hint
  "last_command": ""       // the previous command
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
    {"kind": "write",  "after": "L4", "text": "= x sin(x) + cos(x) + C",
     "latex": "= x \\sin{\\left(x \\right)} + \\cos{\\left(x \\right)} + C"},   // answer/next mode: new lines appended below
    {"kind": "tick",   "target": "L2"},
    {"kind": "underline", "target": "L4"},                     // later lines that inherit the error
    {"kind": "ghost",  "after": "L3", "text": "= x sin(x) + cos(x) + C", "latex": "= ..."},  // hint_level 3 (hint mode)
    {"kind": "note",   "target": "L2", "side": "right",                    // why mode
     "text": "Parts swaps a hard integral for an easier one.\n∫ u dv = uv − ∫ v du\nHere u = x, dv = cos x dx",
     "latex": "\\int u\\,dv = uv - \\int v\\,du", "latex_line": 1},
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
`mode` is one of `hint | answer | check | typeset | graph | next | why` (request `mode`
accepts the same plus `auto`). `agent_trace` is always present (may hold just one entry); each `summary` is
≤ 90 chars. Trace tools: the eight tools above, plus `read_page` (lines read from the image),
`route` (keyword/explicit routing), `muse-spark` (a model reply that was not a tool call),
`router` (the decision router's choice, or why it fell back), `none` (no lines).

With the decision router on, a `mode: "auto"` response also has
`"router": {"state": {...}, "path": "router" | "spark" | "keywords", "decision": {...} | null}`:
the router's input (command, SymPy verdict, session) and its validated decision, logged with the
interaction so consented sessions can become training rows.

Annotation kinds:

| kind | fields | when |
|------|--------|------|
| `tick` | `target` | each `ok` line before the first wrong one |
| `circle` | `target` | the first wrong line |
| `note` | `target`, `text` (≤ ~8 words), `side`; optional `latex`, `latex_line` | hint next to a line. `target` may be `null` (no lines at all, e.g. offline with an image only): render it at the top of the page. Why mode: `text` may hold up to 3 lines separated by `"\n"` (≤ 12 words each); when one of them is a formula, `latex` is that formula and `latex_line` its 0-based index in the split `text` (draw the LaTeX in place of that line, or keep the plain line as a fallback). Other notes are plain text with no `latex` |
| `underline` | `target` | every later line after the first wrong one (it inherits the error; those lines have status `unchecked`) |
| `ghost` | `after`, `text`, `latex` | hint_level 3 in hint mode: the corrected version of the wrong line (or the next step when nothing is wrong), drawn faintly under `after` instead of revealing the whole answer |
| `write` | `after`, `text`, `latex` | answer mode: remaining steps. Several writes share the same `after` (the last line): stack them in array order. Next mode: exactly one write after the last correct line |
| `typeset` | `target`, `text`, `latex` | typeset mode: the neat typed version of that line. `text` is unicode in the student's own order (`∫ x·cos x dx`, `x²`, `∫₀¹`, `[x³/3]₀¹`, `−`); `latex` is SymPy's reading of the line (may reorder terms). Unreadable lines get none |
| `graph` | `after`, `fn`, `points`, `xrange`, `yrange`; optional `overlay`, `overlay_fn`, `shade` | graph mode: a small plot under `after` (the last line). `points` are ~120 `[x, y]` samples of `fn` (non-finite values skipped, so there can be gaps; points far outside `yrange` are dropped: clip to `yrange`). Indefinite integral: `fn` is the integrand until the student's answer is correct, then `fn` is the antiderivative and `overlay` the integrand. Derivative: `fn` is the function, `overlay` its derivative once finished. Definite integral: `fn` is the integrand and `shade: [a, b]` is the area to fill. Nothing graphable → a `note` instead |

`latex` on `write`/`ghost`/`typeset` is SymPy's LaTeX of the parsed `text` (may reorder terms),
keeping a leading `= ` and a trailing ` + C`; never empty (an unreadable part becomes `\text{…}`).

Server notes: if `lines` has any non-empty text, those lines are used as is and the
image is ignored. Otherwise the image is read by Muse Spark (online only), and the server
assigns ids `L1..Ln` with bboxes in **image pixels** (send the snapshot at CSS-pixel
scale, or scale them on the page).
The page renders every annotation in handwriting style (font "Caveat", ink colour
distinct from the student's), animated as if being written.

## POST /api/transcribe

multipart `audio` file (webm/ogg/wav, at most 10 MB). Returns `{"text": "..."}`.
The page calls this only when Web Speech is missing or fails with `network`,
`service-not-allowed` or `language-not-supported`, and checks `/api/health` first.

| status | meaning |
|---|---|
| 400 | empty audio, or a non-numeric `Content-Length` |
| 411 | no `Content-Length` header |
| 413 | audio over 10 MB (refused from the header, before the body is read) |
| 503 | offline (no `MODEL_API_KEY`), rate limited, circuit open or upstream failure |

## GET /api/health

`{"ok": true, "offline": bool, "model": "muse-spark-1.3"}`
