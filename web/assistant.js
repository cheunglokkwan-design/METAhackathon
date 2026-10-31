// Meta's ink: renders /api/assist annotations as handwriting on their own SVG layer.
import { PAGE_W, PAGE_H, RULE, MARGIN_X, rowY, rowAt, baselineOf, overlap, inBox } from './ink.js';

const NS = 'http://www.w3.org/2000/svg';
const FONT = 'Caveat';
const CPS = 34;                                   // handwriting speed, chars per second
const mctx = document.createElement('canvas').getContext('2d');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const rand = (a, b) => a + Math.random() * (b - a);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const ease = t => t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;

function svgEl(tag, attrs = {}, parent) {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  parent?.append(n);
  return n;
}
function smoothPath(pts) {
  let d = `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [x, y] = pts[i], [nx, ny] = pts[i + 1];
    d += ` Q${x.toFixed(1)} ${y.toFixed(1)} ${((x + nx) / 2).toFixed(1)} ${((y + ny) / 2).toFixed(1)}`;
  }
  const [lx, ly] = pts.at(-1);
  return d + ` L${lx.toFixed(1)} ${ly.toFixed(1)}`;
}
const measure = (text, px) => { mctx.font = `${px}px ${FONT}, cursive`; return mctx.measureText(text).width; };
function wrap(text, px, maxW) {
  const out = [];
  let line = '';
  for (const w of String(text).split(/\s+/)) {
    const next = line ? `${line} ${w}` : w;
    if (line && measure(next, px) > maxW) { out.push(line); line = w; } else line = next;
  }
  if (line) out.push(line);
  return out;
}

// Finished markup of a mark: no clip paths (they live in <defs>), no nib, no half-drawn dashes.
function cleanMarkup(g) {
  const c = g.cloneNode(true);
  c.querySelectorAll('.nib, rect.hit').forEach(n => n.remove());
  c.querySelectorAll('[clip-path]').forEach(n => n.removeAttribute('clip-path'));
  c.querySelectorAll('.ink-stroke').forEach(n => n.removeAttribute('style'));
  return c.innerHTML;
}

// Paints Meta's ink onto a 2D canvas (PNG/PDF export, thumbnails). `marks` is serialized data.
export const META_COLOR = '#d0452b';
export function paintMeta(ctx, marks = []) {
  const root = document.createElementNS(NS, 'svg');
  for (const d of marks) {
    const g = svgEl('g', {}, root);
    g.innerHTML = d.html;
    const ghost = d.kind === 'ghost';
    for (const p of g.querySelectorAll('path.ink-stroke')) {
      ctx.save();
      ctx.globalAlpha = 0.92; ctx.strokeStyle = META_COLOR; ctx.lineCap = ctx.lineJoin = 'round';
      ctx.lineWidth = +p.getAttribute('stroke-width') || 2.4;
      ctx.stroke(new Path2D(p.getAttribute('d')));
      ctx.restore();
    }
    for (const t of g.querySelectorAll('text')) {
      ctx.save();
      const x = +t.getAttribute('x'), y = +t.getAttribute('y');
      const rot = /rotate\(([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)/.exec(t.parentNode.getAttribute?.('transform') || '');
      if (rot) { ctx.translate(+rot[2], +rot[3]); ctx.rotate(+rot[1] * Math.PI / 180); ctx.translate(-rot[2], -rot[3]); }
      if (t.classList.contains('read-label')) { ctx.font = '500 10.5px Inter, sans-serif'; ctx.fillStyle = '#9aa3ad'; }
      else { ctx.font = `600 ${t.getAttribute('font-size') || 30}px ${FONT}, cursive`; ctx.fillStyle = META_COLOR; ctx.globalAlpha = ghost ? 0.32 : 1; }
      ctx.fillText(t.textContent, x, y);
      ctx.restore();
    }
  }
}

export async function assist(payload, timeoutMs = 45000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch('/api/assist', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), signal: ctl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally { clearTimeout(timer); }
}

export class MetaInk {
  constructor(svg, nb, { onReveal, onNoteTap } = {}) {
    Object.assign(this, { svg, nb, onReveal, onNoteTap });
    this.marks = []; this.run = 0; this.n = 0; this.byId = new Map();
    svg.setAttribute('viewBox', `0 0 ${PAGE_W} ${PAGE_H}`);
    this.defs = svgEl('defs', {}, svg);
  }

  // ---- layer management ------------------------------------------------------
  mark(kind) {
    const g = svgEl('g', { class: `mark ${kind}` }, this.svg);
    const m = { g, kind, pts: [], boxes: [] };
    this.marks.push(m);
    return m;
  }
  // record: take an undo snapshot first (the student cleared Meta's ink on purpose).
  clear(record = false) {
    if (record && this.marks.length) this.nb.checkpoint();
    this.run++;
    const gone = this.marks;
    this.marks = [];
    for (const m of gone) { m.g.classList.add('fade'); m.t = setTimeout(() => m.g.remove(), 380); }
    return gone.length;
  }
  erase({ x, y }, R) {
    const hit = this.marks.filter(m => m.boxes.some(b => inBox(b, x, y, R / 2)) || m.pts.some(([px, py]) => Math.hypot(px - x, py - y) < R + 3));
    for (const m of hit) { m.g.remove(); this.marks.splice(this.marks.indexOf(m), 1); }
    return hit.length > 0;
  }

  // ---- undo snapshots (see Notebook.ext) -----------------------------------------
  save() { return this.marks.slice(); }
  load(list = []) {
    this.run++;
    for (const m of this.marks) if (!list.includes(m)) m.g.remove();
    for (const m of list) { clearTimeout(m.t); m.g.classList.remove('fade'); this.svg.append(m.g); }
    this.marks = [...list];
  }

  // ---- persistence: marks as plain JSON (finished SVG markup + hit boxes) ----------
  serialize() {
    return this.marks.map(m => ({ kind: m.kind, html: cleanMarkup(m.g), boxes: m.boxes, pts: m.pts.map(([x, y]) => [Math.round(x), Math.round(y)]), note: m.note || null }));
  }
  // Replace the layer with stored marks (switching pages, reload). Drops everything in flight.
  restore(data = []) {
    this.run++;
    for (const m of this.marks) { clearTimeout(m.t); m.g.remove(); }
    this.svg.querySelectorAll('g.mark').forEach(g => g.remove());
    this.defs.replaceChildren();
    this.marks = [];
    for (const d of data || []) {
      const m = this.mark(d.kind);
      m.g.innerHTML = d.html;
      m.boxes = d.boxes || []; m.pts = d.pts || []; m.note = d.note || null;
      if (m.note) this.makeTappable(m);
    }
  }
  makeTappable(m) {
    if (!this.onNoteTap || m.g.classList.contains('tap-target')) return;
    m.g.classList.add('tap-target');
    m.g.setAttribute('role', 'button');
    if (!m.g.querySelector('rect.hit') && m.boxes.length) {
      const pad = 6, xs = m.boxes.map(q => q[0]), ys = m.boxes.map(q => q[1]);
      svgEl('rect', { class: 'hit', x: Math.min(...xs) - pad, y: Math.min(...ys) - pad, width: Math.max(...m.boxes.map(q => q[0] + q[2])) - Math.min(...xs) + 2 * pad, height: Math.max(...m.boxes.map(q => q[1] + q[3])) - Math.min(...ys) + 2 * pad, rx: 8 }, m.g);
    }
    m.g.addEventListener('click', () => this.onNoteTap(m.note));
  }
  metaBoxes() { return this.marks.flatMap(m => m.boxes); }
  isFree(box) { return !this.nb.occupied(box) && !this.metaBoxes().some(b => overlap(b, box)); }
  freeRow(r, x, w, n = 1) {
    for (let k = r; k < r + 30 && rowY(k + n - 1) < PAGE_H - 10; k++) {
      let ok = true;
      for (let j = 0; j < n && ok; j++) ok = this.isFree([x, rowY(k + j) - RULE + 8, w, RULE - 10]);
      if (ok) return k;
    }
    return r;
  }

  // ---- primitives --------------------------------------------------------------
  tween(ms, fn, token) {
    return new Promise(res => {
      const t0 = performance.now();
      const step = now => {
        const t = Math.min(1, (now - t0) / ms);
        fn(t);
        if (t < 1 && token === this.run) requestAnimationFrame(step); else { fn(1); res(); }
      };
      requestAnimationFrame(step);
    });
  }
  async stroke(m, pts, d, ms, token, width = 2.4) {
    const p = svgEl('path', { d, class: 'ink-stroke', 'stroke-width': width }, m.g);
    m.pts.push(...pts);
    const len = p.getTotalLength() + 1;
    p.style.strokeDasharray = `${len} ${len}`;
    await this.tween(ms, t => { p.style.strokeDashoffset = len * (1 - ease(t)); }, token);
  }
  // Text revealed left to right through a growing clip rect, with a moving nib.
  async write(m, text, x, base, px, token) {
    const w = measure(text, px);
    const box = [x - 4, base - px * 0.8, w + 8, px * 1.05];
    m.boxes.push(box);
    this.onReveal?.(box);
    const id = `mclip${++this.n}`;
    const rect = svgEl('rect', { x: x - 8, y: base - px * 1.4, width: 0, height: px * 2.1 }, svgEl('clipPath', { id }, this.defs));
    const g = svgEl('g', { transform: `rotate(${rand(-1.1, 1.1).toFixed(2)} ${x} ${base})`, 'clip-path': `url(#${id})` }, m.g);
    const t = svgEl('text', { x, y: base, 'font-size': px }, g);
    t.textContent = text;
    const chars = [...text];
    t.setAttribute('rotate', chars.map(() => rand(-2.5, 2.5).toFixed(1)).join(' '));
    let prev = 0;
    t.setAttribute('dy', chars.map(() => { const o = rand(-0.8, 0.8), d = o - prev; prev = o; return d.toFixed(2); }).join(' '));
    const nib = svgEl('circle', { r: 2, class: 'nib', cy: base - px * 0.3 }, m.g);
    const ms = Math.max(350, chars.length / CPS * 1000 * rand(0.9, 1.1)), k = Math.max(1, chars.length / 4);
    await this.tween(ms, s => {
      const p = clamp(s - 0.6 * Math.sin(2 * Math.PI * s * k) / (2 * Math.PI * k), 0, 1);   // bursty but monotonic
      rect.setAttribute('width', (w + 16) * p);
      nib.setAttribute('cx', x + w * p);
      nib.setAttribute('cy', base - px * (0.25 + 0.15 * Math.sin(s * 40)));
    }, token);
    rect.setAttribute('width', w + 16);
    nib.remove();
  }
  arrow(from, to, ctrl) {
    const pts = [];
    for (let i = 0; i <= 16; i++) {
      const t = i / 16, u = 1 - t;
      pts.push([u * u * from[0] + 2 * u * t * ctrl[0] + t * t * to[0], u * u * from[1] + 2 * u * t * ctrl[1] + t * t * to[1]]);
    }
    const ang = Math.atan2(to[1] - ctrl[1], to[0] - ctrl[0]), L = 10;
    const h1 = [to[0] - L * Math.cos(ang - 0.5), to[1] - L * Math.sin(ang - 0.5)];
    const h2 = [to[0] - L * Math.cos(ang + 0.5), to[1] - L * Math.sin(ang + 0.5)];
    const d = `M${from[0]} ${from[1]} Q${ctrl[0]} ${ctrl[1]} ${to[0]} ${to[1]} M${h1[0]} ${h1[1]} L${to[0]} ${to[1]} L${h2[0]} ${h2[1]}`;
    return { d, pts };
  }

  // ---- annotations -------------------------------------------------------------
  box(id) { const b = this.byId.get(id)?.bbox; return Array.isArray(b) && b.length === 4 ? b : null; }
  lastBox() { return [...this.byId.values()].map(l => l.bbox).filter(b => b?.length === 4).sort((a, b) => (a[1] + a[3]) - (b[1] + b[3])).at(-1) || null; }
  sizeFor(b, k = 0.95) { return Math.round(clamp((b?.[3] ?? 32) * k, 26, 40)); }   // Meta writes as big as the student

  async render(resp, sentLines) {
    this.clear();
    const token = this.run;
    try { await document.fonts.load(`32px ${FONT}`); } catch { /* fallback font */ }
    this.byId = new Map(sentLines.map(l => [l.id, l]));
    for (const l of resp.lines || []) this.byId.set(l.id, { ...this.byId.get(l.id), ...l, bbox: l.bbox?.length === 4 ? l.bbox : this.byId.get(l.id)?.bbox });
    this.writeRow = null;

    // Show what Meta read for lines it read from handwriting, so misreads are easy to spot.
    const sentText = new Map(sentLines.map(l => [l.id, l.text]));
    for (const l of resp.lines || []) if (l.text && this.box(l.id) && sentText.get(l.id) !== l.text) this.readLabel(l);

    const anns = [...(resp.annotations || [])];
    const ticked = new Set(anns.filter(a => a.kind === 'tick').map(a => a.target));
    for (const l of resp.lines || []) if (l.status === 'ok' && !ticked.has(l.id)) anns.push({ kind: 'tick', target: l.id });
    const order = { tick: 0, circle: 1, underline: 2, note: 3, ghost: 4, write: 5 };
    const todo = anns.filter(a => a.kind in order).sort((a, b) => order[a.kind] - order[b.kind]);
    if (!todo.length && resp.verdict) todo.push({ kind: 'note', target: null, text: resp.verdict });
    for (const a of todo) {
      if (token !== this.run) return;
      try { await this[`a_${a.kind}`](a, token); } catch (err) { console.warn('[margin] skipped annotation', a, err); }
      await sleep(a.kind === 'tick' ? 40 : 140);
    }
    return todo.length;
  }

  readLabel(l) {
    const b = this.box(l.id), m = this.mark('read');
    const t = svgEl('text', { x: b[0], y: b[1] + b[3] + 16, class: 'read-label' }, m.g);
    t.textContent = `I read: ${l.text}`;
    m.boxes.push([b[0], b[1] + b[3] + 7, Math.min(PAGE_W - b[0], 6 * (l.text.length + 8)), 12]);
  }

  async a_tick(a, token) {
    const b = this.box(a.target);
    if (!b) return;
    const m = this.mark('tick'), cy = b[1] + b[3] * 0.5, x = clamp(b[0] - 44, 8, MARGIN_X - 40);
    const pts = [[x, cy - 1], [x + 4, cy + 4], [x + 9, cy + 9], [x + 17, cy - 3], [x + 27 + rand(-2, 2), cy - 15 + rand(-2, 2)]];
    await this.stroke(m, pts, smoothPath(pts), 260, token, 2.8);
  }

  async a_circle(a, token) {
    const b = this.box(a.target);
    if (!b) return;
    const m = this.mark('circle');
    const cx = b[0] + b[2] / 2, cy = b[1] + b[3] / 2, rx = b[2] / 2 + 20, ry = b[3] / 2 + 13;
    const a0 = rand(-2.9, -2.4), sweep = Math.PI * 2 * rand(1.05, 1.1), ph = rand(0, 6), pts = [];
    for (let i = 0; i <= 72; i++) {
      const t = i / 72, th = a0 + sweep * t, wob = 1 + 0.025 * Math.sin(th * 3 + ph) + 0.05 * t;
      pts.push([cx + Math.cos(th) * rx * wob, cy + Math.sin(th) * ry * (wob + 0.03 * t)]);
    }
    this.onReveal?.([cx - rx, cy - ry, rx * 2, ry * 2]);
    await this.stroke(m, pts, smoothPath(pts), 650, token, 2.6);
  }

  async a_underline(a, token) {
    const b = this.box(a.target);
    if (!b) return;
    const m = this.mark('underline'), y = b[1] + b[3] + 3, pts = [];
    for (let x = b[0] - 4; x <= b[0] + b[2] + 4; x += 6) pts.push([x, y + Math.sin((x - b[0]) / 6 * Math.PI / 2) * 2.6 + rand(-0.6, 0.6)]);
    m.boxes.push([b[0] - 4, y - 4, b[2] + 8, 8]);
    await this.stroke(m, pts, smoothPath(pts), 450, token, 2);
  }

  async a_note(a, token) {
    if (!a.text) return;
    const b = this.box(a.target) || this.lastBox();
    const m = this.mark('note');
    const px = this.sizeFor(b);
    if (!b) {                                         // nothing on the page to point at
      const r = this.freeRow(1, MARGIN_X + 40, measure(a.text, px));
      return this.write(m, a.text, MARGIN_X + 40, baselineOf(r), px, token);
    }
    const right = b[0] + b[2] + 60, avail = PAGE_W - 28 - right, full = measure(a.text, px);
    let lines, x, bases, from, to, ctrl;
    if (a.side !== 'below' && avail >= Math.min(full, 220)) {
      lines = wrap(a.text, px, avail); x = right;
      const base = b[1] + b[3] * 0.75;
      bases = lines.map((_, i) => base + i * px * 1.02);
      from = [x - 10, base - px * 0.35]; to = [b[0] + b[2] + 22, b[1] + b[3] * 0.45]; ctrl = [(from[0] + to[0]) / 2, Math.min(from[1], to[1]) - 18];
    } else {
      x = clamp(b[0] + 30, MARGIN_X + 20, PAGE_W - 300);
      lines = wrap(a.text, px, PAGE_W - 28 - x);
      const r = this.freeRow(rowAt(b[1] + b[3]) + 1, x - 30, Math.max(...lines.map(l => measure(l, px))) + 30, lines.length);
      bases = lines.map((_, i) => baselineOf(r + i));
      from = [x - 12, bases[0] - px * 0.4]; to = [b[0] - 8, b[1] + b[3] * 0.6]; ctrl = [b[0] - 34, from[1] - 6];   // curl up the left edge
    }
    const arr = this.arrow(from, to, ctrl);
    await this.stroke(m, arr.pts, arr.d, 320, token, 2.2);
    for (let i = 0; i < lines.length && token === this.run; i++) await this.write(m, lines[i], x, bases[i], px, token);
    m.note = { kind: 'note', target: a.target ?? null, text: a.text };
    this.makeTappable(m);                            // tap a hint to get a more specific one
  }

  async a_write(a, token, ghost = false) {
    if (!a.text) return;
    const after = this.box(a.after) || this.lastBox();
    const x = after ? Math.max(MARGIN_X + 14, after[0]) : MARGIN_X + 40, px = this.sizeFor(after, 1);
    let row = this.writeRow != null ? this.writeRow + 1 : after ? rowAt(after[1] + after[3]) + 1 : 1;
    for (const part of String(a.text).split('\n').filter(s => s.trim())) {
      for (const line of wrap(part, px, PAGE_W - 28 - x)) {
        if (token !== this.run) return;
        row = this.freeRow(row, x, measure(line, px));
        this.writeRow = row;
        const m = this.mark(ghost ? 'ghost' : 'write');
        await this.write(m, line, x, baselineOf(row), px, token);
        row++;
      }
    }
  }
  a_ghost(a, token) { return this.a_write(a, token, true); }
}
