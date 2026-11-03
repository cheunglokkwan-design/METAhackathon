// Student ink: pen and highlighter strokes, text boxes, images, eraser, lasso, undo and the
// page snapshot. Everything is kept in page coordinates (CSS px of the unscaled 1000 x 1400 page).
//
// Stored items (plain JSON, see serializeItem):
//   { type: 'stroke', color, size, sim, pts: [[x, y, p], ...], hl? }    hl: highlighter
//   { type: 'textbox', x, y, w, text, color }                           text rows split on \n, wrapped to w
//   { type: 'image', blob, x, y, w, h, bg? }                            blob: id in store.blobs; bg: page background
// Older pages hold { type: 'text', text, x, row, baseline, color } lines: normalizeItem turns them into text boxes.
//
// Meta's handwriting lives here too, one item per mark (a hint, a tick, an answer line), tagged by: 'meta'
// once Meta has finished writing it:
//   { type: 'ink', color, segs: [{ p: [[x, y], ...], w, o? }] }      its pen strokes, constant width w,
//                                                                   drawn through the same midpoints as Meta's pen;
//                                                                   the eraser and scratch-out remove single segs
//   { type: 'mark', html, box0, x, y, color, polys? }               a mark that is not plain strokes (shaded graphs):
//                                                                   finished SVG markup, moved by (x, y)
//   both with by, mk (note, tick, circle, write…), grp, say? / note? (its words), kept? (accepted, moved or
//   recoloured: Meta's next reply leaves it alone), dim? (an old hint faded once the student wrote on below it)
// Round-1 pages stored Meta's ink as one { type: 'stroke', mono: true, by: 'meta', grp } item per pen stroke:
// mergeMetaStrokes turns those into ink items when the page loads.
// Ownership rule: items with by: 'meta' are Meta's, and are left out of everything Meta reads (snapshot,
// inkRows, lines, signature, hasStrokes), so Meta never grades its own hint. The student can erase, lasso,
// move and recolour them like their own ink without changing that; moving a hint out of the way does not
// make it theirs. Only an explicit "Keep as mine" (lasso bar) adopts them: by becomes 'student-adopted',
// they take the student's pen colour and Meta reads them from then on. Older items have no `by` (student).
import { getStroke } from './vendor/perfect-freehand.js';   // vendored, MIT (web/vendor/perfect-freehand.LICENSE)
import { smoothStroke, recognizeShape } from './shapes.js';
import { imageFor, loadItemImages } from './images.js';

export const PAGE_W = 1000, PAGE_H = 1400, RULE = 44, TOP = 132, MARGIN_X = 96;
export const STUDENT_FONT = '"Patrick Hand", "Bradley Hand", "Segoe Print", cursive';
export const TEXT_PX = 28;
export const rowY = k => TOP + k * RULE;              // y of ruled line k
export const baselineOf = k => rowY(k) - 8;           // text sits just above the rule
export const rowAt = y => Math.max(0, Math.ceil((y - TOP) / RULE));

export const PEN_SIZES = { fine: 1.2, medium: 1.8, bold: 3 };
export const HL_SIZE = 22, HL_ALPHA = 0.42;
const HOLD_MS = 550;                                  // hold the pen still this long to snap a shape
export const HL_COLORS = ['#ffd426', '#7fe07a', '#ff86c3'];

const mctx = document.createElement('canvas').getContext('2d');
export const isMeta = it => it?.by === 'meta';
const mine = it => !isMeta(it);
// Painter for type 'mark' items (Meta's graphs); assistant.js registers it, so ink.js needs no SVG code.
let markPainter = null;
export function setMarkPainter(fn) { markPainter = fn; }
const inBox = ([x, y, w, h], px, py, pad = 0) => px >= x - pad && px <= x + w + pad && py >= y - pad && py <= y + h + pad;
const overlap = (a, b) => a[0] < b[0] + b[2] && b[0] < a[0] + a[2] && a[1] < b[1] + b[3] && b[1] < a[1] + a[3];
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const union = boxes => {
  if (!boxes.length) return null;
  const x0 = Math.min(...boxes.map(b => b[0])), y0 = Math.min(...boxes.map(b => b[1]));
  const x1 = Math.max(...boxes.map(b => b[0] + b[2])), y1 = Math.max(...boxes.map(b => b[1] + b[3]));
  return [x0, y0, x1 - x0, y1 - y0];
};

function distSeg(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, l = dx * dx + dy * dy;
  const t = l ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / l)) : 0;
  return Math.hypot(ax + t * dx - px, ay + t * dy - py);
}
function inPoly(poly, x, y) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function outlineToPath(pts) {
  if (!pts.length) return new Path2D();
  let d = `M${pts[0][0]},${pts[0][1]} Q`;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length];
    d += `${x0},${y0} ${(x0 + x1) / 2},${(y0 + y1) / 2} `;
  }
  return new Path2D(d + 'Z');
}
function polyline(pts) {
  const p = new Path2D();
  p.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length - 1; i++) p.quadraticCurveTo(pts[i][0], pts[i][1], (pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2);
  const l = pts.at(-1); p.lineTo(l[0], l[1]);
  return p;
}

// Pen: pressure-shaped outline (Apple Pencil pressure; simulated from speed for mouse and finger).
// Highlighter: a constant-width translucent band.
// Ink items (Meta's marks): every seg gets its path and box once.
function segGeometry(seg) {
  if (!seg._path) {
    seg._path = polyline(seg.p.length > 1 ? seg.p : [seg.p[0], [seg.p[0][0] + 0.4, seg.p[0][1] + 0.3]]);
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const [x, y] of seg.p) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
    const pad = (seg.w || 2) * 1.5;
    seg._box = [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
  }
  return seg;
}
const segHit = (seg, x, y, R) => inBox(segGeometry(seg)._box, x, y, R) && nearLine(seg.p, x, y, R + (seg.w || 2) / 2);
const pointsOf = it => it.type === 'ink' ? it.segs.flatMap(sg => sg.p) : it.pts || [];

// Round-1 per-stroke Meta items -> one ink item per mark (kept at the first stroke's place).
export function mergeMetaStrokes(items) {
  const groups = new Map(), out = [];
  for (const it of items) {
    if (!(it.type === 'stroke' && it.mono && it.by === 'meta' && it.grp)) { out.push(it); continue; }
    let g = groups.get(it.grp);
    if (!g) {
      g = { type: 'ink', color: it.color, segs: [] };
      for (const k of ['by', 'mk', 'grp', 'say', 'note', 'kept']) if (it[k] != null) g[k] = it[k];
      groups.set(it.grp, g); out.push(g);
    }
    g.say ??= it.say; g.note ??= it.note; if (it.kept) g.kept = true;
    g.segs.push({ p: it.pts.map(([x, y]) => [x, y]), w: it.size, ...(it.op != null ? { o: it.op } : {}) });
  }
  return out;
}

// A quick back-and-forth zigzag (scratch-out): at least 3 sideways reversals, flatter than it is wide.
export function isScratch(pts) {
  if (pts.length < 8) return false;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  const w = x1 - x0, h = y1 - y0;
  if (w < 24 || w > 320 || h > Math.max(40, w * 0.7) || h > 90) return false;
  let dir = 0, turns = 0, ext = pts[0][0], travel = 0;
  for (let i = 1; i < pts.length; i++) travel += Math.abs(pts[i][0] - pts[i - 1][0]);
  for (const [x] of pts) {
    if (!dir) { if (Math.abs(x - pts[0][0]) > 8) { dir = Math.sign(x - pts[0][0]); ext = x; } continue; }
    if ((x - ext) * dir > 0) ext = x;                       // further the same way
    else if (Math.abs(x - ext) > 8) { turns++; dir = -dir; ext = x; }   // came back: a reversal
  }
  return turns >= 3 && travel > w * 3;
}

function strokeGeometry(s, last = true) {
  if (s.hl || s.mono) return { path: polyline(s.pts), fill: false };
  const outline = getStroke(s.pts, { size: s.size * 2, thinning: 0.6, smoothing: 0.55, streamline: 0.4, simulatePressure: s.sim, last });
  return { path: outlineToPath(outline), fill: true };
}

// Text box rows: explicit line breaks, then word wrap at the box width.
const textFont = () => { mctx.font = `${TEXT_PX}px ${STUDENT_FONT}`; };
export function wrapRows(text, w) {
  textFont();
  const out = [];
  for (const para of String(text).split('\n')) {
    let line = '';
    for (const word of para.split(/(\s+)/)) {
      const next = line + word;
      if (line.trim() && mctx.measureText(next.trimEnd()).width > w) { out.push(line.trimEnd()); line = word.trimStart(); }
      else line = next;
    }
    out.push(line.trimEnd());
  }
  return out;
}
const rowBaseline = (it, i) => it.y + (i + 1) * RULE - 8;

export function paint(c, it) {
  if (it.type === 'image') {
    const img = imageFor(it.blob);
    if (img) c.drawImage(img, it.x, it.y, it.w, it.h);
    else {
      c.save(); c.fillStyle = 'rgba(0,0,0,.04)'; c.fillRect(it.x, it.y, it.w, it.h);
      c.strokeStyle = 'rgba(0,0,0,.15)'; c.setLineDash([6, 6]); c.strokeRect(it.x, it.y, it.w, it.h); c.restore();
    }
    return;
  }
  if (it.type === 'mark') {
    if (it.dim) { c.save(); c.globalAlpha = 0.4; }
    markPainter?.(c, it);
    if (it.dim) c.restore();
    return;
  }
  c.fillStyle = c.strokeStyle = it.color;
  if (it.type === 'ink') {
    c.save();
    c.lineCap = c.lineJoin = 'round';
    const k = it.dim ? 0.4 : 1;
    for (const seg of it.segs) {
      segGeometry(seg);
      c.globalAlpha = (seg.o ?? 1) * k; c.lineWidth = seg.w || 2;
      c.stroke(seg._path);
    }
    c.restore();
    return;
  }
  if (it.type === 'textbox') {
    c.font = `${TEXT_PX}px ${STUDENT_FONT}`;
    it.rows.forEach((r, i) => r && c.fillText(r, it.x, rowBaseline(it, i)));
    return;
  }
  if (it.hl) {
    c.save();
    c.globalCompositeOperation = 'multiply'; c.globalAlpha = HL_ALPHA;
    c.lineWidth = it.size; c.lineCap = 'round'; c.lineJoin = 'round';
    c.stroke(it.path);
    c.restore();
    return;
  }
  if (it.op != null) { c.save(); c.globalAlpha = it.op; }
  if (it.fill) c.fill(it.path);
  else { c.lineWidth = it.size; c.lineCap = c.lineJoin = 'round'; c.stroke(it.path); }
  if (it.op != null) c.restore();
}

// Layer order: page background, images, highlighter, then pen ink and text on top.
const layer = it => it.type === 'image' ? (it.bg ? 0 : 1) : it.hl ? 2 : 3;
export function paintItems(c, items) {
  for (let k = 0; k <= 3; k++) for (const it of items) if (layer(it) === k && !it.editing) paint(c, it);
}

const nearLine = (p, x, y, r) => {
  if (p.length === 1) return Math.hypot(p[0][0] - x, p[0][1] - y) < r;
  for (let i = 0; i < p.length - 1; i++) if (distSeg(x, y, p[i][0], p[i][1], p[i + 1][0], p[i + 1][1]) < r) return true;
  return false;
};
function hits(it, { x, y }, R) {
  if (it.type === 'image') return false;             // the eraser leaves pictures alone (lasso + delete removes them)
  if (!inBox(it.bbox, x, y, R)) return false;
  if (it.type === 'textbox') return true;
  if (it.type === 'mark') return !it.polys?.length || it.polys.some(p => nearLine(p, x - it.x, y - it.y, R + 2));
  if (it.type === 'ink') return it.segs.some(sg => segHit(sg, x, y, R));
  return nearLine(it.pts, x, y, R + it.size / 2);
}

// Offset from an editor's top edge to its text baseline for a given line height.
function editorBaseline(lineH) {
  textFont();
  const m = mctx.measureText('Hg');
  const asc = m.fontBoundingBoxAscent || TEXT_PX * 0.8, desc = m.fontBoundingBoxDescent || TEXT_PX * 0.25;
  return (lineH - asc - desc) / 2 + asc;
}

// Old one-line typed text -> a text box on the same ruled row.
export function normalizeItem(it) {
  if (it.type !== 'text') return it;
  return { type: 'textbox', x: it.x, y: it.baseline + 8 - RULE, w: Math.max(120, PAGE_W - it.x - 24), text: it.text, color: it.color };
}

// Adds the derived fields (outline path, rows, bbox) to a stored item.
export function prepareItem(it) {
  if (it.type === 'stroke') {
    Object.assign(it, strokeGeometry(it));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const [x, y] of it.pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
    const pad = it.hl ? it.size / 2 : it.size * 1.5;
    it.bbox = [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
  } else if (it.type === 'textbox') {
    it.rows = wrapRows(it.text, it.w);
    textFont();
    const cw = Math.max(8, ...it.rows.map(r => mctx.measureText(r).width));
    it.bbox = [it.x, it.y + RULE - 32, Math.min(cw, Math.max(it.w, 8)) || 8, (it.rows.length - 1) * RULE + 32];
    it.rowBoxes = it.rows.map((r, i) => r.trim() && [it.x, rowBaseline(it, i) - 24, mctx.measureText(r).width, 32]).filter(Boolean);
  } else if (it.type === 'image') {
    it.bbox = [it.x, it.y, it.w, it.h];
  } else if (it.type === 'ink') {
    const bs = it.segs.map(sg => segGeometry(sg)._box);
    it.bbox = bs.length ? union(bs) : [0, 0, 0, 0];
  } else if (it.type === 'mark') {
    it.x ??= 0; it.y ??= 0;
    const b = it.box0 || [0, 0, 1, 1];
    it.bbox = [b[0] + it.x, b[1] + it.y, b[2], b[3]];
  }
  return it;
}

// Plain JSON for storage: no Path2D, rounded coordinates.
const r2 = v => Math.round(v * 100) / 100;
const META_KEYS = ['by', 'mk', 'grp', 'say', 'note', 'kept', 'dim', 'rid'];
const withMeta = (s, it) => { for (const k of META_KEYS) if (it[k] != null && it[k] !== false) s[k] = it[k]; return s; };
export function serializeItem(it) {
  if (it.type === 'stroke') {
    const s = { type: 'stroke', color: it.color, size: it.size, sim: !!it.sim, pts: it.pts.map(([x, y, p]) => [r2(x), r2(y), Math.round((p ?? 0.5) * 1000) / 1000]) };
    if (it.hl) s.hl = true;
    if (it.mono) s.mono = true;
    if (it.op != null) s.op = it.op;
    return withMeta(s, it);
  }
  if (it.type === 'image') { const s = { type: 'image', blob: it.blob, x: r2(it.x), y: r2(it.y), w: r2(it.w), h: r2(it.h) }; if (it.bg) s.bg = true; return s; }
  if (it.type === 'ink') {
    const segs = it.segs.map(sg => ({ p: sg.p.map(([x, y]) => [r2(x), r2(y)]), w: sg.w, ...(sg.o != null ? { o: sg.o } : {}) }));
    return withMeta({ type: 'ink', color: it.color, segs }, it);
  }
  if (it.type === 'mark') {
    const s = { type: 'mark', html: it.html, box0: it.box0, x: r2(it.x || 0), y: r2(it.y || 0), color: it.color };
    if (it.polys?.length) s.polys = it.polys;
    return withMeta(s, it);
  }
  return withMeta({ type: 'textbox', x: r2(it.x), y: r2(it.y), w: r2(it.w), text: it.text, color: it.color }, it);
}
const cloneItem = it => ({ ...it, editing: false });

export class Notebook {
  // paper: optional canvas under the ink, painted by paintPaper(ctx, style) at the same resolution.
  // onSelect(box|null): the lasso selection changed (box in page coords).
  // onLongPress(item): the pen or a finger rested ~0.5 s on one of Meta's strokes (per-mark menu).
  // beforeStroke(stroke, { tap }) -> true: the pen stroke was used up (a tap on Meta's preview, skip…).
  // afterStroke(stroke): it was added (Meta's preview under it is dismissed, old hints fade…).
  constructor({ page, ink, live, paper, paintPaper, scroller, onErase, onChange, onSelect, onLongPress, beforeStroke, afterStroke }) {
    Object.assign(this, { page, ink, live, paper, paintPaper, scroller, onErase, onChange, onSelect, onLongPress, beforeStroke, afterStroke });
    this.paperStyle = 'ruled';
    this.items = []; this.history = []; this.future = [];
    this.ext = null;                                  // { save, load }: extra state (Meta's ink) kept in undo snapshots
    this.tool = 'pen'; this.color = '#1f3a7a'; this.size = PEN_SIZES.medium; this.hlColor = HL_COLORS[0]; this.scale = 1;
    this.lastPoint = { x: 320, y: 220 }; this.penSeen = false; this.active = null; this.cursor = null;
    this.sel = []; this.lasso = null; this.drag = null;
    this.metaHidden = false;                          // "Hide Meta's ink": by:'meta' items are not drawn or touched
    this.tidy = { smooth: true, shapes: true };       // smooth finished strokes; hold still to snap a shape
    this.ctx = ink.getContext('2d'); this.lctx = live.getContext('2d');
    for (const t of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel']) page.addEventListener(t, e => this[t](e));
    page.addEventListener('pointerleave', () => { if (!this.active) { this.cursor = null; this.renderLive(); } });
    document.fonts?.load(`${TEXT_PX}px "Patrick Hand"`).then(() => this.rebuild(), () => {});
  }

  // Scale of the page on screen (used for hit-testing). The canvas backing store follows
  // with setResolution, which zoom calls once a pinch settles.
  setScale(s, { resolution = true } = {}) {
    this.scale = s;
    if (resolution) this.setResolution(s);
    this.notifySelect();
  }
  setResolution(s) {
    const r = Math.min(3, (window.devicePixelRatio || 1) * s);
    if (r === this.res) return;
    this.res = r;
    for (const c of [this.ink, this.live, this.paper].filter(Boolean)) {
      c.width = Math.round(PAGE_W * r); c.height = Math.round(PAGE_H * r);
      c.getContext('2d').setTransform(r, 0, 0, r, 0, 0);
    }
    this.redraw(); this.renderLive(); this.renderPaper();
  }
  setPaper(style) { this.paperStyle = style; this.renderPaper(); }
  renderPaper() { if (this.paper && this.paintPaper) this.paintPaper(this.paper.getContext('2d'), this.paperStyle); }

  setTool(t) {
    if (t !== 'type') this.commitInput();
    if (t !== 'lasso') this.clearSelection();
    this.tool = t; this.cursor = null; this.renderLive();
  }
  setColor(c) { this.color = c; if (this.input) this.input.el.style.color = c; }

  pos(e) {
    const b = this.page.getBoundingClientRect();
    return { x: (e.clientX - b.left) / this.scale, y: (e.clientY - b.top) / this.scale, p: e.pointerType === 'pen' ? Math.max(0.05, e.pressure) : 0.5 };
  }

  // ---- pointer input -------------------------------------------------------
  pointerdown(e) {
    if (e.target.closest('.tbox-ui, .sel-bar') || (e.pointerType !== 'pen' && e.target.closest('.tap-target'))) return;   // the Pencil writes through Meta's previews
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (e.pointerType === 'pen') this.penSeen = true;
    e.preventDefault();
    if (this.active) return;                          // one drawing pointer at a time (fingers pan once a stylus is used, see zoom.js)
    const pt = this.pos(e);
    if (this.tool === 'type') { this.openInput(pt); return; }
    this.commitInput();
    this.page.setPointerCapture?.(e.pointerId);
    this.active = { id: e.pointerId };
    if (this.tool === 'pen' || this.tool === 'highlighter') {
      const hl = this.tool === 'highlighter';
      this.cur = { type: 'stroke', color: hl ? this.hlColor : this.color, size: hl ? HL_SIZE : this.size, sim: e.pointerType !== 'pen', pts: [[pt.x, pt.y, pt.p]] };
      if (hl) this.cur.hl = true;
      this.cur.t0 = performance.now(); this.cur.ptype = e.pointerType;
      if (!hl) this.holdAt(pt);
      // Long-press menu on Meta's ink: fingers and mouse only. A resting Pencil is about to write.
      const hit = !hl && e.pointerType !== 'pen' && this.onLongPress && this.metaAt(pt);
      if (hit) this.press = { x: pt.x, y: pt.y, t: setTimeout(() => this.longPress(hit), 520) };
    } else if (this.tool === 'lasso') this.lassoDown(pt);
    else { this.pending = this.snap(); this.erasedAny = false; this.eraseAt(pt); }
    this.cursor = pt; this.renderLive();
  }

  pointermove(e) {
    const a = this.active;
    if (!a || e.pointerId !== a.id) {
      if (e.pointerType !== 'touch' && this.tool === 'eraser') { this.cursor = this.pos(e); this.renderLive(); }
      return;
    }
    if (this.drag) { this.dragTo(this.pos(e)); return; }
    if (this.press) { const q = this.pos(e); if (Math.hypot(q.x - this.press.x, q.y - this.press.y) > 6 / this.scale) this.cancelPress(); }
    const evs = e.getCoalescedEvents?.() ?? [];
    for (const ev of evs.length ? evs : [e]) {
      const q = this.pos(ev);
      if (this.cur) { if (!this.cur.shaped) { this.cur.pts.push([q.x, q.y, q.p]); if (!this.cur.hl) this.holdAt(q); } }
      else if (this.lasso) this.lasso.push([q.x, q.y]);
      else this.eraseAt(q);
    }
    this.cursor = this.pos(e); this.renderLive();
  }

  pointerup(e) {
    const a = this.active;
    if (!a || e.pointerId !== a.id) return;
    this.active = null;
    this.cancelPress();
    clearTimeout(this.hold?.t); this.hold = null;
    if (this.cur) {
      const s = this.cur; this.cur = null;
      const p0 = s.pts[0], tap = performance.now() - s.t0 < 300 && s.pts.every(([x, y]) => Math.hypot(x - p0[0], y - p0[1]) < 6 / Math.max(0.3, this.scale));
      const ptype = s.ptype; delete s.t0; delete s.ptype;
      if (this.beforeStroke?.(s, { tap, ptype })) { this.renderLive(); return; }
      if (!s.hl && this.scratchOut(s.pts)) { this.renderLive(); return; }
      if (!s.hl && !s.shaped && this.tidy.smooth) s.pts = smoothStroke(s.pts);
      delete s.shaped;
      if (s.pts.length === 1) s.pts.push([s.pts[0][0] + 0.4, s.pts[0][1] + 0.4, s.pts[0][2]]);
      this.prepare(s);
      this.checkpoint(); this.items.push(s);
      if (!s.hl) { const [x, y] = s.pts.at(-1); this.lastPoint = { x, y }; }
      this.afterStroke?.(s);
      this.redraw(); this.onChange?.('stroke');
    } else if (this.drag) this.dragEnd();
    else if (this.lasso) this.lassoEnd();
    else if (this.erasedAny) this.onChange?.('erase');
    this.pending = null; this.erasedAny = false;
    if (e.pointerType !== 'mouse') this.cursor = null;
    this.renderLive();
  }
  pointercancel(e) { this.pointerup(e); }

  // Hold the pen still for a moment at the end of a stroke: a rough shape snaps to a clean one.
  holdAt(q) {
    if (!this.tidy.shapes) return;
    const h = this.hold;
    if (h && Math.hypot(q.x - h.x, q.y - h.y) < 3 / Math.max(0.3, this.scale)) return;   // still holding
    clearTimeout(h?.t);
    this.hold = { x: q.x, y: q.y, t: setTimeout(() => this.snapShape(), HOLD_MS) };
  }
  snapShape() {
    const s = this.cur;
    if (!s || s.hl || s.shaped) return;
    const shape = recognizeShape(s.pts);
    if (!shape) return;
    s.pts = shape.pts; s.shaped = shape.kind;
    this.renderLive();
    this.onShape?.(shape.kind);
  }

  // A second finger arrived: drop the half-drawn stroke, the gesture is a pinch.
  cancelActive() {
    if (!this.active) return;
    this.cancelPress();
    this.active = null; this.cur = null; this.cursor = null; this.lasso = null;
    if (this.drag) { this.dragEnd(); }
    if (this.erasedAny) this.onChange?.('erase');
    this.pending = null; this.erasedAny = false;
    this.renderLive();
  }

  // Removes what `hitItem` hits; ink items lose only the segs `hitSeg` hits. Returns whether anything went.
  cut(hitItem, hitSeg) {
    let dirty = false;
    const out = [];
    for (const it of this.items) {
      if (this.metaHidden && isMeta(it)) { out.push(it); continue; }
      if (it.type === 'ink') {
        const keep = it.segs.filter(sg => !hitSeg(sg));
        if (keep.length === it.segs.length) { out.push(it); continue; }
        dirty = true;
        if (keep.length) { it.segs = keep; prepareItem(it); out.push(it); }
        continue;
      }
      if (hitItem(it)) { dirty = true; continue; }
      out.push(it);
    }
    if (dirty) { this.items = out; this.sel = this.sel.filter(it => out.includes(it)); }
    return dirty;
  }
  // Scratch-out: a zigzag over ink erases the ink it covers (student's or Meta's), in one undo step.
  // A zigzag over empty paper is ordinary writing.
  scratchOut(pts) {
    if (!isScratch(pts)) return false;
    const near = (x, y) => { for (let i = 0; i < pts.length - 1; i++) if (distSeg(x, y, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) < 9) return true; return false; };
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    const gb = [Math.min(...xs) - 6, Math.min(...ys) - 6, Math.max(...xs) - Math.min(...xs) + 12, Math.max(...ys) - Math.min(...ys) + 12];
    const covered = ps => ps.some(([x, y]) => inBox(gb, x, y) && near(x, y));
    const before = this.snap();
    const typed = this.scratchText(gb);
    const dirty = typed | this.cut(it => (it.type === 'stroke' || it.type === 'mark') && overlap(it.bbox, gb) && covered(it.type === 'mark' ? (it.polys || []).flat().map(([x, y]) => [x + it.x, y + it.y]) : it.pts),
      sg => overlap(segGeometry(sg)._box, gb) && covered(sg.p));
    if (!dirty) {                     // a zigzag over typed text that missed every word: drop it, no smudge
      return this.items.some(it => it.type === 'textbox' && !it.editing && (it.rowBoxes || []).some(b => overlap(b, gb)));
    }
    this.history.push(before); this.future = [];
    this.redraw(); this.notifySelect(); this.onChange?.('erase');
    return true;
  }
  // Scratch-out over typed text: the words whose middle the zigzag covers go (a box left empty goes too).
  scratchText(gb) {
    const cy = gb[1] + gb[3] / 2, x0 = gb[0] + 6, x1 = gb[0] + gb[2] - 6;
    let dirty = false;
    textFont();
    for (const it of [...this.items]) {
      if (it.type !== 'textbox' || it.editing || (this.metaHidden && isMeta(it)) || !overlap(it.bbox, gb)) continue;
      const paras = String(it.text).split('\n').map(p => wrapRows(p, it.w));
      let i = 0, hit = false;
      for (const rows of paras) rows.forEach((row, j) => {
        const base = rowBaseline(it, i++);
        if (cy < base - 26 || cy > base + 10) return;
        let x = it.x;
        const kept = row.split(/(\s+)/).filter(w => {
          const wx = x, ww = mctx.measureText(w).width; x += ww;
          const gone = w.trim() && wx + ww / 2 >= x0 && wx + ww / 2 <= x1;
          if (gone) hit = true;
          return !gone;
        });
        rows[j] = kept.join('').replace(/\s+/g, ' ').trim();
      });
      if (!hit) continue;
      dirty = true;
      const text = paras.map(rows => rows.filter(Boolean).join(' ')).join('\n').replace(/^\n+|\n+$/g, '');
      if (text.trim()) { it.text = text; prepareItem(it); }
      else { this.items = this.items.filter(x => x !== it); this.sel = this.sel.filter(x => x !== it); }
    }
    return dirty;
  }
  cancelPress() { if (this.press) { clearTimeout(this.press.t); this.press = null; } }
  longPress(it) {
    this.press = null;
    if (!this.active || !this.cur) return;
    this.active = null; this.cur = null; this.cursor = null;      // the resting pen drew nothing
    this.renderLive();
    this.onLongPress?.(it);
  }
  // Meta's topmost item under a point (ignored while Meta's ink is hidden).
  metaAt(pt) {
    if (this.metaHidden) return null;
    for (let i = this.items.length - 1; i >= 0; i--) { const it = this.items[i]; if (isMeta(it) && hits(it, pt, 6)) return it; }
    return null;
  }

  eraseAt(pt) {
    const R = 10;
    const before = this.items.length;
    let dirty = this.cut(it => hits(it, pt, R), sg => segHit(sg, pt.x, pt.y, R));
    dirty ||= this.items.length !== before;
    const metaHit = this.onErase?.(pt, R);
    if ((dirty || metaHit) && !this.erasedAny && this.pending) { this.history.push(this.pending); this.future = []; this.erasedAny = true; }
    if (dirty) this.redraw();
  }

  // ---- lasso: loop to select, drag to move, handle to resize -------------------------
  selBox() { const b = union(this.sel.map(it => it.bbox)); return b && [b[0] - 6, b[1] - 6, b[2] + 12, b[3] + 12]; }
  resizable() { return this.sel.length === 1 && (this.sel[0].type === 'image' || this.sel[0].type === 'textbox'); }
  handlePos() { const b = this.selBox(); return b && [b[0] + b[2], b[1] + b[3]]; }
  notifySelect() { this.onSelect?.(this.sel.length ? this.selBox() : null); }
  setSelection(list) { this.sel = list.filter(it => this.items.includes(it)); this.renderLive(); this.notifySelect(); }
  clearSelection() { if (!this.sel.length && !this.lasso) return; this.sel = []; this.lasso = null; this.renderLive(); this.notifySelect(); }

  lassoDown(pt) {
    const hp = this.resizable() && this.handlePos();
    const near = hp && Math.hypot(pt.x - hp[0], pt.y - hp[1]) < 22 / Math.min(1, this.scale);
    if (near || (this.sel.length && inBox(this.selBox(), pt.x, pt.y, 4))) {
      this.drag = { kind: near ? 'resize' : 'move', x: pt.x, y: pt.y, moved: false, before: this.snap(),
        orig: this.sel.map(it => ({ it, x: it.x, y: it.y, w: it.w, h: it.h, bw: it.bbox[2], pts: it.pts, segs: it.segs })) };
      this.onSelect?.(null);                            // hide the action bar while dragging
      return;
    }
    this.sel = []; this.notifySelect();
    this.lasso = [[pt.x, pt.y]];
  }
  dragTo(pt) {
    const d = this.drag, dx = pt.x - d.x, dy = pt.y - d.y;
    if (!d.moved && Math.hypot(dx, dy) < 2) return;
    d.moved = true;
    for (const o of d.orig) {
      const it = o.it;
      if (d.kind === 'move') {
        if (it.type === 'stroke') it.pts = o.pts.map(([x, y, p]) => [x + dx, y + dy, p]);
        else if (it.type === 'mark') { it.x = (o.x || 0) + dx; it.y = (o.y || 0) + dy; }
        else if (it.type === 'ink') it.segs = o.segs.map(sg => ({ ...sg, _path: null, p: sg.p.map(([x, y]) => [x + dx, y + dy]) }));
        else { it.x = o.x + dx; it.y = o.y + dy; }
      } else if (it.type === 'image') {
        it.w = clamp(o.w + dx, 40, PAGE_W * 1.5); it.h = it.w * o.h / o.w;
      } else if (it.type === 'textbox') {
        it.w = clamp(o.bw + dx, 60, PAGE_W);
      }
      this.prepare(it);
    }
    this.redraw(); this.renderLive();
  }
  dragEnd() {
    const d = this.drag; this.drag = null;
    if (d.moved) {
      this.history.push(d.before); this.future = [];
      for (const it of this.sel) if (isMeta(it)) { it.kept = true; delete it.dim; }   // moved on purpose: Meta's next reply leaves it
      this.onChange?.(d.kind);
    }
    this.notifySelect();
  }
  lassoEnd() {
    const poly = this.lasso; this.lasso = null;
    let len = 0;
    for (let i = 1; i < poly.length; i++) len += Math.hypot(poly[i][0] - poly[i - 1][0], poly[i][1] - poly[i - 1][1]);
    const pick = this.items.filter(it => !it.bg && !(this.metaHidden && isMeta(it)));
    let sel;
    if (len < 16) {                                    // a tap: the topmost thing under it
      const [x, y] = poly[0];
      const hit = [...pick].reverse()                  // latest first, then the top layer wins
        .filter(it => it.type === 'stroke' || it.type === 'ink' ? hits(it, { x, y }, 8) : inBox(it.bbox, x, y, 4))
        .sort((a, b) => layer(b) - layer(a));
      sel = hit.slice(0, 1);
      // Tapping one of Meta's strokes picks up the whole mark (a hint, an answer line), ready to move.
      if (isMeta(sel[0]) && sel[0].grp) sel = pick.filter(it => isMeta(it) && it.grp === sel[0].grp);
    } else {
      sel = pick.filter(it => {
        if (it.type === 'stroke' || it.type === 'ink') { const ps = pointsOf(it), n = ps.filter(([x, y]) => inPoly(poly, x, y)).length; return n >= ps.length * 0.6; }
        const [x, y, w, h] = it.bbox;
        return inPoly(poly, x + w / 2, y + h / 2);
      });
    }
    this.setSelection(sel);
  }
  deleteSelection() {
    if (!this.sel.length) return false;
    this.checkpoint();
    this.items = this.items.filter(it => !this.sel.includes(it));
    this.sel = []; this.redraw(); this.renderLive(); this.notifySelect();
    this.onChange?.('delete');
    return true;
  }
  // "Keep as mine": Meta's ink in the selection becomes the student's (their pen colour, read by Meta).
  adoptSelection(color = this.color) {
    const targets = this.sel.filter(isMeta);
    if (!targets.length) return false;
    this.checkpoint();
    for (const it of targets) { it.by = 'student-adopted'; it.color = color; delete it.kept; delete it.dim; }
    this.redraw(); this.renderLive(); this.notifySelect(); this.onChange?.('adopt');
    return true;
  }
  metaItems() { return this.items.filter(isMeta); }
  // Drops Meta's items matching `pred` (no checkpoint: callers decide). Returns how many went.
  removeMeta(pred = () => true) {
    const n = this.items.length;
    this.items = this.items.filter(it => !(isMeta(it) && pred(it)));
    if (this.items.length === n) return 0;
    this.sel = this.sel.filter(it => this.items.includes(it));
    this.redraw(); this.renderLive(); this.notifySelect();
    return n - this.items.length;
  }
  setMetaHidden(on) {
    this.metaHidden = !!on;
    if (on) this.sel = this.sel.filter(mine);
    this.redraw(); this.renderLive(); this.notifySelect();
  }
  // Pen colours recolour pen ink and text; highlighter colours recolour highlighter strokes.
  recolorSelection(color, hl = HL_COLORS.includes(color)) {
    let targets = this.sel.filter(it => it.type !== 'image' && !!it.hl === hl);
    if (!targets.length) targets = this.sel.filter(it => it.type !== 'image');
    if (!targets.length) return false;
    this.checkpoint();
    for (const it of targets) { it.color = color; if (isMeta(it)) it.kept = true; }    // recoloured on purpose: keep it
    this.redraw(); this.onChange?.('recolor');
    return true;
  }

  // ---- model ---------------------------------------------------------------
  prepare(it) { return prepareItem(it); }
  rebuild() { this.items.forEach(it => this.prepare(it)); this.redraw(); this.notifySelect(); }
  redraw() {
    this.ctx.clearRect(0, 0, PAGE_W, PAGE_H);
    paintItems(this.ctx, this.metaHidden ? this.items.filter(mine) : this.items);
  }
  renderLive() {
    const c = this.lctx;
    c.clearRect(0, 0, PAGE_W, PAGE_H);
    if (this.cur) { Object.assign(this.cur, strokeGeometry(this.cur, false)); paint(c, this.cur); }
    const k = 1 / Math.max(0.3, this.scale);           // keep selection chrome the same size on screen
    if (this.lasso?.length > 1) {
      c.save(); c.setLineDash([5 * k, 5 * k]); c.lineWidth = 1.4 * k; c.strokeStyle = '#2f6fe0';
      c.beginPath(); this.lasso.forEach(([x, y], i) => i ? c.lineTo(x, y) : c.moveTo(x, y)); c.stroke(); c.restore();
    }
    if (this.sel.length) {
      const b = this.selBox();
      c.save();
      c.fillStyle = 'rgba(47,111,224,.05)'; c.fillRect(...b);
      c.setLineDash([6 * k, 4 * k]); c.lineWidth = 1.3 * k; c.strokeStyle = '#2f6fe0'; c.strokeRect(...b);
      if (this.resizable()) {
        const [hx, hy] = this.handlePos();
        c.setLineDash([]); c.beginPath(); c.arc(hx, hy, 7 * k, 0, Math.PI * 2);
        c.fillStyle = '#fff'; c.fill(); c.lineWidth = 2 * k; c.stroke();
      }
      c.restore();
    }
    if (this.tool === 'eraser' && this.cursor) {
      c.beginPath(); c.arc(this.cursor.x, this.cursor.y, 10, 0, Math.PI * 2);
      c.fillStyle = 'rgba(255,255,255,.55)'; c.fill();
      c.strokeStyle = 'rgba(0,0,0,.35)'; c.lineWidth = 1; c.stroke();
    }
  }

  // ---- undo / redo: whole-page snapshots (student ink + Meta's ink) ----------
  snap() { return { items: this.items.map(cloneItem), ext: this.ext?.save() }; }
  restore(s) { this.items = s.items.map(cloneItem); this.sel = []; this.ext?.load(s.ext); this.redraw(); this.renderLive(); this.notifySelect(); }
  checkpoint() {
    this.history.push(this.snap());
    if (this.history.length > 200) this.history.shift();
    this.future = [];
  }
  undo() {
    this.commitInput();
    const s = this.history.pop();
    if (!s) return false;
    this.future.push(this.snap()); this.restore(s); this.onChange?.('undo');
    return true;
  }
  redo() {
    this.commitInput();
    const s = this.future.pop();
    if (!s) return false;
    this.history.push(this.snap()); this.restore(s); this.onChange?.('redo');
    return true;
  }
  remove(it) { const i = this.items.indexOf(it); if (i >= 0) this.items.splice(i, 1); }
  clear() {
    this.commitInput();
    if (!this.items.length) return false;
    this.items = []; this.sel = []; this.notifySelect();
    this.redraw(); this.renderLive(); return true;
  }
  add(it) { this.prepare(it); this.items.push(it); this.redraw(); return it; }

  // ---- pages ------------------------------------------------------------------
  // Swap in another page's items and its undo history ({ history, future } arrays, kept by the caller).
  load(items, hist) {
    this.cancelActive();
    if (this.input) { this.input.ui.remove(); this.input = null; }
    this.sel = []; this.lasso = null; this.drag = null;
    this.items = mergeMetaStrokes(items.map(it => ({ ...normalizeItem(it) }))).map(prepareItem);
    this.history = hist.history; this.future = hist.future;
    this.lastPoint = { x: 320, y: 220 };
    this.redraw(); this.renderLive(); this.notifySelect();
    this.loadImages();
  }
  loadImages() { const items = this.items; loadItemImages(items).then(n => { if (n && this.items === items) this.redraw(); }); }
  serialize() { return this.items.map(serializeItem); }

  // ---- text boxes -----------------------------------------------------------
  addText(text, x, row, color = this.color) {
    return this.add({ type: 'textbox', x, y: rowY(row) - RULE, w: Math.max(120, PAGE_W - x - 24), text, color });
  }
  textAt(pt) {
    for (let i = this.items.length - 1; i >= 0; i--) {
      const it = this.items[i];
      if (it.type === 'textbox' && inBox(it.bbox, pt.x, pt.y, 6)) return it;
    }
  }

  // An editable box over the page: a textarea with a drag grip and a width handle.
  openInput(pt) {
    this.commitInput();
    const orig = this.textAt(pt);
    let x, y, w;
    if (orig) ({ x, y, w } = orig);
    else {
      const row = Math.max(1, rowAt(pt.y));
      x = clamp(pt.x, MARGIN_X + 14, PAGE_W - 160); y = rowY(row) - RULE; w = PAGE_W - x - 24;
    }
    const ui = document.createElement('div');
    ui.className = 'tbox-ui';
    const el = document.createElement('textarea');
    Object.assign(el, { className: 'type-input', value: orig?.text ?? '', spellcheck: false, autocomplete: 'off', rows: 1, placeholder: orig ? '' : 'type math…  (int → ∫)' });
    el.setAttribute('autocapitalize', 'off'); el.setAttribute('autocorrect', 'off'); el.setAttribute('aria-label', 'Text box');
    el.style.color = orig?.color ?? this.color;
    const grip = document.createElement('span'); grip.className = 'tbox-grip'; grip.title = 'Drag to move';
    const size = document.createElement('span'); size.className = 'tbox-size'; size.title = 'Drag to resize';
    const mathBar = document.createElement('div');
    mathBar.className = 'tbox-math';
    mathBar.setAttribute('role', 'toolbar');
    mathBar.setAttribute('aria-label', 'Quick math symbols');
    for (const [ins, lbl, tip] of [
      ['∫ ', '∫', 'Integral (int)'], ['dx', 'dx', 'dx'], ['d/dx ', 'd/dx', 'Derivative'],
      ['²', 'x²', 'Squared (^2)'], ['³', 'x³', 'Cubed (^3)'], ['√(', '√', 'Square root'],
      ['π', 'π', 'Pi'], ['sin(', 'sin', 'Sine'], ['cos(', 'cos', 'Cosine'],
      ['ln(', 'ln', 'Natural log'], ['e^(', 'eˣ', 'Exponential'], ['= ', '=', 'Equals'], [' + C', '+ C', 'Constant C'],
    ]) {
      const b = document.createElement('button');
      b.type = 'button'; b.textContent = lbl; b.title = tip;
      b.addEventListener('pointerdown', e => {
        e.preventDefault(); e.stopPropagation();
        const st = el.selectionStart ?? el.value.length, en = el.selectionEnd ?? st;
        el.value = el.value.slice(0, st) + ins + el.value.slice(en);
        el.focus();
        el.setSelectionRange(st + ins.length, st + ins.length);
        place();
      });
      mathBar.append(b);
    }
    ui.append(mathBar, el, grip, size);
    if (orig) { orig.editing = true; this.redraw(); }
    const s = this.input = { ui, el, orig, x, y, w };
    const off = 36 - editorBaseline(RULE);
    const place = () => {
      Object.assign(ui.style, { left: s.x + 'px', top: s.y + off + 'px', width: s.w + 'px' });
      el.style.height = '0px';
      el.style.height = Math.max(RULE, Math.ceil(el.scrollHeight / RULE) * RULE) + 'px';
    };
    el.addEventListener('input', () => {
      const v = el.value
        .replace(/\\int/g, '∫')
        .replace(/(^|[\s=+\-−(])int\s/g, '$1∫ ')
        .replace(/\\pi\b/g, 'π')
        .replace(/\\sqrt\b/g, '√')
        .replace(/->/g, '→');
      if (v !== el.value) { const c = el.selectionStart + v.length - el.value.length; el.value = v; el.setSelectionRange(c, c); }
      place();
    });
    el.addEventListener('keydown', e => {
      e.stopPropagation();
      if (e.key === 'Escape' || (e.key === 'Enter' && (e.metaKey || e.ctrlKey))) { e.preventDefault(); this.commitInput(); }
    });
    el.addEventListener('blur', () => setTimeout(() => { if (this.input === s && document.activeElement !== el && !s.dragging) this.commitInput(); }, 0));
    for (const [h, kind] of [[grip, 'move'], [size, 'size']]) {
      h.addEventListener('pointerdown', e => {
        e.preventDefault(); e.stopPropagation();
        h.setPointerCapture?.(e.pointerId);
        const p0 = this.pos(e), o = { x: s.x, y: s.y, w: s.w };
        s.dragging = true;
        const mv = ev => {
          const p = this.pos(ev);
          if (kind === 'move') { s.x = clamp(o.x + p.x - p0.x, 0, PAGE_W - 60); s.y = clamp(o.y + p.y - p0.y, 0, PAGE_H - RULE); }
          else s.w = clamp(o.w + p.x - p0.x, 60, PAGE_W - s.x);
          place();
        };
        const up = () => { h.removeEventListener('pointermove', mv); h.removeEventListener('pointerup', up); h.removeEventListener('pointercancel', up); s.dragging = false; el.focus(); };
        h.addEventListener('pointermove', mv); h.addEventListener('pointerup', up); h.addEventListener('pointercancel', up);
      });
    }
    this.page.append(ui);
    place();
    el.focus();
    if (orig) el.setSelectionRange(el.value.length, el.value.length);
  }

  commitInput() {
    const s = this.input;
    if (!s) return;
    this.input = null;
    const v = s.el.value.split('\n').map(l => l.trimEnd()).join('\n').replace(/^\n+|\s+$/g, ''), { orig } = s;
    s.ui.remove();
    let changed = false;
    if (orig) {
      orig.editing = false;
      if (!v) { this.checkpoint(); this.remove(orig); changed = true; }
      else if (v !== orig.text || s.x !== orig.x || s.y !== orig.y || s.w !== orig.w) {
        this.checkpoint(); Object.assign(orig, { text: v, x: s.x, y: s.y, w: s.w }); this.prepare(orig); changed = true;
      }
      if (v) { const b = orig.bbox; this.lastPoint = { x: b[0] + b[2], y: b[1] + b[3] - 32 }; }
    } else if (v) {
      this.checkpoint();
      const it = this.add({ type: 'textbox', x: s.x, y: s.y, w: s.w, text: v, color: s.el.style.color || this.color });
      this.lastPoint = { x: it.bbox[0] + it.bbox[2], y: it.bbox[1] + it.bbox[3] - 32 };
      changed = true;
    }
    this.redraw();
    if (changed) this.onChange?.('text');
  }

  // ---- what the assistant sees ------------------------------------------------
  // One line per text row (rows of different boxes on the same ruled line are joined, left to right).
  lines() {
    const rows = new Map();
    textFont();
    for (const it of this.items) if (it.type === 'textbox' && !it.editing && mine(it)) {
      it.rows.forEach((text, i) => {
        if (!text.trim()) return;
        const base = rowBaseline(it, i), key = Math.round((base + 8 - TOP) / RULE);
        if (!rows.has(key)) rows.set(key, []);
        rows.get(key).push({ text, bbox: [it.x, base - 24, Math.max(8, mctx.measureText(text).width), 32] });
      });
    }
    return [...rows.keys()].sort((a, b) => a - b).map((k, i) => {
      const rs = rows.get(k).sort((a, b) => a.bbox[0] - b.bbox[0]);
      return { id: `L${i + 1}`, text: rs.map(r => r.text).join(' '), bbox: union(rs.map(r => r.bbox)).map(Math.round) };
    });
  }
  hasStrokes() { return this.items.some(it => (it.type === 'stroke' || it.type === 'ink') && mine(it)); }
  hasImages() { return this.items.some(it => it.type === 'image'); }
  hasBackground() { return this.items.some(it => it.bg); }
  isEmpty() { return !this.items.length; }
  signature() { return this.items.filter(mine).map(it => it.type === 'textbox' ? `${Math.round(it.y)}:${it.text}` : it.type === 'image' ? `i${Math.round(it.x)},${Math.round(it.y)},${Math.round(it.w)}` : it.type === 'ink' ? `k${it.segs.length}` : it.pts?.length ?? `m${Math.round(it.x)},${Math.round(it.y)}`).join('|'); }
  // Text boxes count row by row, so Meta can still write to the right of a short line in a wide box.
  occupied(box) { return this.items.some(it => !it.bg && (it.rowBoxes ? it.rowBoxes.some(b => overlap(b, box)) : overlap(it.bbox, box))); }

  // Boxes of handwritten rows, top to bottom: pen strokes grouped where they overlap vertically
  // (half the shorter stroke's height), so an integral sign and its small terms land in one row.
  inkRows() {
    const boxes = this.items.filter(it => (it.type === 'stroke' || it.type === 'ink') && !it.hl && it.bbox && mine(it)).map(it => [...it.bbox])
      .sort((a, b) => (a[1] + a[3] / 2) - (b[1] + b[3] / 2));
    const rows = [];
    for (const b of boxes) {
      const r = rows.find(r => {
        const ov = Math.min(r[1] + r[3], b[1] + b[3]) - Math.max(r[1], b[1]);
        return ov > Math.min(r[3], b[3]) * 0.5;
      });
      if (!r) { rows.push(b); continue; }
      const x2 = Math.max(r[0] + r[2], b[0] + b[2]), y2 = Math.max(r[1] + r[3], b[1] + b[3]);
      r[0] = Math.min(r[0], b[0]); r[1] = Math.min(r[1], b[1]); r[2] = x2 - r[0]; r[3] = y2 - r[1];
    }
    return rows.sort((a, b) => a[1] - b[1]).map(r => r.map(v => Math.round(v)));
  }

  // White page with the student's ink, typed text and pictures, cropped below the content.
  // Meta's own ink (by: 'meta') is left out. JPEG when there is a photo on it (much smaller), PNG otherwise.
  // Just these items, cropped to them, on white (for Meta to read a lasso selection).
  snapshotOf(items) {
    const b = union(items.map(it => it.bbox)), pad = 16;
    const c = document.createElement('canvas');
    c.width = Math.ceil(b[2] + 2 * pad); c.height = Math.ceil(b[3] + 2 * pad);
    const g = c.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height);
    g.translate(pad - b[0], pad - b[1]);
    paintItems(g, items);
    return c.toDataURL('image/png');
  }

  snapshot() {
    const items = this.items.filter(mine);
    const bottom = Math.min(PAGE_H, Math.max(TOP + RULE, ...items.map(it => it.bbox[1] + it.bbox[3])) + 40);
    const c = document.createElement('canvas');
    c.width = PAGE_W; c.height = Math.ceil(bottom);
    const g = c.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height);
    paintItems(g, items);
    return this.hasImages() ? c.toDataURL('image/jpeg', 0.85) : c.toDataURL('image/png');
  }
}

export { overlap, inBox };
