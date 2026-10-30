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
  `MODEL_API_KEY`: reads the page image into math lines, classifies the student's intent,
  and phrases hints. It never decides correctness. SymPy does.
- Muse Voice Transcribe (`muse-voice-transcribe-1.0`): voice commands. Browser Web
  Speech API is the fallback.
- With no `MODEL_API_KEY`, the server runs in **offline mode**: no image reading (the page
  must contain typed lines, see `lines` below), rule-based hints, keyword intent.

## POST /api/assist

Request JSON:
```json
{
  "image": "data:image/png;base64,...",   // optional, full page snapshot
  "lines": [                               // optional; lines the page already knows as text
    {"id": "L1", "text": "∫ x cos(x) dx", "bbox": [x, y, w, h]}
  ],
  "command": "hey meta help me with this integral",   // raw words from voice/typed
  "mode": "auto",          // "hint" | "answer" | "check" | "auto" (derive from command)
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
    {"kind": "ghost",  "after": "L3", "text": "= x sin(x) + cos(x) + C"}  // hint_level 3 (hint mode)
  ]
}
```

Annotation kinds:

| kind | fields | when |
|------|--------|------|
| `tick` | `target` | each `ok` line before the first wrong one |
| `circle` | `target` | the first wrong line |
| `note` | `target`, `text` (≤ ~8 words), `side` | hint next to a line. `target` may be `null` (no lines at all, e.g. offline with an image only): render it at the top of the page |
| `underline` | `target` | every later line after the first wrong one (it inherits the error; those lines have status `unchecked`) |
| `ghost` | `after`, `text` | hint_level 3 in hint mode: the corrected version of the wrong line (or the next step when nothing is wrong), drawn faintly under `after` instead of revealing the whole answer |
| `write` | `after`, `text` | answer mode: remaining steps. Several writes share the same `after` (the last line): stack them in array order |

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
