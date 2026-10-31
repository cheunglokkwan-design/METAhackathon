// Student ink: pen strokes, typed lines, eraser, undo and the page snapshot.
// Everything is kept in page coordinates (CSS px of the unscaled 1000 x 1400 page).

export const PAGE_W = 1000, PAGE_H = 1400, RULE = 44, TOP = 132, MARGIN_X = 96;
export const STUDENT_FONT = '"Patrick Hand", "Bradley Hand", "Segoe Print", cursive';
export const TEXT_PX = 28;
export const rowY = k => TOP + k * RULE;              // y of ruled line k
export const baselineOf = k => rowY(k) - 8;           // text sits just above the rule
export const rowAt = y => Math.max(0, Math.ceil((y - TOP) / RULE));

let getStroke = null;
const freehand = import('https://cdn.jsdelivr.net/npm/perfect-freehand@1.2.2/+esm')
  .then(m => { getStroke = m.getStroke || m.default?.getStroke || m.default; })
  .catch(() => console.info('[margin] perfect-freehand unavailable, using the built-in pen'));

const mctx = document.createElement('canvas').getContext('2d');
const inBox = ([x, y, w, h], px, py, pad = 0) => px >= x - pad && px <= x + w + pad && py >= y - pad && py <= y + h + pad;
const overlap = (a, b) => a[0] < b[0] + b[2] && b[0] < a[0] + a[2] && a[1] < b[1] + b[3] && b[1] < a[1] + a[3];

function distSeg(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, l = dx * dx + dy * dy;
  const t = l ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / l)) : 0;
  return Math.hypot(ax + t * dx - px, ay + t * dy - py);
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

function strokeGeometry(s, last = true) {
  if (getStroke) {
    const outline = getStroke(s.pts, { size: s.size * 2, thinning: 0.6, smoothing: 0.55, streamline: 0.4, simulatePressure: s.sim, last });
    return { path: outlineToPath(outline), fill: true };
  }
  const p = new Path2D(), pts = s.pts;               // fallback: smoothed polyline
  p.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length - 1; i++) p.quadraticCurveTo(pts[i][0], pts[i][1], (pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2);
  p.lineTo(pts.at(-1)[0], pts.at(-1)[1]);
  return { path: p, fill: false };
}

function paint(c, it) {
  c.fillStyle = c.strokeStyle = it.color;
  if (it.type === 'text') { c.font = `${TEXT_PX}px ${STUDENT_FONT}`; c.fillText(it.text, it.x, it.baseline); return; }
  if (it.fill) c.fill(it.path);
  else { c.lineWidth = it.size; c.lineCap = c.lineJoin = 'round'; c.stroke(it.path); }
}

function hits(it, { x, y }, R) {
  if (!inBox(it.bbox, x, y, R)) return false;
  if (it.type === 'text') return true;
  const p = it.pts, r = R + it.size / 2;
  for (let i = 0; i < p.length - 1; i++) if (distSeg(x, y, p[i][0], p[i][1], p[i + 1][0], p[i + 1][1]) < r) return true;
  return false;
}

// Offset from an input's top edge to its text baseline (line-height 36px).
function inputBaseline() {
  mctx.font = `${TEXT_PX}px ${STUDENT_FONT}`;
  const m = mctx.measureText('Hg');
  const asc = m.fontBoundingBoxAscent || TEXT_PX * 0.8, desc = m.fontBoundingBoxDescent || TEXT_PX * 0.25;
  return (36 - asc - desc) / 2 + asc;
}

export class Notebook {
  constructor({ page, ink, live, scroller, onErase, onChange }) {
    Object.assign(this, { page, ink, live, scroller, onErase, onChange });
    this.items = []; this.history = [];
    this.tool = 'pen'; this.color = '#1f3a7a'; this.size = 1.8; this.scale = 1;
    this.lastPoint = { x: 320, y: 220 }; this.penSeen = false; this.active = null; this.cursor = null;
    this.ctx = ink.getContext('2d'); this.lctx = live.getContext('2d');
    for (const t of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel']) page.addEventListener(t, e => this[t](e));
    page.addEventListener('pointerleave', () => { if (!this.active) { this.cursor = null; this.renderLive(); } });
    freehand.then(() => this.rebuild());
    document.fonts?.load(`${TEXT_PX}px "Patrick Hand"`).then(() => this.rebuild(), () => {});
  }

  setScale(s) {
    this.scale = s;
    const r = Math.min(3, (window.devicePixelRatio || 1) * s);
    for (const c of [this.ink, this.live]) {
      c.width = Math.round(PAGE_W * r); c.height = Math.round(PAGE_H * r);
      c.getContext('2d').setTransform(r, 0, 0, r, 0, 0);
    }
    this.redraw(); this.renderLive();
  }

  setTool(t) { if (t !== 'type') this.commitInput(); this.tool = t; this.cursor = null; this.renderLive(); }
  setColor(c) { this.color = c; if (this.input) this.input.el.style.color = c; }

  pos(e) {
    const b = this.page.getBoundingClientRect();
    return { x: (e.clientX - b.left) / this.scale, y: (e.clientY - b.top) / this.scale, p: e.pointerType === 'pen' ? Math.max(0.05, e.pressure) : 0.5 };
  }

  // ---- pointer input -------------------------------------------------------
  pointerdown(e) {
    if (e.target.closest('.type-input, .tap-target')) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (e.pointerType === 'pen') this.penSeen = true;
    e.preventDefault();
    if (e.pointerType === 'touch' && this.penSeen) { this.active = { id: e.pointerId, scrollY: e.clientY }; return; } // fingers scroll once a stylus is used
    const pt = this.pos(e);
    if (this.tool === 'type') { this.openInput(pt); return; }
    this.commitInput();
    this.page.setPointerCapture?.(e.pointerId);
    this.active = { id: e.pointerId };
    if (this.tool === 'pen') this.cur = { type: 'stroke', color: this.color, size: this.size, sim: e.pointerType !== 'pen', pts: [[pt.x, pt.y, pt.p]] };
    else { this.erased = []; this.eraseAt(pt); }
    this.cursor = pt; this.renderLive();
  }

  pointermove(e) {
    const a = this.active;
    if (a && e.pointerId === a.id && a.scrollY != null) { this.scroller.scrollTop += a.scrollY - e.clientY; a.scrollY = e.clientY; return; }
    if (!a || e.pointerId !== a.id) {
      if (e.pointerType !== 'touch' && this.tool === 'eraser') { this.cursor = this.pos(e); this.renderLive(); }
      return;
    }
    const evs = e.getCoalescedEvents?.() ?? [];
    for (const ev of evs.length ? evs : [e]) {
      const q = this.pos(ev);
      if (this.cur) this.cur.pts.push([q.x, q.y, q.p]); else this.eraseAt(q);
    }
    this.cursor = this.pos(e); this.renderLive();
  }

  pointerup(e) {
    const a = this.active;
    if (!a || e.pointerId !== a.id) return;
    this.active = null;
    if (this.cur) {
      const s = this.cur; this.cur = null;
      if (s.pts.length === 1) s.pts.push([s.pts[0][0] + 0.4, s.pts[0][1] + 0.4, s.pts[0][2]]);
      this.prepare(s); this.items.push(s);
      this.push(() => this.remove(s));
      const [x, y] = s.pts.at(-1); this.lastPoint = { x, y };
      this.redraw(); this.onChange?.('stroke');
    } else if (this.erased?.length) {
      const undo = this.erased.reverse(); this.push(() => undo.forEach(f => f())); this.onChange?.('erase');
    }
    this.erased = null;
    if (e.pointerType !== 'mouse') this.cursor = null;
    this.renderLive();
  }
  pointercancel(e) { this.pointerup(e); }

  eraseAt(pt) {
    const R = 10;
    let dirty = false;
    for (let i = this.items.length - 1; i >= 0; i--) {
      const it = this.items[i];
      if (!hits(it, pt, R)) continue;
      this.items.splice(i, 1); this.erased.push(() => this.items.splice(i, 0, it)); dirty = true;
    }
    const u = this.onErase?.(pt, R);
    if (u) this.erased.push(u);
    if (dirty) this.redraw();
  }

  // ---- model ---------------------------------------------------------------
  prepare(it) {
    if (it.type === 'stroke') {
      Object.assign(it, strokeGeometry(it));
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const [x, y] of it.pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
      const pad = it.size * 1.5;
      it.bbox = [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
    } else {
      mctx.font = `${TEXT_PX}px ${STUDENT_FONT}`;
      it.bbox = [it.x, it.baseline - 24, Math.max(8, mctx.measureText(it.text).width), 32];
    }
  }
  rebuild() { this.items.forEach(it => this.prepare(it)); this.redraw(); }
  redraw() {
    this.ctx.clearRect(0, 0, PAGE_W, PAGE_H);
    for (const it of this.items) if (!it.editing) paint(this.ctx, it);
  }
  renderLive() {
    const c = this.lctx;
    c.clearRect(0, 0, PAGE_W, PAGE_H);
    if (this.cur) { Object.assign(this.cur, strokeGeometry(this.cur, false)); paint(c, this.cur); }
    if (this.tool === 'eraser' && this.cursor) {
      c.beginPath(); c.arc(this.cursor.x, this.cursor.y, 10, 0, Math.PI * 2);
      c.fillStyle = 'rgba(255,255,255,.55)'; c.fill();
      c.strokeStyle = 'rgba(0,0,0,.35)'; c.lineWidth = 1; c.stroke();
    }
  }

  push(undo) { this.history.push(undo); if (this.history.length > 300) this.history.shift(); }
  mergeLast(n) { const fns = this.history.splice(-n, n); if (fns.length) this.push(() => fns.reverse().forEach(f => f())); }
  undo() {
    this.commitInput();
    const u = this.history.pop();
    if (u) { u(); this.redraw(); this.onChange?.('undo'); }
    return !!u;
  }
  remove(it) { const i = this.items.indexOf(it); if (i >= 0) this.items.splice(i, 1); }
  clear() {
    this.commitInput();
    if (!this.items.length) return false;
    const old = this.items; this.items = [];
    this.push(() => { this.items = old; });
    this.redraw(); return true;
  }

  // ---- typed lines -----------------------------------------------------------
  addText(text, x, row, color = this.color) {
    const it = { type: 'text', text, x, row, baseline: baselineOf(row), color };
    this.prepare(it); this.items.push(it); this.redraw();
    return it;
  }
  textAt(pt) {
    for (let i = this.items.length - 1; i >= 0; i--) {
      const it = this.items[i];
      if (it.type === 'text' && inBox(it.bbox, pt.x, pt.y, 4)) return it;
    }
  }

  openInput(pt) {
    this.commitInput();
    const orig = this.textAt(pt);
    const row = orig ? orig.row : rowAt(pt.y);
    const x = orig ? orig.x : Math.min(PAGE_W - 160, Math.max(MARGIN_X + 14, pt.x));
    const el = document.createElement('input');
    Object.assign(el, { className: 'type-input', value: orig?.text ?? '', spellcheck: false, autocomplete: 'off', placeholder: orig ? '' : 'type math…  (int → ∫)' });
    el.setAttribute('autocapitalize', 'off'); el.setAttribute('autocorrect', 'off'); el.setAttribute('enterkeyhint', 'next');
    Object.assign(el.style, { left: x + 'px', top: baselineOf(row) - inputBaseline() + 'px', width: PAGE_W - x - 24 + 'px', color: orig?.color ?? this.color });
    if (orig) { orig.editing = true; this.redraw(); }
    this.input = { el, orig, x, row };
    el.addEventListener('input', () => {
      const v = el.value.replace(/\\int/g, '∫').replace(/(^|[\s=+\-−(])int\s/g, '$1∫ ').replace(/->/g, '→');
      if (v !== el.value) { const c = el.selectionStart + v.length - el.value.length; el.value = v; el.setSelectionRange(c, c); }
    });
    el.addEventListener('keydown', e => {
      e.stopPropagation();
      if (e.key === 'Enter') {
        e.preventDefault();
        const more = el.value.trim();
        this.commitInput();
        if (more) this.openInput({ x, y: rowY(row + 1) - 10 });
      } else if (e.key === 'Escape') this.commitInput();
    });
    el.addEventListener('blur', () => this.commitInput());
    this.page.append(el);
    el.focus();
  }

  commitInput() {
    const s = this.input;
    if (!s) return;
    this.input = null;
    const v = s.el.value.trim(), { orig } = s;
    s.el.remove();
    if (orig) {
      orig.editing = false;
      if (!v) { const i = this.items.indexOf(orig); this.remove(orig); this.push(() => this.items.splice(i, 0, orig)); }
      else if (v !== orig.text) { const old = orig.text; orig.text = v; this.prepare(orig); this.push(() => { orig.text = old; this.prepare(orig); }); }
    } else if (v) {
      const it = this.addText(v, s.x, s.row);
      this.push(() => this.remove(it));
      this.lastPoint = { x: it.bbox[0] + it.bbox[2], y: it.bbox[1] };
    }
    this.redraw();
    if (v || orig) this.onChange?.('text');
  }

  // ---- what the assistant sees ------------------------------------------------
  lines() {
    const rows = new Map();
    for (const it of this.items) if (it.type === 'text' && !it.editing) {
      if (!rows.has(it.row)) rows.set(it.row, []);
      rows.get(it.row).push(it);
    }
    return [...rows.keys()].sort((a, b) => a - b).map((k, i) => {
      const its = rows.get(k).sort((a, b) => a.x - b.x);
      const x0 = Math.min(...its.map(t => t.bbox[0])), y0 = Math.min(...its.map(t => t.bbox[1]));
      const x1 = Math.max(...its.map(t => t.bbox[0] + t.bbox[2])), y1 = Math.max(...its.map(t => t.bbox[1] + t.bbox[3]));
      return { id: `L${i + 1}`, text: its.map(t => t.text).join(' '), bbox: [x0, y0, x1 - x0, y1 - y0].map(Math.round) };
    });
  }
  hasStrokes() { return this.items.some(it => it.type === 'stroke'); }
  signature() { return this.items.map(it => it.type === 'text' ? `${it.row}:${it.text}` : it.pts.length).join('|'); }
  occupied(box) { return this.items.some(it => overlap(it.bbox, box)); }

  // White page with only the student's ink (typed lines included), cropped below the content.
  snapshot() {
    const bottom = Math.min(PAGE_H, Math.max(TOP + RULE, ...this.items.map(it => it.bbox[1] + it.bbox[3])) + 40);
    const c = document.createElement('canvas');
    c.width = PAGE_W; c.height = Math.ceil(bottom);
    const g = c.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height);
    this.items.forEach(it => paint(g, it));
    return c.toDataURL('image/png');
  }
}

export { overlap, inBox };
