// Meta's ink: renders /api/assist annotations as handwriting on their own SVG layer.
// Words and maths are real pen strokes from Writey's handwriting engine (./handwriting.js),
// animated stroke by stroke; the Caveat font is only a fallback if the engine throws.
import { PAGE_W, PAGE_H, RULE, MARGIN_X, rowY, rowAt, baselineOf, overlap, inBox, isMeta, setMarkPainter, prepareItem } from './ink.js';
import { authClient } from './store.js';
import { saveImage } from './images.js';
import { mathBox, textBox, lineBox, paragraph, toLatex, isMath, pathD, penTimes, penWidth, emFor, newSeed } from './handwriting.js';
import { splitRows } from './vendor/writey/latex.js';

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
// The words a mark says, for tests, screen readers and "I read" checks.
export function markText(g) {
  return [...g.querySelectorAll('[data-text], text')].map(n => n.getAttribute('data-text') ?? n.textContent).filter(Boolean).join(' ');
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

// Paints Meta's ink onto a 2D canvas (PNG/PDF export, thumbnails). `marks` is serialized data:
// only previews and "I read" labels stay in page.meta now (finished ink is in page.items), but
// older pages keep every mark here, so this still draws them all.
export const META_COLOR = '#d0452b';
const READ_COLOR = '#9aa3ad';
export function paintMeta(ctx, marks = []) {
  for (const d of marks) paintMarkHtml(ctx, d.html, { kind: d.kind, faint: d.pending ? 0.34 : 1, color: d.color || META_COLOR });
}
const parsed = new Map();                          // html -> parsed <g>, so canvas repaints don't reparse
function parseMark(html) {
  let g = parsed.get(html);
  if (!g) {
    g = svgEl('g', {}, document.createElementNS(NS, 'svg'));
    g.innerHTML = html;
    if (parsed.size > 300) parsed.clear();
    parsed.set(html, g);
  }
  return g;
}
export function paintMarkHtml(ctx, html, { kind = '', faint = 1, color = META_COLOR } = {}) {
  const g = parseMark(html);
  const ghost = kind === 'ghost' && !g.querySelector('path.ink-stroke');       // older pages: ghost text
  if (kind === 'read') color = READ_COLOR;
  for (const p of g.querySelectorAll('path.shade')) {
    ctx.save(); ctx.fillStyle = color; ctx.globalAlpha = (+p.getAttribute('fill-opacity') || 0.14) * faint;
    ctx.fill(new Path2D(p.getAttribute('d'))); ctx.restore();
  }
  for (const p of g.querySelectorAll('path.ink-stroke')) {
    ctx.save();
    ctx.globalAlpha = (+(p.getAttribute('opacity') ?? 0.92) || 0.92) * faint; ctx.strokeStyle = color; ctx.lineCap = ctx.lineJoin = 'round';
    ctx.lineWidth = +p.getAttribute('stroke-width') || 2.4;
    const dash = p.getAttribute('stroke-dasharray');
    if (dash) ctx.setLineDash(dash.split(/[\s,]+/).map(Number));
    ctx.stroke(new Path2D(p.getAttribute('d')));
    ctx.restore();
  }
  for (const t of g.querySelectorAll('text')) {                 // Caveat fallback and older pages
    ctx.save();
    const x = +t.getAttribute('x'), y = +t.getAttribute('y');
    const rot = /rotate\(([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)/.exec(t.parentNode.getAttribute?.('transform') || '');
    if (rot) { ctx.translate(+rot[2], +rot[3]); ctx.rotate(+rot[1] * Math.PI / 180); ctx.translate(-rot[2], -rot[3]); }
    if (t.classList.contains('read-label')) { ctx.font = '500 10.5px Inter, sans-serif'; ctx.fillStyle = READ_COLOR; ctx.fillText(t.textContent, x, y); }
    else if (t.classList.contains('typeset')) {
      const px = +t.getAttribute('font-size') || 24;
      ctx.fillStyle = color;
      let cx = x;
      for (const r of mathRuns(t.textContent)) { ctx.font = mathFont(px, r.it); ctx.fillText(r.t, cx, y); cx += ctx.measureText(r.t).width; }
    } else {
      ctx.font = `600 ${t.getAttribute('font-size') || 30}px ${FONT}, cursive`; ctx.fillStyle = color; ctx.globalAlpha = (ghost ? 0.32 : +(t.getAttribute('opacity') ?? 1)) * faint;
      ctx.fillText(t.textContent, x, y);
    }
    ctx.restore();
  }
}
// Meta's graphs (and font-fallback marks) live in the notebook as type 'mark' items.
setMarkPainter((ctx, it) => {
  ctx.save(); ctx.translate(it.x || 0, it.y || 0);
  paintMarkHtml(ctx, it.html, { kind: it.mk, color: it.color || META_COLOR });
  ctx.restore();
});

// Polylines of a stored mark's paths (for hit-testing older marks that kept no point lists).
function polysFromHtml(html) {
  const out = [];
  for (const p of parseMark(html).querySelectorAll('path.ink-stroke')) {
    for (const sub of (p.getAttribute('d') || '').split(/(?=M)/)) {
      const n = (sub.match(/-?[\d.]+/g) || []).map(Number), pts = [];
      for (let i = 0; i + 1 < n.length; i += 2) pts.push([Math.round(n[i] * 10) / 10, Math.round(n[i + 1] * 10) / 10]);
      if (pts.length) out.push(pts);
    }
  }
  return out;
}
const unionBox = bs => {
  const x0 = Math.min(...bs.map(b => b[0])), y0 = Math.min(...bs.map(b => b[1]));
  return [x0, y0, Math.max(...bs.map(b => b[0] + b[2])) - x0, Math.max(...bs.map(b => b[1] + b[3])) - y0];
};
const r1 = v => Math.round(v * 10) / 10;

export async function assist(payload, timeoutMs = 45000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await authClient.fetchWithAuth('/api/assist', { method: 'POST', body: JSON.stringify(payload), signal: ctl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally { clearTimeout(timer); }
}

// Meta writes on its SVG layer (the animated "pen writing" moment). As soon as a mark is finished it is
// baked into the notebook as ordinary items tagged by: 'meta' (see the ownership rule in ink.js), so the
// eraser, lasso, undo, save and export treat it like the student's ink. Only previews (answers / ghost
// steps waiting for Accept) and the grey "I read" labels stay on the SVG layer, in `this.live`.
export class MetaInk {
  constructor(svg, nb, { onReveal, onRedo, onCommit, onEditRead, inkColor } = {}) {
    Object.assign(this, { svg, nb, onReveal, onRedo, onCommit, onEditRead, inkColor });
    this.live = []; this.run = 0; this.n = 0; this.byId = new Map();
    svg.setAttribute('viewBox', `0 0 ${PAGE_W} ${PAGE_H}`);
    this.defs = svgEl('defs', {}, svg);
    // Accept / Redo / Discard pills for ink shown as a preview; HTML so they are real buttons.
    this.ui = Object.assign(document.createElement('div'), { className: 'meta-ui' });
    svg.after(this.ui);
  }
  // The colour Meta writes in now (the "Meta's ink" setting; "My pen" = the student's pen colour).
  color() { return this.inkColor?.() || META_COLOR; }
  syncColor() { this.svg.style.setProperty('--meta', this.color()); }

  // Everything Meta has on the page: baked marks grouped from the notebook's items, then live SVG marks.
  get marks() { return [...this.groups(), ...this.live]; }             // oldest first: baked, then in flight
  groups() {
    const by = new Map();
    for (const it of this.nb.items) if (isMeta(it)) { const k = it.grp || it; if (!by.has(k)) by.set(k, []); by.get(k).push(it); }
    return [...by.values()].map(items => ({
      kind: items[0].mk || 'note', rid: items[0].rid || null, items, baked: true, pts: [],
      strokes: items.reduce((n, it) => n + (it.segs?.length ?? (it.type === 'stroke' ? 1 : 0)), 0),
      boxes: items.map(it => it.bbox), box: unionBox(items.map(it => it.bbox)),
      note: items.find(it => it.note)?.note || null, text: items.find(it => it.say)?.say || '', kept: items.some(it => it.kept),
    }));
  }

  // ---- layer management ------------------------------------------------------
  mark(kind) {
    const g = svgEl('g', { class: `mark ${kind}` }, this.svg);
    const m = { g, kind, pts: [], boxes: [], rid: this.pid };      // rid: which reply wrote it
    this.live.push(m);
    return m;
  }
  fadeOut(ms) { for (const m of ms) { m.g.classList.add('fade'); m.t = setTimeout(() => m.g.remove(), 380); } }
  // All of Meta's ink: previews, labels and every by:'meta' item. record: one undo step first.
  clear(record = false) {
    const n = this.live.length + this.nb.metaItems().length;
    if (!n) return 0;
    if (record) this.nb.checkpoint();
    this.run++;
    this.fadeOut(this.live);
    this.live = [];
    this.nb.removeMeta();
    this.syncPills();
    return n;
  }
  // Only the marks of the given kinds, so tidy-ups, graphs and accepted answers survive a new hint.
  // spare: a reply id whose marks stay (a queued command must not wipe the answer just before it).
  clearKinds(kinds, spare = null) {
    this.run++;
    const go = m => kinds.has(m.kind) && !(spare && m.rid === spare);
    this.fadeOut(this.live.filter(go));
    this.live = this.live.filter(m => !go(m));
    this.nb.removeMeta(it => kinds.has(it.mk) && !it.kept && !(spare && it.rid === spare));
    this.syncPills();
  }
  // The eraser on live marks (previews, read labels): stroke by stroke, like ink.
  erase({ x, y }, R) {
    let any = false;
    for (const m of [...this.live]) {
      for (const p of m.g.querySelectorAll('path.ink-stroke')) {
        if (!p._polys?.some(poly => poly.some(([px, py]) => Math.hypot(px - x, py - y) < R + 3))) continue;
        p.remove(); any = true;
      }
      const empty = !m.g.querySelector('path.ink-stroke, text');
      const textHit = m.g.querySelector('text') && m.boxes.some(b => inBox(b, x, y, R / 2));
      if (empty || textHit) { m.g.remove(); this.live.splice(this.live.indexOf(m), 1); any = true; }
    }
    if (any) this.syncPills();
    return any;
  }

  // ---- undo snapshots (see Notebook.ext): the live marks, and which were still previews ----------
  // Path elements the eraser took out of a preview are restored too.
  save() { return this.live.map(m => ({ m, pending: !!m.pending, kids: [...m.g.childNodes] })); }
  load(list = []) {
    this.run++;
    const ms = list.map(e => e.m || e);
    for (const m of this.live) if (!ms.includes(m)) m.g.remove();
    for (const e of list) {
      const m = e.m || e;
      if (e.m) this.setPending(m, e.pending);
      if (e.kids) m.g.replaceChildren(...e.kids);
      clearTimeout(m.t); m.g.classList.remove('fade'); this.svg.append(m.g);
    }
    this.live = [...ms];
    this.syncPills();
  }

  // ---- persistence: live marks as plain JSON (finished SVG markup, point lists, hit boxes) ----------
  serialize() {
    return this.live.map(m => ({
      kind: m.kind, html: cleanMarkup(m.g), boxes: m.boxes, pts: m.pts.map(([x, y]) => [Math.round(x), Math.round(y)]), note: m.note || null,
      polys: [...m.g.querySelectorAll('path.ink-stroke')].map(p => (p._polys || []).map(poly => poly.map(([x, y]) => [r1(x), r1(y)]))),
      color: this.color(),
      ...(m.pending ? { pending: true, pid: m.pid, ask: m.ask || null } : {}),
    }));
  }
  // Stored page.meta -> what stays live (previews, read labels) and items for finished marks. Older
  // pages kept every mark as SVG markup: those become one by:'meta' 'mark' item each (erasable,
  // movable), so old notebooks behave like new ones.
  split(data = []) {
    const live = [], items = [];
    (data || []).forEach((d, i) => {
      if (d.pending || d.kind === 'read') { live.push(d); return; }
      if (!d.html) return;
      const bs = [...(d.boxes || []), ...(d.pts?.length > 1 ? [ptsBox(d.pts)] : [])];
      if (!bs.length) return;
      const it = { type: 'mark', by: 'meta', mk: d.kind, grp: `old${Date.now().toString(36)}${i}`, html: d.html, box0: unionBox(bs), x: 0, y: 0, color: d.color || META_COLOR, polys: polysFromHtml(d.html) };
      const g = parseMark(d.html), say = markText(g);
      if (say) it.say = say;
      if (d.note) it.note = d.note;
      items.push(it);
    });
    return { live, items };
  }
  // Replace the layer with stored live marks (switching pages, reload). Drops everything in flight.
  restore(data = []) {
    this.run++;
    for (const m of this.live) { clearTimeout(m.t); m.g.remove(); }
    this.svg.querySelectorAll('g.mark').forEach(g => g.remove());
    this.defs.replaceChildren();
    this.live = [];
    this.syncColor();
    for (const d of data || []) {
      const m = this.mark(d.kind);
      m.g.innerHTML = d.html;
      m.boxes = d.boxes || []; m.pts = d.pts || []; m.note = d.note || null;
      const paths = m.g.querySelectorAll('path.ink-stroke');
      paths.forEach((p, i) => { p._polys = d.polys?.[i] || polysFromHtml(`<path class="ink-stroke" d="${p.getAttribute('d')}"/>`); });
      if (d.pending) { m.pid = d.pid; m.ask = d.ask; this.setPending(m, true); }
    }
    this.syncPills();
  }

  // ---- baking: a finished mark becomes notebook items -------------------------------------
  // A mark of plain pen strokes (words, maths, ticks, circles, arrows, plain graphs) becomes ONE 'ink'
  // item holding every pen stroke as a seg, from the engine's point lists (the eraser still takes them
  // one by one). Anything else (shaded or dashed graphs, the font fallback) becomes one 'mark' item
  // holding its markup. Not undoable by itself: callers checkpoint.
  bake(m, extra = {}) {
    const color = this.color(), grp = `g${Date.now().toString(36)}${++this.n}`;
    const paths = [...m.g.querySelectorAll('path.ink-stroke')];
    const plain = paths.length && !m.g.querySelector('text, path.shade, [stroke-dasharray]') && paths.every(p => p._polys?.length);
    const base = { by: 'meta', mk: m.kind, grp, ...(m.rid ? { rid: m.rid } : {}), ...extra };
    let items;
    if (plain) {
      const segs = paths.flatMap(p => {
        const op = p.getAttribute('opacity'), w = +p.getAttribute('stroke-width') || 2.4;
        return p._polys.filter(poly => poly.length).map(poly => ({ p: poly.map(([x, y]) => [x, y]), w, ...(op != null ? { o: +op } : {}) }));
      });
      items = segs.length ? [{ type: 'ink', ...base, color, segs }] : [];
    } else {
      const html = cleanMarkup(m.g);
      const bs = [...m.boxes, ...(m.pts.length > 1 ? [ptsBox(m.pts)] : [])];
      if (!bs.length) { m.g.remove(); this.live = this.live.filter(x => x !== m); return []; }
      const polys = paths.flatMap(p => p._polys || []).map(poly => poly.map(([x, y]) => [r1(x), r1(y)]));
      items = [{ type: 'mark', ...base, html, box0: unionBox(bs), x: 0, y: 0, color, polys: polys.length ? polys : polysFromHtml(html) }];
    }
    if (!items.length) { m.g.remove(); this.live = this.live.filter(x => x !== m); return []; }
    const say = markText(m.g);
    if (say) items[0].say = say;
    if (m.note) items[0].note = m.note;
    this.nb.items.push(...items.map(prepareItem));
    this.live = this.live.filter(x => x !== m);
    clearTimeout(m.t); m.g.remove();
    this.nb.redraw();
    return items;
  }
  // Recolours Meta's ink on this page (the colour setting changed): only items still in `from`
  // (or all of them when `from` is null), so a mark the student recoloured keeps its colour.
  recolor(from, to) {
    const its = this.nb.metaItems().filter(it => from == null || it.color === from);
    if (its.length) { this.nb.checkpoint(); for (const it of its) it.color = to; this.nb.redraw(); }
    this.syncColor();
    return its.length;
  }

  // Everything Meta has drawn on the SVG layer; baked marks are in nb.items, which nb.occupied covers.
  metaBoxes() { return this.live.flatMap(m => m.pts.length > 1 ? [...m.boxes, ptsBox(m.pts)] : m.boxes); }
  isFree(box) { return !this.nb.occupied(box) && !this.metaBoxes().some(b => overlap(b, box)); }
  freeRow(r, x, w, n = 1) {
    for (let k = r; k < r + 30 && rowY(k + n - 1) < PAGE_H - 10; k++) {
      let ok = true;
      for (let j = 0; j < n && ok; j++) ok = this.isFree([x, rowY(k + j) - RULE + 2, w, RULE - 4]);   // whole row: "I read" labels sit high in it
      if (ok) return k;
    }
    return r;
  }

  // ---- previews: Accept / Redo / Discard (tap the faint ink itself to accept) -------------------
  setPending(m, on) {
    m.pending = !!on;
    m.g.classList.toggle('pending', !!on);
    m.g.classList.toggle('tap-target', !!on);
    m.g.querySelector(':scope > rect.hit')?.remove();
    if (!m.tap) {
      // A finger or mouse tap on the faint ink accepts it. The Pencil writes through it: a stroke that
      // starts here dismisses the preview and is kept, a Pencil tap accepts (see app.js beforeStroke).
      m.tap = e => { if (!m.pending || e.pointerType === 'pen') return; e.stopPropagation(); this.decide(m.pid, 'accept'); };
      m.g.addEventListener('click', m.tap);
      m.g.addEventListener('pointerdown', e => { if (m.pending && e.pointerType !== 'pen') e.stopPropagation(); });
    }
  }
  pending() { return this.live.filter(m => m.pending); }
  syncPills() {
    this.ui.replaceChildren();
    const groups = new Map();
    for (const m of this.pending()) {
      if (m.boxes.length && !m.g.querySelector(':scope > rect.hit')) {      // tap target over the faint ink
        const b = unionBox(m.boxes);
        m.g.prepend(svgEl('rect', { class: 'hit', x: b[0] - 4, y: b[1] - 4, width: b[2] + 8, height: b[3] + 8, rx: 6 }));
      }
      if (!groups.has(m.pid)) groups.set(m.pid, []); groups.get(m.pid).push(m); }
    for (const [pid, ms] of groups) {
      if (pid === this.redoing) continue;
      const boxes = ms.flatMap(m => m.boxes);
      if (!boxes.length) continue;
      const last = boxes.reduce((a, b) => (b[1] + b[3] > a[1] + a[3] ? b : a));
      const coarse = matchMedia('(pointer: coarse)').matches;
      const k = 1 / Math.max(0.3, this.nb.scale || 1), W = (coarse ? 280 : 196) * k, H = coarse ? 22 : 13;
      let x = last[0] + last[2] + 14, y = last[1] + last[3] / 2 - H * k;
      if (x + W > PAGE_W - 8) { x = clamp(Math.min(...boxes.map(b => b[0])), 8, PAGE_W - W - 8); y = Math.max(...boxes.map(b => b[1] + b[3])) + 6; }
      const bar = Object.assign(document.createElement('div'), { className: 'meta-pills' });
      bar.dataset.pid = pid;
      Object.assign(bar.style, { left: `${x}px`, top: `${y}px`, transform: `scale(${k})` });
      bar.addEventListener('pointerdown', e => e.stopPropagation());
      for (const [act, label, title] of [['accept', 'Accept', 'Keep this ink (or tap the faint ink)'], ['redo', 'Redo', 'Ask Meta again'], ['discard', 'Discard', 'Remove this ink']]) {
        const b = Object.assign(document.createElement('button'), { type: 'button', textContent: label, title });
        b.dataset.act = act;
        b.onclick = e => { e.stopPropagation(); this.decide(pid, act); };
        bar.append(b);
      }
      this.ui.append(bar);
    }
  }
  decide(pid, act) {
    const ms = this.live.filter(m => m.pending && m.pid === pid);
    if (!ms.length) return;
    if (act === 'redo') {                              // pills hide while Meta writes it again
      this.redoing = pid; this.syncPills();
      Promise.resolve(this.onRedo?.(ms[0].ask || null)).finally(() => { this.redoing = null; this.syncPills(); });
      return;
    }
    this.nb.checkpoint();                              // Accept and Discard are undoable
    if (act === 'accept') for (const m of ms) this.bake(m, { kept: true });     // real ink now; a new hint won't replace it
    else {
      this.fadeOut(ms);
      this.live = this.live.filter(m => !ms.includes(m));
    }
    this.syncPills();
    this.onCommit?.(act);
  }
  // The preview under a page point (its marks' boxes, a little padded).
  previewAt({ x, y }) { return this.pending().find(m => m.pid !== this.redoing && m.boxes.some(b => inBox(b, x, y, 6))) || null; }
  // Dismiss a preview without an undo step of its own (the stroke written over it already took one).
  dismiss(pid) {
    const ms = this.live.filter(m => m.pending && m.pid === pid);
    if (!ms.length) return false;
    this.fadeOut(ms);
    this.live = this.live.filter(m => !ms.includes(m));
    this.syncPills();
    this.onCommit?.('discard');
    return true;
  }
  // Tap while Meta writes: finish everything still being written at once.
  skip() { if (this.writing) this.fast = true; }

  // Enter with a preview showing: accept the newest one.
  acceptLatest() { const m = this.pending().at(-1); if (!m) return false; this.decide(m.pid, 'accept'); return true; }

  // ---- primitives --------------------------------------------------------------
  tween(ms, fn, token) {
    if (this.fast) { fn(1); return Promise.resolve(); }
    return new Promise(res => {
      const t0 = performance.now();
      const step = now => {
        const t = clamp((now - t0) / ms, 0, 1);            // rAF time can be a little before t0
        fn(t);
        if (t < 1 && token === this.run && !this.fast) requestAnimationFrame(step); else { fn(1); res(); }
      };
      requestAnimationFrame(step);
    });
  }
  // polys: the pen strokes in `d` as point lists (drawn through midpoints, as ink.js draws them).
  async stroke(m, pts, d, ms, token, width = 2.4, attrs = {}, polys = [pts]) {
    const p = svgEl('path', { d, class: 'ink-stroke', 'stroke-width': width, ...attrs }, m.g);
    p._polys = polys;
    m.pts.push(...pts);
    const len = p.getTotalLength() + 1;
    p.style.strokeDasharray = `${len} ${len}`;
    await this.tween(ms, t => { p.style.strokeDashoffset = len * (1 - ease(t)); }, token);
  }

  // Pen strokes for a box from the handwriting engine (em units), written at (x, base) with
  // 1 em = `em` px, stroke by stroke at a person's pace, at most `maxMs` for the whole box.
  async hand(m, box, x, base, em, token, { label = '', attrs = {}, maxMs = 2500, speed = 1, instant = false, reveal = true, width } = {}) {
    const g = svgEl('g', { class: 'hw', 'data-text': label }, m.g);
    const w = (width ?? penWidth(em)).toFixed(2);
    const els = box.paths.map(p => {
      const e = svgEl('path', { d: pathD(p, em, x, base), class: 'ink-stroke', 'stroke-width': w, ...attrs }, g);
      const pts = [];
      for (let i = 0; i + 1 < p.length; i += 2) pts.push([x + p[i] * em, base + p[i + 1] * em]);
      e._polys = [pts];
      return e;
    });
    const rect = [x - 4, base - box.h * em - 3, box.w * em + 8, (box.h + box.d) * em + 6];
    m.boxes.push(rect);
    if (reveal) this.onReveal?.(rect);
    if (instant || this.fast || !els.length || token !== this.run) return rect;
    const { times, end } = penTimes(box.paths.map(p => p.map((v, i) => (i % 2 ? base : x) + v * em)), { speed });
    const total = Math.min(end, maxMs), k = end / total;
    const lens = els.map(e => e.getTotalLength() + 0.5), done = els.map(() => -1);
    els.forEach((e, i) => { e.style.strokeDasharray = `${lens[i]} ${lens[i]}`; e.style.strokeDashoffset = lens[i]; });
    const nib = svgEl('circle', { r: 1.8, class: 'nib', cx: -9, cy: -9 }, m.g);
    await this.tween(total, t => {
      const now = t * total * k;
      for (let i = 0; i < els.length; i++) {
        const { at, dur } = times[i];
        const f = now >= at + dur ? 1 : now <= at ? 0 : (now - at) / dur;
        if (f === done[i]) continue;
        done[i] = f;
        els[i].style.strokeDashoffset = lens[i] * (1 - f);
        if (f > 0 && f < 1) { const q = els[i].getPointAtLength(lens[i] * f); nib.setAttribute('cx', q.x); nib.setAttribute('cy', q.y); }
      }
    }, token);
    els.forEach(e => e.removeAttribute('style'));
    nib.remove();
    return rect;
  }
  // Builds a handwriting box; on any engine error returns null so the caller falls back to Caveat.
  box_(fn) {
    const good = b => b && Array.isArray(b.paths) && Number.isFinite(b.w);
    try { const b = fn(); return Array.isArray(b) ? (b.length && b.every(good) ? b : null) : good(b) ? b : null; }
    catch (err) { console.warn('[margin] handwriting engine failed, using the font', err); return null; }
  }
  // One line of ink: engine strokes, or the Caveat font if the engine could not lay it out.
  async inkLine(m, box, text, x, base, em, token, opts = {}) {
    if (box) return this.hand(m, box, x, base, em, token, { label: text, ...opts });
    return this.write(m, text, x, base, Math.round(em * 0.9), token);
  }
  // Text revealed left to right through a growing clip rect, with a moving nib (Caveat fallback).
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
    return box;
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
    const d = `${smoothPath(pts)} M${h1[0]} ${h1[1]} L${to[0]} ${to[1]} L${h2[0]} ${h2[1]}`;
    return { d, pts, polys: [pts, [h1, to, h2]] };
  }

  // ---- annotations -------------------------------------------------------------
  box(id) { const b = this.byId.get(id)?.bbox; return Array.isArray(b) && b.length === 4 ? b : null; }
  lastBox() { return [...this.byId.values()].map(l => l.bbox).filter(b => b?.length === 4).sort((a, b) => (a[1] + a[3]) - (b[1] + b[3])).at(-1) || null; }
  // Meta writes about as big as the student, times the "Meta's writing size" setting (S 0.7, M 0.85, L 1).
  emFor(b) { const k = this.inkScale || 0.85; return Math.max(14, emFor(b?.[3] ?? 32) * k); }

  // Rows a box needs and where its baseline goes when it starts on ruled row r.
  rowsFor(box, em) {
    const h = box.h * em, d = box.d * em;
    if (h <= RULE - 4 && d <= 16) return { n: 1, base: r => baselineOf(r) };
    const n = Math.max(1, Math.ceil((h + d + 6) / RULE));
    return { n, base: r => rowY(r) - RULE + 2 + (n * RULE - (h + d)) / 2 + h };
  }

  // spare: keep the previous reply's marks (set for a command that was queued behind it).
  async render(resp, sentLines, ask = null, { spare = false } = {}) {
    // Typeset and graph ink stack with hints; each kind of answer replaces only its own kind.
    // The first-run tip stays until the student erases it.
    const EXTRAS = new Set(['typeset', 'graph', 'graph3d', 'picture', 'tip']);
    const kinds = new Set((resp.annotations || []).map(a => a.kind));
    const extrasOnly = kinds.size > 0 && [...kinds].every(k => EXTRAS.has(k));
    const keep = spare ? this.pid : null;
    if (extrasOnly) this.clearKinds(kinds, keep);
    else this.clearKinds(new Set(this.marks.map(m => m.kind).filter(k => !EXTRAS.has(k))), keep);
    this.nb.redraw(); this.syncColor();
    this.writing = true;
    this.fast = matchMedia('(prefers-reduced-motion: reduce)').matches;     // reduced motion: ink appears at once
    const token = this.run;
    this.byId = new Map(sentLines.map(l => [l.id, l]));
    for (const l of resp.lines || []) this.byId.set(l.id, { ...this.byId.get(l.id), ...l, bbox: l.bbox?.length === 4 ? l.bbox : this.byId.get(l.id)?.bbox });
    this.writeRow = null;
    this.ask = ask; this.pid = `p${Date.now().toString(36)}${++this.n}`;
    this.tsAnns = (resp.annotations || []).filter(a => a.kind === 'typeset' && (a.text || a.latex) && this.box(a.target));
    this.tsPlan = null;                               // laid out when the first one is drawn (after the notes)

    // Show what Meta read for lines it read from handwriting, so misreads are easy to spot.
    const sentText = new Map(sentLines.map(l => [l.id, l.text]));
    for (const l of resp.lines || []) if (l.text && this.box(l.id) && sentText.get(l.id) !== l.text) this.readLabel(l);

    const anns = [...(resp.annotations || [])].map(a => (a.kind === 'explain' ? { ...a, kind: 'note' } : a));
    const ticked = new Set(anns.filter(a => a.kind === 'tick').map(a => a.target));
    // A tidy-up or a graph is not a marking: ticking only the right lines would hide the wrong one.
    if (!extrasOnly) for (const l of resp.lines || []) if (l.status === 'ok' && !ticked.has(l.id)) anns.push({ kind: 'tick', target: l.id });
    const order = { tick: 0, circle: 1, underline: 2, note: 3, ghost: 4, write: 5, typeset: 6, graph: 7, graph3d: 8, picture: 9 };
    const todo = anns.filter(a => a.kind in order).sort((a, b) => order[a.kind] - order[b.kind]);
    if (!todo.length && resp.verdict) todo.push({ kind: 'note', target: null, text: resp.verdict });
    const pid = this.pid;
    try {
      for (const a of todo) {
        if (token !== this.run) return todo.length;
        const before = new Set(this.live);
        try { await this[`a_${a.kind}`](a, token); } catch (err) { console.warn('[margin] skipped annotation', a, err); }
        // Meta lifted the pen: what it just wrote becomes notebook ink (previews wait for Accept).
        if (token === this.run) for (const m of this.live.filter(m => !before.has(m) && !m.pending && m.kind !== 'read')) this.bake(m);
        if (!this.fast) await sleep(a.kind === 'tick' ? 40 : 140);
      }
      if (token === this.run) this.syncPills();
      return todo.length;
    } finally {
      if (this.pid === pid) { this.writing = false; this.fast = false; }   // never left "writing" (it gates the queue)
    }
  }

  // First-run tip: two short lines in Meta's hand, right-aligned in the space between the page header
  // and the first rule (a new page is empty there), as ordinary erasable by:'meta' ink. Kept, so a reply
  // never replaces it; never sent to Meta (by:'meta' ink is left out of what Meta reads).
  async writeTip(lines = ['Tap the mic or press / to ask me', 'scribble to erase · hold to move']) {
    const em = 17, right = PAGE_W - 40, bases = [rowY(0) - 40, rowY(0) - 15];
    const boxes = lines.map(t => this.box_(() => textBox(t)));
    if (boxes.some(b => !b)) return false;
    const w = Math.max(...boxes.map(b => b.w * em));
    const area = [right - w - 6, bases[0] - em, w + 12, bases[1] - bases[0] + em * 1.4];
    if (!this.isFree(area)) return false;
    this.syncColor();
    this.pid = `tip${Date.now().toString(36)}`;
    const m = this.mark('tip'), token = this.run;
    m.g.setAttribute('aria-label', lines.join(' · '));
    this.writing = true;
    try {
      for (let i = 0; i < lines.length; i++) await this.hand(m, boxes[i], right - boxes[i].w * em, bases[i], em, token, { label: lines[i], maxMs: 1400, reveal: false, width: 1.5 });
      if (!this.live.includes(m)) return false;
      if (token !== this.run) { m.g.remove(); this.live = this.live.filter(x => x !== m); return false; }
      const [it] = this.bake(m, { kept: true });
      if (it) it.say = lines.join(' · ');
      return !!it;
    } finally { this.writing = false; this.fast = false; }
  }

  readLabel(l) {
    const b = this.box(l.id), m = this.mark('read');
    const x = b[0], base = b[1] + b[3] + 17, em = 15;
    const bx = this.box_(() => mathBox(`\\text{I read: }${toLatex(l.text)}`));
    if (bx) {
      this.hand(m, bx, x, base, em, this.run, { label: `I read: ${l.text}`, instant: true, reveal: false, width: 1.1 });
    } else {
      const t = svgEl('text', { x, y: base - 1, class: 'read-label' }, m.g);
      t.textContent = `I read: ${l.text}`;
      m.boxes.push([x, b[1] + b[3] + 7, Math.min(PAGE_W - x, 6 * (l.text.length + 8)), 12]);
    }
    if (this.onEditRead && m.boxes.length) {
      const rb = unionBox(m.boxes);
      m.g.classList.add('tap-target', 'read-edit');
      m.g.setAttribute('role', 'button');
      m.g.setAttribute('tabindex', '0');
      m.g.setAttribute('title', `Meta read “${l.text}” — click to fix if misread`);
      m.g.setAttribute('aria-label', `Meta read ${l.text}. Click to edit.`);
      m.g.prepend(svgEl('rect', { class: 'hit', x: rb[0] - 4, y: rb[1] - 3, width: rb[2] + 8, height: rb[3] + 6, rx: 5 }));
      const open = e => { if (e.pointerType === 'pen') return; e.stopPropagation(); this.onEditRead(l, this.ask); };
      m.g.addEventListener('click', open);
      m.g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(e); } });
    }
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

  // A note's lines as engine boxes (em), wrapped to `maxPx`. Maths inside the text is laid
  // out as maths; text may hold several lines ("\n", why notes), and `latex` + `latex_line`
  // replaces that line with the laid-out formula (no `latex_line`: the formula is added below).
  noteLines(a, em, maxPx) {
    const seed = newSeed();
    const parts = String(a.text || '').split('\n').filter(t => t.trim());
    const li = Number.isInteger(a.latex_line) && a.latex_line >= 0 && a.latex_line < parts.length ? a.latex_line : -1;
    const out = [];
    for (let i = 0; i < parts.length; i++) {
      let got = null;
      if (i === li && a.latex) {
        const mb = this.box_(() => mathBox(a.latex, seed + i));
        if (mb) got = [mb];
      }
      got ??= this.box_(() => paragraph(parts[i], maxPx / em, seed + i));
      if (!got) return null;
      out.push(...got);
    }
    if (a.latex && li < 0 && !/\$/.test(a.text)) { const mb = this.box_(() => mathBox(a.latex, seed + 99)); if (mb) out.push(mb); }
    return out;
  }

  async a_note(a, token) {
    if (!a.text) return;
    const b = this.box(a.target) || this.lastBox();
    const m = this.mark('note');
    m.g.setAttribute('aria-label', a.text);
    let em, px, lead;
    const setEm = e => { em = e; px = Math.round(e * 0.9); lead = e * 1.2; };
    setEm(this.emFor(b));
    const fontLines = maxPx => String(a.text).split('\n').flatMap(t => wrap(t, px, maxPx)).map(t => ({ t, box: null, w: measure(t, px) }));
    const linesFor = maxPx => this.noteLines(a, em, maxPx)?.map(bx => ({ t: '', box: bx, w: bx.w * em })) || fontLines(maxPx);
    const writeAll = async (lines, x, bases) => {
      const budget = 2300 / lines.length;
      for (let i = 0; i < lines.length && token === this.run; i++) {
        const L = lines[i], text = L.box ? (i === 0 ? a.text : '') : L.t;
        if (L.box) await this.hand(m, L.box, x, bases[i], em, token, { label: text, maxMs: budget });
        else await this.write(m, L.t, x, bases[i], px, token);
      }
    };
    if (!b) {                                         // nothing on the page to point at
      const x = MARGIN_X + 40, lines = linesFor(PAGE_W - 28 - x);
      const r = this.freeRow(1, x, Math.max(...lines.map(l => l.w)), lines.length);
      return writeAll(lines, x, lines.map((_, i) => baselineOf(r + i)));
    }
    const right = b[0] + b[2] + 60, avail = PAGE_W - 28 - right;
    let lines, x, bases, from, to, ctrl;
    // Beside the line when it fits; if not, try smaller writing (down to 3/4) before going below.
    let rightLines = [], fitsRight = false;
    const em0 = em;
    for (const k of [1, 0.85, 0.75]) {
      setEm(Math.max(13, em0 * k));
      rightLines = avail > 60 ? linesFor(avail) : [];
      const rightW = rightLines.length ? Math.max(...rightLines.map(l => l.w)) : 0;
      const rightH = rightLines.reduce((h, l) => h + (l.box ? Math.max(lead, (l.box.h + l.box.d + 0.25) * em) : lead), 0);
      const rightFree = rightLines.length && this.isFree([right - 30, b[1] - 4, rightW + 30, rightH + 8]);
      const full = rightLines.reduce((s, l) => s + l.w, 0);
      fitsRight = a.side !== 'below' && avail >= Math.min(full, 220) && rightFree && rightH <= Math.max(b[3] * 2.2, lead * 3.2);
      if (fitsRight || a.side === 'below') break;
    }
    if (!fitsRight) setEm(em0);
    if (fitsRight) {
      lines = rightLines; x = right;
      const base = b[1] + b[3] * 0.75;
      bases = [base];
      for (let i = 1; i < lines.length; i++) {
        const p = lines[i - 1].box, q = lines[i].box;
        bases.push(bases[i - 1] + (p && q ? Math.max(lead, (p.d + q.h + 0.25) * em) : lead));
      }
      from = [x - 10, base - em * 0.3]; to = [b[0] + b[2] + 22, b[1] + b[3] * 0.45]; ctrl = [(from[0] + to[0]) / 2, Math.min(from[1], to[1]) - 18];
    } else {
      x = clamp(b[0] + 30, MARGIN_X + 20, PAGE_W - 300);
      lines = linesFor(PAGE_W - 28 - x);
      const fits = lines.map(l => l.box ? this.rowsFor(l.box, em) : { n: 1, base: baselineOf });
      const r = this.freeRow(rowAt(b[1] + b[3]) + 1, x - 30, Math.max(...lines.map(l => l.w)) + 30, fits.reduce((n, f) => n + f.n, 0));
      let k = r;
      bases = fits.map(f => { const y = f.base(k); k += f.n; return y; });
      from = [x - 12, bases[0] - em * 0.35]; to = [b[0] - 8, b[1] + b[3] * 0.6]; ctrl = [b[0] - 34, from[1] - 6];   // curl up the left edge
    }
    const arr = this.arrow(from, to, ctrl);
    await this.stroke(m, arr.pts, arr.d, 280, token, 2.2, {}, arr.polys);
    await writeAll(lines, x, bases);
    m.note = { kind: 'note', target: a.target ?? null, text: a.text };
  }

  // Lines of a write/ghost: LaTeX rows when the server sent `latex`, else the text's own lines.
  writeParts(a) {
    const texts = String(a.text || '').split('\n').filter(s => s.trim());
    if (a.latex) {
      let rows = [];
      try { rows = splitRows(a.latex); } catch { rows = [a.latex]; }
      return rows.map((latex, i) => ({ text: rows.length === texts.length ? texts[i] : (i === 0 ? a.text || latex : ''), latex }));
    }
    return texts.map(text => ({ text, latex: isMath(text) ? toLatex(text) : null }));
  }

  // Answer steps / ghost of the next line: below `after`, each line on its own free row(s).
  // They appear as a faint preview with Accept / Redo / Discard until the student decides.
  async a_write(a, token, ghost = false) {
    if (!a.text && !a.latex) return;
    const after = this.box(a.after) || this.lastBox();
    const x = after ? Math.max(MARGIN_X + 14, after[0]) : MARGIN_X + 40, em0 = this.emFor(after);
    const maxW = PAGE_W - 28 - x;
    let row = this.writeRow != null ? this.writeRow + 1 : after ? rowAt(after[1] + after[3]) + 1 : 1;
    for (const part of this.writeParts(a)) {
      // One engine box per line: maths laid out as maths; long sentences wrap.
      let boxes = null, em = em0;
      if (part.latex) {
        const mb = this.box_(() => mathBox(part.latex));
        if (mb) { em = Math.max(em0 * 0.6, Math.min(em0, maxW / Math.max(mb.w, 0.1))); boxes = [mb]; }
      }
      if (!boxes && part.text) boxes = this.box_(() => paragraph(part.text, maxW / em0));
      const items = boxes ? boxes.map((bx, i) => ({ bx, text: i === 0 ? part.text : '' })) : wrap(part.text, Math.round(em0 * 0.9), maxW).map(t => ({ bx: null, text: t }));
      for (const it of items) {
        if (token !== this.run) return;
        const fit = it.bx ? this.rowsFor(it.bx, em) : { n: 1, base: r => baselineOf(r) };
        const w = it.bx ? it.bx.w * em : measure(it.text, Math.round(em * 0.9));
        row = this.freeRow(row, x, w, fit.n);
        this.writeRow = row + fit.n - 1;
        const m = this.mark(ghost ? 'ghost' : 'write');
        m.pid = this.pid; m.ask = this.ask; this.setPending(m, true);
        await this.inkLine(m, it.bx, it.text, x, fit.base(row), em, token);
        row += fit.n;
      }
    }
  }
  a_ghost(a, token) { return this.a_write(a, token, true); }

  // ---- typeset: the neat copy of each line, its maths laid out properly (fractions, limits) ----
  // One column to the right of everything already on those lines (student ink, notes, circles)
  // when it fits; otherwise a "neat copy" block in the first free rows below the student's work.
  // `text` keeps the student's own order (SymPy's `latex` may reorder terms), so it is preferred.
  planTypeset() {
    const items = this.tsAnns.map(a => {
      const b = this.box(a.target);
      const bx = this.box_(() => mathBox(a.text ? toLatex(a.text) : a.latex)) || (a.latex ? this.box_(() => mathBox(a.latex)) : null);
      const k = this.inkScale || 0.85;
      let em = Math.round(clamp(b[3] * 0.95, 22, 32) * Math.min(1, k + 0.1));
      if (bx) em = Math.min(em, (RULE + 8) / Math.max(0.5, bx.h + bx.d));
      const px = Math.round(clamp(b[3] * 0.78, 20, 27));
      return { a, b, bx, em, px, w: bx ? bx.w * em : measureMath(a.text || '', px), h: bx ? bx.h * em : px * 0.85 };
    });
    const plan = new Map();
    if (!items.length) return plan;
    const taken = [...this.metaBoxes(), ...this.nb.items.filter(it => !it.bg).flatMap(it => it.rowBoxes || [it.bbox])];
    const bandRight = b => Math.max(b[0] + b[2], ...taken.filter(r => r[1] < b[1] + b[3] + 2 && b[1] - 2 < r[1] + r[3] && r[0] < PAGE_W - 40).map(r => r[0] + r[2]));
    const colX = Math.max(...items.map(it => bandRight(it.b))) + 30;
    const boxAt = (it, x, base) => [x - 4, base - it.h, it.w + 8, it.h + 8];
    if (items.every(it => colX + it.w <= PAGE_W - 24 && this.isFree(boxAt(it, colX, it.b[1] + it.b[3] * 0.78)))) {
      for (const it of items) plan.set(it.a, { ...it, x: colX, base: it.b[1] + it.b[3] * 0.78 });
      return plan;
    }
    const x = clamp(Math.min(...items.map(it => it.b[0])), MARGIN_X + 14, PAGE_W - 24 - Math.max(...items.map(it => it.w)));
    const low = Math.max(...this.nb.lines().map(l => l.bbox[1] + l.bbox[3]), ...items.map(it => it.b[1] + it.b[3]));
    const r = this.freeRow(rowAt(low) + 1, x - 4, Math.max(160, ...items.map(it => it.w)) + 8, items.length + 1);
    plan.label = { x, base: baselineOf(r) - 6 };
    items.forEach((it, i) => plan.set(it.a, { ...it, x, base: baselineOf(r + 1 + i) }));
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
      const lb = this.box_(() => paragraph('neat copy', 100)[0]);
      const w = lb ? lb.w * 19 : measure('neat copy', 18);
      if (lb) await this.hand(lm, lb, x, base, 19, token, { label: 'neat copy', attrs: { opacity: 0.75 }, maxMs: 700, width: 1.4 });
      else { const t = svgEl('text', { x, y: base, 'font-size': 18, opacity: 0.75 }, lm.g); t.textContent = 'neat copy'; lm.boxes.push([x - 4, base - 16, w + 8, 24]); }
      const u = [[x, base + 6], [x + w * 0.5, base + 7.5], [x + w, base + 5.5]];
      await this.stroke(lm, u, smoothPath(u), 200, token, 1.4, { opacity: 0.5 });
    }
    const { x, base, px, w, bx, em } = p;
    const m = this.mark('typeset');
    if (bx) { await this.hand(m, bx, x, base, em, token, { label: a.text || a.latex, maxMs: Math.min(1100, 2600 / this.tsAnns.length), speed: 1.4 }); return; }
    // Fallback: the serif font, revealed left to right.
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
    const headPts = (tip, dir) => dir === 'x' ? [[tip[0] - 7, tip[1] - 4], tip, [tip[0] - 7, tip[1] + 4]] : [[tip[0] - 4, tip[1] + 7], tip, [tip[0] + 4, tip[1] + 7]];
    const head = (tip, dir) => { const [a, b, c] = headPts(tip, dir); return ` M${a[0]} ${a[1]} L${b[0]} ${b[1]} L${c[0]} ${c[1]}`; };
    await this.stroke(m, xAxis, smoothPath(xAxis) + head(xAxis.at(-1), 'x'), 260, token, 1.5, { opacity: 0.55 }, [xAxis, headPts(xAxis.at(-1), 'x')]);
    await this.stroke(m, yAxis, smoothPath(yAxis) + head(yAxis.at(-1), 'y'), 220, token, 1.5, { opacity: 0.55 }, [yAxis, headPts(yAxis.at(-1), 'y')]);
    for (const [t, x, y] of [['x', R + 2, ax + 16], ['y', ay + 7, T - 2]]) {
      const lb = this.box_(() => textBox(t));
      if (lb) await this.hand(m, lb, x, y, 20, token, { label: t, attrs: { opacity: 0.7 }, maxMs: 100, reveal: false, width: 1.4 });
      else { const lt = svgEl('text', { x, y, 'font-size': 16, opacity: 0.7 }, m.g); lt.textContent = t; }
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
      await this.legend(m, `y = ${a.fn}`, cut(`y = ${a.fn}`, two ? 30 : 34), tx, gy + 16, gx + W - 4 - tx, token);
      if (two) {
        const sw = [[gx + 4, gy + 31], [gx + 24, gy + 31]];
        svgEl('path', { d: smoothPath(sw), class: 'ink-stroke', 'stroke-width': 1.7, 'stroke-dasharray': '6 5', opacity: 0.75 }, m.g);
        const ol = a.overlay_fn ? `y = ${a.overlay_fn}` : 'overlay';
        await this.legend(m, ol, cut(ol, 30), tx, gy + 36, gx + W - 4 - tx, token);
      }
    }
  }
  // ---- 3D surface z = f(x, y): a hand-sketched wireframe in an oblique view, as erasable ink ----
  async a_graph3d(a, token) {
    const xs = a.xs || [], ys = a.ys || [], Z = a.z || [];
    const [z0, z1] = a.zrange?.length === 2 ? a.zrange : [0, 1];
    if (xs.length < 2 || ys.length < 2 || Z.length !== ys.length || !(z1 > z0)) return;
    const W = 300, H = 270;
    const anchor = this.box(a.after) || this.lastBox();
    const [gx, gy] = this.graphSpot(anchor, W, H);
    const m = this.mark('graph3d');
    m.boxes.push([gx, gy, W, H]);
    this.onReveal?.([gx, gy, W, H]);
    // u, v, w in [-1, 1]; screen: u to the right and down, v to the left and down, w up
    const A = 62, B = 26, HZ = 58, cx = gx + W / 2, cy = gy + 40 + 2 * B + HZ;
    const nu = x => -1 + 2 * (x - xs[0]) / (xs.at(-1) - xs[0]), nv = y => -1 + 2 * (y - ys[0]) / (ys.at(-1) - ys[0]);
    const nw = z => -1 + 2 * (z - z0) / (z1 - z0);
    const P = (u, v, w) => [cx + (u - v) * A, cy + (u + v) * B - w * HZ];
    // axes from the back corner: x, y along the floor, z up
    const axes = [[[-1, -1, -1], [1.25, -1, -1], 'x'], [[-1, -1, -1], [-1, 1.25, -1], 'y'], [[-1, -1, -1], [-1, -1, 1.3], 'z']];
    for (const [p0, p1, t] of axes) {
      if (token !== this.run) return;
      const pa = P(...p0), pb = P(...p1), seg = [pa, pb];
      await this.stroke(m, seg, smoothPath(seg), 160, token, 1.3, { opacity: 0.5 });
      const lb = this.box_(() => textBox(t));
      const [lx, ly] = t === 'z' ? [pb[0] - 6, pb[1] - 4] : t === 'x' ? [pb[0] + 3, pb[1] + 14] : [pb[0] - 16, pb[1] + 14];
      if (lb) await this.hand(m, lb, lx, ly, 18, token, { label: t, attrs: { opacity: 0.65 }, maxMs: 90, reveal: false, width: 1.3 });
    }
    // grid lines: rows (constant y) then columns (constant x), each broken where the surface is undefined
    const lines = [];
    const run = cells => { let cur = []; for (const c of cells) { if (c) cur.push(c); else { if (cur.length > 1) lines.push(cur); cur = []; } } if (cur.length > 1) lines.push(cur); };
    const step = Math.max(1, Math.round(xs.length / 12));
    for (let j = 0; j < ys.length; j += step) run(xs.map((x, i) => Z[j][i] == null ? null : P(nu(x), nv(ys[j]), nw(Z[j][i]))));
    for (let i = 0; i < xs.length; i += step) run(ys.map((y, j) => Z[j][i] == null ? null : P(nu(xs[i]), nv(y), nw(Z[j][i]))));
    const ms = Math.max(25, Math.min(80, 1800 / (lines.length || 1)));
    for (const s of lines) {
      if (token !== this.run) return;
      await this.stroke(m, s, smoothPath(s), ms, token, 1.4, { opacity: 0.85 });
    }
    if (a.fn) await this.legend(m, a.fn, a.fn.length > 34 ? a.fn.slice(0, 33) + '…' : a.fn, gx + 4, gy + 18, W - 8, token);
  }

  // ---- picture from Muse Image: a pencil sketch placed beside the work as a notebook picture ----
  async a_picture(a, token) {
    if (typeof a.image !== 'string' || !/^data:image\/(png|jpeg|webp);base64,/.test(a.image)) return;
    let rec;
    try { rec = await saveImage(await (await fetch(a.image)).blob()); } catch (err) { console.warn('[margin] picture failed', err); return; }
    if (token !== this.run) return;
    const k = Math.min(1, 300 / rec.w, 300 / rec.h), w = Math.round(rec.w * k), h = Math.round(rec.h * k);
    const anchor = this.box(a.after) || this.lastBox();
    const [x, y] = this.graphSpot(anchor, w, h);
    this.onReveal?.([x, y, w, h]);
    // fade in on Meta's layer, then hand it to the notebook as a picture the student can move or delete
    const img = svgEl('image', { href: a.image, x, y, width: w, height: h, opacity: 0 }, this.svg);
    await this.tween(this.fast ? 1 : 600, t => img.setAttribute('opacity', t), token);
    img.remove();
    if (token !== this.run) return;
    const it = { type: 'image', blob: rec.id, x, y, w, h, by: 'meta', mk: 'picture', kept: true, rid: this.pid };
    if (a.caption) it.say = a.caption;
    this.nb.items.push(prepareItem(it));
    this.nb.redraw();
  }
  // A legend line (`y = x sin x`): handwritten maths shrunk to fit the plot's width.
  async legend(m, text, short, x, base, maxW, token) {
    const bx = this.box_(() => lineBox({ text }));
    if (!bx) return this.write(m, short, x, base, 19, token);
    const em = Math.max(13, Math.min(22, maxW / Math.max(bx.w, 0.1)));
    return this.hand(m, bx, x, base, em, token, { label: text, maxMs: 600, speed: 1.6 });
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
