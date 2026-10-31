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
function ptsBox(pts) {
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const x0 = Math.min(...xs), y0 = Math.min(...ys);
  return [x0 - 3, y0 - 3, Math.max(...xs) - x0 + 6, Math.max(...ys) - y0 + 6];
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

// Clean typeset maths: Latin letters in italic (variables), function names and digits upright.
export const MATH_FONT = '"STIX Two Text", "Cambria Math", "Times New Roman", serif';
const FUNCS = /^(sin|cos|tan|sec|csc|cot|arcsin|arccos|arctan|sinh|cosh|tanh|ln|log|exp|lim|max|min|d|dx|dy|dt|du|C)$/;
export function mathRuns(text) {
  const runs = [];
  for (const tok of String(text).match(/[A-Za-z]+|[^A-Za-z]+/g) || []) {
    if (/^[A-Za-z]+$/.test(tok) && !FUNCS.test(tok)) for (const ch of tok) runs.push({ t: ch, it: true });
    else if (/^d[a-z]$/.test(tok)) runs.push({ t: 'd', it: false }, { t: tok[1], it: true });
    else runs.push({ t: tok, it: false });
  }
  return runs;
}
const mathFont = (px, it) => `${it ? 'italic ' : ''}400 ${px}px ${MATH_FONT}`;
const measureMath = (text, px) => mathRuns(text).reduce((w, r) => { mctx.font = mathFont(px, r.it); return w + mctx.measureText(r.t).width; }, 0);

// Paints Meta's ink onto a 2D canvas (PNG/PDF export, thumbnails). `marks` is serialized data.
export const META_COLOR = '#d0452b';
export function paintMeta(ctx, marks = []) {
  const root = document.createElementNS(NS, 'svg');
  for (const d of marks) {
    const g = svgEl('g', {}, root);
    g.innerHTML = d.html;
    const ghost = d.kind === 'ghost';
    for (const p of g.querySelectorAll('path.shade')) {
      ctx.save(); ctx.fillStyle = META_COLOR; ctx.globalAlpha = +p.getAttribute('fill-opacity') || 0.14;
      ctx.fill(new Path2D(p.getAttribute('d'))); ctx.restore();
    }
    for (const p of g.querySelectorAll('path.ink-stroke')) {
      ctx.save();
      ctx.globalAlpha = +(p.getAttribute('opacity') ?? 0.92) || 0.92; ctx.strokeStyle = META_COLOR; ctx.lineCap = ctx.lineJoin = 'round';
      ctx.lineWidth = +p.getAttribute('stroke-width') || 2.4;
      const dash = p.getAttribute('stroke-dasharray');
      if (dash) ctx.setLineDash(dash.split(/[\s,]+/).map(Number));
      ctx.stroke(new Path2D(p.getAttribute('d')));
      ctx.restore();
    }
    for (const t of g.querySelectorAll('text')) {
      ctx.save();
      const x = +t.getAttribute('x'), y = +t.getAttribute('y');
      const rot = /rotate\(([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)/.exec(t.parentNode.getAttribute?.('transform') || '');
      if (rot) { ctx.translate(+rot[2], +rot[3]); ctx.rotate(+rot[1] * Math.PI / 180); ctx.translate(-rot[2], -rot[3]); }
      if (t.classList.contains('read-label')) { ctx.font = '500 10.5px Inter, sans-serif'; ctx.fillStyle = '#9aa3ad'; ctx.fillText(t.textContent, x, y); }
      else if (t.classList.contains('typeset')) {
        const px = +t.getAttribute('font-size') || 24;
        ctx.fillStyle = META_COLOR;
        let cx = x;
        for (const r of mathRuns(t.textContent)) { ctx.font = mathFont(px, r.it); ctx.fillText(r.t, cx, y); cx += ctx.measureText(r.t).width; }
      } else {
        ctx.font = `600 ${t.getAttribute('font-size') || 30}px ${FONT}, cursive`; ctx.fillStyle = META_COLOR; ctx.globalAlpha = ghost ? 0.32 : +(t.getAttribute('opacity') ?? 1);
        ctx.fillText(t.textContent, x, y);
      }
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
  // Fade out only the marks of the given kinds, so tidy-ups and graphs survive a new hint.
  clearKinds(kinds) {
    this.run++;
    const gone = this.marks.filter(m => kinds.has(m.kind));
    this.marks = this.marks.filter(m => !kinds.has(m.kind));
    for (const m of gone) { m.g.classList.add('fade'); m.t = setTimeout(() => m.g.remove(), 380); }
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
  // Everything Meta has drawn: text boxes plus the extent of its strokes (circles, ticks, arrows).
  metaBoxes() { return this.marks.flatMap(m => m.pts.length > 1 ? [...m.boxes, ptsBox(m.pts)] : m.boxes); }
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
        const t = clamp((now - t0) / ms, 0, 1);            // rAF time can be a little before t0
        fn(t);
        if (t < 1 && token === this.run) requestAnimationFrame(step); else { fn(1); res(); }
      };
      requestAnimationFrame(step);
    });
  }
  async stroke(m, pts, d, ms, token, width = 2.4, attrs = {}) {
    const p = svgEl('path', { d, class: 'ink-stroke', 'stroke-width': width, ...attrs }, m.g);
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
    // Typeset and graph ink stack with hints; each kind of answer replaces only its own kind.
    const EXTRAS = new Set(['typeset', 'graph']);
    const kinds = new Set((resp.annotations || []).map(a => a.kind));
    const extrasOnly = kinds.size > 0 && [...kinds].every(k => EXTRAS.has(k));
    if (extrasOnly) this.clearKinds(kinds);
    else this.clearKinds(new Set(this.marks.map(m => m.kind).filter(k => !EXTRAS.has(k))));
    const token = this.run;
    try { await document.fonts.load(`32px ${FONT}`); } catch { /* fallback font */ }
    if ((resp.annotations || []).some(a => a.kind === 'typeset')) {
      try { await Promise.all([document.fonts.load(mathFont(24, false)), document.fonts.load(mathFont(24, true))]); } catch { /* serif fallback */ }
    }
    this.byId = new Map(sentLines.map(l => [l.id, l]));
    for (const l of resp.lines || []) this.byId.set(l.id, { ...this.byId.get(l.id), ...l, bbox: l.bbox?.length === 4 ? l.bbox : this.byId.get(l.id)?.bbox });
    this.writeRow = null;
    this.tsAnns = (resp.annotations || []).filter(a => a.kind === 'typeset' && a.text && this.box(a.target));
    this.tsPlan = null;                               // laid out when the first one is drawn (after the notes)

    // Show what Meta read for lines it read from handwriting, so misreads are easy to spot.
    const sentText = new Map(sentLines.map(l => [l.id, l.text]));
    for (const l of resp.lines || []) if (l.text && this.box(l.id) && sentText.get(l.id) !== l.text) this.readLabel(l);

    const anns = [...(resp.annotations || [])];
    const ticked = new Set(anns.filter(a => a.kind === 'tick').map(a => a.target));
    for (const l of resp.lines || []) if (l.status === 'ok' && !ticked.has(l.id)) anns.push({ kind: 'tick', target: l.id });
    const order = { tick: 0, circle: 1, underline: 2, note: 3, ghost: 4, write: 5, typeset: 6, graph: 7 };
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
    const rightLines = avail > 60 ? wrap(a.text, px, avail) : [];
    const rightFree = rightLines.length && this.isFree([right - 30, b[1] - 4, Math.max(...rightLines.map(l => measure(l, px))) + 30, rightLines.length * px * 1.02 + 8]);
    if (a.side !== 'below' && avail >= Math.min(full, 220) && rightFree) {
      lines = rightLines; x = right;
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

  // ---- typeset: the neat typed version of each line, in a clean serif ----
  // One column to the right of everything already on those lines (student ink, notes, circles)
  // when it fits; otherwise a "neat copy" block in the first free rows below the student's work.
  planTypeset() {
    const items = this.tsAnns.map(a => {
      const b = this.box(a.target), px = Math.round(clamp(b[3] * 0.78, 20, 27));
      return { a, b, px, w: measureMath(a.text, px) };
    });
    const plan = new Map();
    if (!items.length) return plan;
    const taken = [...this.metaBoxes(), ...this.nb.items.filter(it => !it.bg).flatMap(it => it.rowBoxes || [it.bbox])];
    const bandRight = b => Math.max(b[0] + b[2], ...taken.filter(r => r[1] < b[1] + b[3] + 2 && b[1] - 2 < r[1] + r[3] && r[0] < PAGE_W - 40).map(r => r[0] + r[2]));
    const colX = Math.max(...items.map(it => bandRight(it.b))) + 30;
    const boxAt = (it, x, base) => [x - 4, base - it.px * 0.85, it.w + 8, it.px * 1.15];
    if (items.every(it => colX + it.w <= PAGE_W - 24 && this.isFree(boxAt(it, colX, it.b[1] + it.b[3] * 0.78)))) {
      for (const it of items) plan.set(it.a, { x: colX, base: it.b[1] + it.b[3] * 0.78, px: it.px, w: it.w });
      return plan;
    }
    const x = clamp(Math.min(...items.map(it => it.b[0])), MARGIN_X + 14, PAGE_W - 24 - Math.max(...items.map(it => it.w)));
    const low = Math.max(...this.nb.lines().map(l => l.bbox[1] + l.bbox[3]), ...items.map(it => it.b[1] + it.b[3]));
    const r = this.freeRow(rowAt(low) + 1, x - 4, Math.max(160, ...items.map(it => it.w)) + 8, items.length + 1);
    plan.label = { x, base: baselineOf(r) - 6 };
    items.forEach((it, i) => plan.set(it.a, { x, base: baselineOf(r + 1 + i), px: it.px, w: it.w }));
    return plan;
  }

  async a_typeset(a, token) {
    if (!this.tsAnns?.includes(a)) return;
    this.tsPlan ??= this.planTypeset();
    const p = this.tsPlan.get(a);
    if (!p) return;
    if (this.tsPlan.label && !this.tsPlan.labelDone) {
      this.tsPlan.labelDone = true;
      const lm = this.mark('typeset');
      const { x, base } = this.tsPlan.label;
      const t = svgEl('text', { x, y: base, 'font-size': 18, opacity: 0.75 }, lm.g);
      t.textContent = 'neat copy';
      const w = measure('neat copy', 18);
      const u = [[x, base + 5], [x + w * 0.5, base + 6.5], [x + w, base + 4.5]];
      lm.boxes.push([x - 4, base - 16, w + 8, 24]);
      await this.stroke(lm, u, smoothPath(u), 200, token, 1.4, { opacity: 0.5 });
    }
    const { x, base, px, w } = p;
    const m = this.mark('typeset');
    const box = [x - 4, base - px * 0.85, w + 8, px * 1.15];
    m.boxes.push(box);
    this.onReveal?.(box);
    const id = `mclip${++this.n}`;
    const rect = svgEl('rect', { x: x - 4, y: base - px * 1.3, width: 0, height: px * 1.9 }, svgEl('clipPath', { id }, this.defs));
    const g = svgEl('g', { 'clip-path': `url(#${id})` }, m.g);
    const t = svgEl('text', { x, y: base, 'font-size': px, class: 'typeset' }, g);
    for (const r of mathRuns(a.text)) { const s = svgEl('tspan', r.it ? { 'font-style': 'italic' } : {}, t); s.textContent = r.t; }
    await this.tween(Math.max(300, [...a.text].length * 18), q => rect.setAttribute('width', (w + 10) * q), token);
  }

  // ---- graph: a small hand-sketched plot in the right margin near the line (or under it) ----
  graphSpot(anchor, W, H) {
    const tries = [];
    const y0 = anchor ? clamp(anchor[1] - 10, 90, PAGE_H - H - 10) : 120;
    for (const x of [PAGE_W - 24 - W, PAGE_W - 24 - W - 110]) for (let dy = 0; y0 + dy + H < PAGE_H - 10 && dy < 520; dy += RULE) tries.push([x, y0 + dy]);
    for (const x of [MARGIN_X + 40, MARGIN_X + 300]) {
      const r = anchor ? rowAt(anchor[1] + anchor[3]) + 1 : 2;
      for (let k = r; rowY(k) + H < PAGE_H - 10 && k < r + 25; k++) tries.push([x, rowY(k) - RULE + 10]);
    }
    for (const [x, y] of tries) if (this.isFree([x - 8, y - 8, W + 16, H + 16])) return [x, y];
    return tries[0] || [PAGE_W - 24 - W, 120];
  }

  async a_graph(a, token) {
    const pts = (a.points || []).filter(p => Array.isArray(p) && p.length >= 2);
    const [x0, x1] = a.xrange?.length === 2 ? a.xrange : pts.length ? [Math.min(...pts.map(p => p[0])), Math.max(...pts.map(p => p[0]))] : [0, 1];
    const fin = pts.map(p => p[1]).filter(Number.isFinite);
    const [y0, y1] = a.yrange?.length === 2 ? a.yrange : fin.length ? [Math.min(...fin), Math.max(...fin)] : [0, 1];
    if (!pts.length || !(x1 > x0) || !(y1 > y0)) return;
    const W = 260, H = 180;
    const anchor = this.box(a.after) || this.lastBox();
    const [gx, gy] = this.graphSpot(anchor, W, H);
    const m = this.mark('graph');
    m.boxes.push([gx, gy, W, H]);
    this.onReveal?.([gx, gy, W, H]);
    const twoRows = (a.overlay || []).length > 1 && a.fn;
    const L = gx + 16, R = gx + W - 12, T = gy + (twoRows ? 48 : 30), B = gy + H - 14;
    const sx = x => L + (x - x0) / (x1 - x0) * (R - L), sy = y => B - (y - y0) / (y1 - y0) * (B - T);
    const wob = (pa, pb, n = 8) => {
      const out = [];
      for (let i = 0; i <= n; i++) { const t = i / n, j = i && i < n ? rand(-0.7, 0.7) : 0; out.push([pa[0] + (pb[0] - pa[0]) * t + (pa[1] === pb[1] ? 0 : j), pa[1] + (pb[1] - pa[1]) * t + (pa[1] === pb[1] ? j : 0)]); }
      return out;
    };
    // axes through the origin when it is in view, else along the edges
    const ax = y0 <= 0 && y1 >= 0 ? sy(0) : B, ay = x0 <= 0 && x1 >= 0 ? sx(0) : L;
    const xAxis = wob([L - 6, ax], [R + 4, ax]), yAxis = wob([ay, B + 4], [ay, T - 8]);
    const head = (tip, dir) => dir === 'x' ? `M${tip[0] - 7} ${tip[1] - 4} L${tip[0]} ${tip[1]} L${tip[0] - 7} ${tip[1] + 4}` : `M${tip[0] - 4} ${tip[1] + 7} L${tip[0]} ${tip[1]} L${tip[0] + 4} ${tip[1] + 7}`;
    await this.stroke(m, xAxis, smoothPath(xAxis) + head(xAxis.at(-1), 'x'), 260, token, 1.5, { opacity: 0.55 });
    await this.stroke(m, yAxis, smoothPath(yAxis) + head(yAxis.at(-1), 'y'), 220, token, 1.5, { opacity: 0.55 });
    for (const [t, x, y] of [['x', R + 2, ax + 16], ['y', ay + 7, T - 2]]) {
      const lt = svgEl('text', { x, y, 'font-size': 16, opacity: 0.7 }, m.g); lt.textContent = t;
    }
    const segs = graphSegments(pts, y0, y1).map(s => s.map(([x, y]) => [sx(x), sy(y)]));
    // shaded area for a definite integral
    if (Array.isArray(a.shade) && a.shade.length === 2) {
      const [sa, sb] = [Math.min(...a.shade), Math.max(...a.shade)];
      const inside = pts.filter(p => Number.isFinite(p[1]) && p[0] >= sa && p[0] <= sb).map(([x, y]) => [sx(x), sy(clamp(y, y0, y1))]);
      if (inside.length > 1) {
        const d = `M${inside[0][0].toFixed(1)} ${ax.toFixed(1)} ` + inside.map(([x, y]) => `L${x.toFixed(1)} ${y.toFixed(1)}`).join(' ') + ` L${inside.at(-1)[0].toFixed(1)} ${ax.toFixed(1)} Z`;
        const sh = svgEl('path', { d, class: 'shade', 'fill-opacity': 0.14 }, m.g);
        sh.style.opacity = 0;
        await this.tween(300, t => { sh.style.opacity = t; }, token);
        sh.removeAttribute('style');
      }
    }
    // the curve, sketched left to right
    const total = segs.reduce((n, s) => n + s.length, 0) || 1;
    for (const s of segs) {
      if (token !== this.run) return;
      if (s.length > 1) await this.stroke(m, s, smoothPath(s), Math.max(120, 1100 * s.length / total), token, 2.2);
    }
    // overlay (e.g. the integrand under its antiderivative): dashed, revealed left to right
    const ov = (a.overlay || []).filter(p => Array.isArray(p) && p.length >= 2);
    if (ov.length > 1) {
      const id = `mclip${++this.n}`;
      const rect = svgEl('rect', { x: L - 4, y: gy, width: 0, height: H }, svgEl('clipPath', { id }, this.defs));
      const g = svgEl('g', { 'clip-path': `url(#${id})` }, m.g);
      for (const s of graphSegments(ov, y0, y1).map(s => s.map(([x, y]) => [sx(x), sy(y)]))) {
        if (s.length > 1) { svgEl('path', { d: smoothPath(s), class: 'ink-stroke', 'stroke-width': 1.7, 'stroke-dasharray': '6 5', opacity: 0.75 }, g); m.pts.push(...s); }
      }
      await this.tween(700, t => rect.setAttribute('width', (R - L + 12) * t), token);
    }
    // legend: which curve is which (solid = fn, dashed = overlay)
    const cut = (t, n) => t.length > n ? t.slice(0, n - 1) + '…' : t;
    if (a.fn) {
      const two = ov.length > 1;
      const tx = two ? gx + 30 : gx + 4;
      if (two) { const sw = [[gx + 4, gy + 11], [gx + 24, gy + 11]]; await this.stroke(m, sw, smoothPath(sw), 120, token, 2.2, { opacity: 0.92, 'data-legend': 1 }); }
      await this.write(m, cut(`y = ${a.fn}`, two ? 30 : 34), tx, gy + 16, 19, token);
      if (two) {
        const sw = [[gx + 4, gy + 31], [gx + 24, gy + 31]];
        svgEl('path', { d: smoothPath(sw), class: 'ink-stroke', 'stroke-width': 1.7, 'stroke-dasharray': '6 5', opacity: 0.75 }, m.g);
        await this.write(m, cut(a.overlay_fn ? `y = ${a.overlay_fn}` : 'overlay', 30), tx, gy + 36, 19, token);
      }
    }
  }
}

// Samples -> drawable runs: breaks at gaps (a jump in x of more than ~2x the usual step,
// non-finite values) and clips each run to the y range, cutting it where it leaves the box.
export function graphSegments(points, y0, y1) {
  const pts = points.filter(p => Number.isFinite(p[0])).sort((a, b) => a[0] - b[0]);
  const steps = [];
  for (let i = 1; i < pts.length; i++) steps.push(pts[i][0] - pts[i - 1][0]);
  const step = steps.length ? steps.slice().sort((a, b) => a - b)[Math.floor(steps.length / 2)] : 0;
  const out = [];
  let cur = [];
  const flush = () => { if (cur.length > 1) out.push(cur); cur = []; };
  const inR = y => y >= y0 && y <= y1;
  for (let i = 0; i < pts.length; i++) {
    const [x, y] = pts[i], prev = pts[i - 1];
    if (!Number.isFinite(y)) { flush(); continue; }
    if (prev && Number.isFinite(prev[1]) && step && x - prev[0] > 2.2 * step) flush();
    const pin = prev && Number.isFinite(prev[1]) && !(step && x - prev[0] > 2.2 * step);
    if (inR(y)) {
      if (pin && !inR(prev[1])) {                       // re-entering: start at the edge
        const e = prev[1] > y1 ? y1 : y0, t = (e - prev[1]) / (y - prev[1]);
        cur.push([prev[0] + t * (x - prev[0]), e]);
      }
      cur.push([x, y]);
    } else {
      if (pin && inR(prev[1])) {                        // leaving: end at the edge
        const e = y > y1 ? y1 : y0, t = (e - prev[1]) / (y - prev[1]);
        cur.push([prev[0] + t * (x - prev[0]), e]);
      }
      flush();
    }
  }
  flush();
  return out;
}
