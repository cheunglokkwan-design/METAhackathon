// Tidy handwriting automatically, the way Apple Notes and Samsung Notes do:
//   1. A letter we're sure about is redrawn outright as a clean handwritten letter (a wobbly C
//      becomes a clear c). "Sure" means Meta read it and the shape agrees, or, without Meta, the
//      shape matches one letter closely and clearly better than any other.
//   2. Everything else only gets the light touch: each row is straightened onto its baseline,
//      letters of about the same size are evened out, gaps are evened out, and strokes are smoothed.
// Works on stroke items ({ pts: [[x, y, p], ...] }); returns new point arrays and never mutates its input.

import { PRINT_GLYPHS } from './vendor/writey/glyphs_print.js';
import { smoothStroke } from './shapes.js';

const N = 32;                      // points per stroke when comparing shapes

const bboxOf = pts => {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  return { x0, y0, x1, y1, w: x1 - x0, h: y1 - y0 };
};
const median = xs => { if (!xs.length) return 0; const s = [...xs].sort((a, b) => a - b); return s[Math.floor(s.length / 2)]; };

// Rows of strokes, top to bottom (strokes overlapping vertically by half the shorter one share a row).
export function rowsOf(strokes) {
  const size = s => { const b = bboxOf(s.pts); return Math.max(b.w, b.h); };
  const big = median(strokes.map(size)) * 0.35;
  const mid = s => { const b = bboxOf(s.pts); return (b.y0 + b.y1) / 2; };
  const rows = [];
  // letters first; dots (of i, j, a full stop) then join the row they sit over or under
  for (const s of strokes.filter(s => size(s) >= big).sort((a, b) => mid(a) - mid(b))) {
    const b = bboxOf(s.pts);
    const r = rows.find(r => Math.min(r.b.y1, b.y1) - Math.max(r.b.y0, b.y0) > Math.min(r.b.h, b.h) * 0.5);
    if (r) { r.items.push(s); r.b = bboxOf([[r.b.x0, r.b.y0], [r.b.x1, r.b.y1], [b.x0, b.y0], [b.x1, b.y1]]); }
    else rows.push({ items: [s], b });
  }
  for (const s of strokes.filter(s => size(s) < big)) {
    const b = bboxOf(s.pts), y = mid(s);
    const near = rows.map(r => ({ r, d: Math.max(0, r.b.y0 - y, y - r.b.y1) })).sort((a, b) => a.d - b.d)[0];
    if (near) near.r.items.push(s); else rows.push({ items: [s], b });
  }
  return rows.sort((a, b) => a.b.y0 - b.b.y0).map(r => r.items);
}

// Letters of a row: strokes whose x-ranges overlap (the dot of an i, the bar of a t, the two lines of =).
export function lettersOf(row) {
  const out = [];
  for (const s of [...row].sort((a, b) => bboxOf(a.pts).x0 - bboxOf(b.pts).x0)) {
    const b = bboxOf(s.pts), last = out.at(-1);
    const ov = last ? Math.min(last.b.x1, b.x1) - Math.max(last.b.x0, b.x0) : -1;
    if (last && ov > Math.min(last.b.w, b.w) * 0.35) {
      last.items.push(s);
      last.b = bboxOf([[last.b.x0, last.b.y0], [last.b.x1, last.b.y1], [b.x0, b.y0], [b.x1, b.y1]]);
    } else out.push({ items: [s], b });
  }
  return out;
}

// ---- 1. geometry: baseline, size and spacing -------------------------------------------------

function geometry(row, pts, strength) {
  const letters = lettersOf(row).map(l => ({ ...l, b: bboxOf(l.items.flatMap(s => pts.get(s))) }));
  if (letters.length < 2) return;
  const H = median(letters.map(l => l.b.h));
  // "body" letters: about the median height and sitting on the line (not ∫, g, y or capitals)
  const body = letters.filter(l => l.b.h > H * 0.6 && l.b.h < H * 1.35);
  const bottom = l => l.b.y1;
  // baseline: least squares through the bottoms of the body letters
  if (body.length >= 2) {
    const xs = body.map(l => (l.b.x0 + l.b.x1) / 2), ys = body.map(bottom);
    const mx = xs.reduce((a, b) => a + b) / xs.length, my = ys.reduce((a, b) => a + b) / ys.length;
    const sxx = xs.reduce((a, x) => a + (x - mx) ** 2, 0);
    let slope = sxx ? xs.reduce((a, x, i) => a + (x - mx) * (ys[i] - my), 0) / sxx : 0;
    slope = Math.max(-0.18, Math.min(0.18, slope)) * strength;       // at most about 10°
    const cos = Math.cos(-Math.atan(slope)), sin = Math.sin(-Math.atan(slope));
    for (const s of row) pts.set(s, pts.get(s).map(([x, y, p]) => [mx + (x - mx) * cos - (y - my) * sin, my + (x - mx) * sin + (y - my) * cos, p]));
    for (const l of letters) l.b = bboxOf(l.items.flatMap(s => pts.get(s)));
    const base = median(body.map(bottom));
    for (const l of body) {
      const dy = (base - bottom(l)) * 0.7 * strength;
      // size: pull a body letter halfway to the median height, about its bottom centre
      const k = 1 + (H / Math.max(1, l.b.h) - 1) * 0.5 * strength, cx = (l.b.x0 + l.b.x1) / 2;
      for (const s of l.items) pts.set(s, pts.get(s).map(([x, y, p]) => [cx + (x - cx) * k, l.b.y1 + (y - l.b.y1) * k + dy, p]));
      l.b = bboxOf(l.items.flatMap(s => pts.get(s)));
    }
  }
  // spacing: letter gaps towards their median, word gaps towards theirs
  const gaps = letters.slice(1).map((l, i) => l.b.x0 - letters[i].b.x1);
  const inner = gaps.filter(g => g < Math.max(H * 0.6, median(gaps) * 2.2));
  const g0 = median(inner), gw = median(gaps.filter(g => !inner.includes(g))) || g0 * 3;
  let shift = 0;
  for (let i = 1; i < letters.length; i++) {
    const g = gaps[i - 1], target = inner.includes(g) ? Math.max(g0, H * 0.08) : gw;
    shift += (target - g) * 0.5 * strength;
    if (Math.abs(shift) > 0.01) for (const s of letters[i].items) pts.set(s, pts.get(s).map(([x, y, p]) => [x + shift, y, p]));
  }
}

// ---- 2. letters: blend towards the print letter when we're sure ------------------------------

function resample(pts, n = N) {
  const out = [], seg = [0];
  for (let i = 1; i < pts.length; i++) seg.push(seg[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  const L = seg.at(-1) || 1;
  for (let k = 0, j = 0; k < n; k++) {
    const t = L * k / (n - 1);
    while (j < pts.length - 2 && seg[j + 1] < t) j++;
    const a = pts[j], b = pts[Math.min(j + 1, pts.length - 1)], d = (seg[j + 1] ?? seg[j]) - seg[j] || 1;
    const u = Math.max(0, Math.min(1, (t - seg[j]) / d));
    out.push([a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, (a[2] ?? 0.5) + ((b[2] ?? 0.5) - (a[2] ?? 0.5)) * u]);
  }
  return out;
}

const templateOf = ch => {
  const g = PRINT_GLYPHS[ch];
  if (!g || !g[1].length) return null;
  return g[1].map(flat => { const p = []; for (let i = 0; i < flat.length; i += 2) p.push([flat[i], flat[i + 1], 0.5]); return p; });
};

// ---- recognising a letter: point clouds compared by chamfer distance ----------------------------

// ~n points spread along the strokes, centred and scaled so the bigger side is 1 (aspect kept).
function cloud(strokes, n = 48) {
  const lens = strokes.map(s => s.reduce((a, p, i) => i ? a + Math.hypot(p[0] - s[i - 1][0], p[1] - s[i - 1][1]) : 0, 0));
  const total = lens.reduce((a, b) => a + b, 0) || 1;
  const pts = strokes.flatMap((s, i) => s.length < 2 ? [s[0]] : resample(s, Math.max(2, Math.round(n * lens[i] / total))));
  const b = bboxOf(pts), k = 1 / Math.max(b.w, b.h, 1e-6), cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
  return { pts: pts.map(([x, y]) => [(x - cx) * k, (y - cy) * k]), aspect: b.w / Math.max(b.h, 1e-6) };
}
function chamfer(a, b) {
  const one = (p, q) => p.reduce((s, [x, y]) => s + Math.min(...q.map(([u, v]) => Math.hypot(x - u, y - v))), 0) / p.length;
  return (one(a.pts, b.pts) + one(b.pts, a.pts)) / 2;
}
const CANDIDATES = [...'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-=()'];
const clouds = new Map();
const cloudOf = ch => { if (!clouds.has(ch)) { const t = templateOf(ch); clouds.set(ch, t ? cloud(t) : null); } return clouds.get(ch); };
// letters that look the same apart from size (c and C, o O 0): picked between by height, not shape
const family = ch => ({ o: 'o', O: 'o', 0: 'o', l: 'l', I: 'l', 1: 'l', '|': 'l' })[ch] ?? ch.toLowerCase();
const SURE_META = 0.17;            // Meta named the letter and the shape agrees this well
const SURE_APP = 0.085;            // no Meta: the best match must be this close...
const MARGIN = 1.55;               // ...and clearly better than any different-looking letter

// What letter this is, if we're sure: { ch, by } or null.
export function recognise(strokes, metaCh = null, rowH = 0) {
  const c = cloud(strokes), box = bboxOf(strokes.flat());
  if (Math.max(box.w, box.h) < 6) return null;
  const scored = CANDIDATES.map(ch => ({ ch, d: cloudOf(ch) ? chamfer(c, cloudOf(ch)) : Infinity })).sort((a, b) => a.d - b.d);
  if (metaCh) {
    // Meta's letter must fit the shape, and fit it about as well as the best-looking letter does
    const t = cloudOf(metaCh), d = t ? chamfer(c, t) : Infinity;
    const other = scored.find(x => family(x.ch) !== family(metaCh));
    return d < SURE_META && (!other || d <= other.d * 1.1) ? { ch: metaCh, by: 'meta' } : null;
  }
  const best = scored[0], rival = scored.find(x => family(x.ch) !== family(best.ch));
  if (!(best.d < SURE_APP && (!rival || rival.d > best.d * MARGIN))) return null;
  // size decides c / C and o / O / 0 the way a reader would
  const tall = rowH && box.h > rowH * 1.3;
  let ch = best.ch;
  if (/[a-z]/i.test(ch) && ch.toLowerCase() !== ch.toUpperCase()) {
    const lowerSameShape = 'cosuvwxz'.includes(ch.toLowerCase());
    if (lowerSameShape) ch = tall ? ch.toUpperCase() : ch.toLowerCase();
  }
  if (family(ch) === 'o') ch = tall ? '0' : 'o';                  // maths pages: a tall round one is a zero
  return { ch, by: 'app' };
}

// The clean letter `ch` drawn into the student's letter box (height kept, centred), as stroke points.
function cleanLetter(ch, box, pressure, spacing = 4) {
  const tmpl = templateOf(ch);
  const tb = bboxOf(tmpl.flat());
  const k = tb.h > 40 ? box.h / tb.h : box.w / Math.max(tb.w, 1);
  const tcx = (tb.x0 + tb.x1) / 2, tcy = (tb.y0 + tb.y1) / 2, bcx = (box.x0 + box.x1) / 2, bcy = (box.y0 + box.y1) / 2;
  return tmpl.map(st => {
    const pts = st.map(([x, y]) => [bcx + (x - tcx) * k, bcy + (y - tcy) * k, pressure]);
    if (pts.length < 2) return pts;
    // as many points as the student's pen would lay down over that length (point spacing sets how
    // thick simulated-pressure ink looks)
    const len = pts.reduce((a, p, i) => i ? a + Math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]) : 0, 0);
    return resample(pts, Math.max(4, Math.round(len / Math.max(1, spacing)) + 1));
  });
}

// ---- entry points -------------------------------------------------------------------------------

// Tidies `strokes`. Returns { pts: Map stroke -> new points, replace: [{ items, strokes }], rows, letters }:
// `replace` lists letters recognised for sure, to be redrawn outright as a clean handwritten letter
// (`strokes` = the new point lists). Everything else is only straightened, evened out and smoothed.
// `texts` (optional): what Meta read, one string per row, top to bottom.
export function refine(strokes, { texts = null, strength = 1, letters: doLetters = true } = {}) {
  const pts = new Map(strokes.map(s => [s, s.pts.map(p => [...p])]));
  const rows = rowsOf(strokes);
  const replace = [];
  rows.forEach((row, r) => {
    geometry(row, pts, strength);
    if (!doLetters) return;
    const ls = lettersOf(row);
    const chars = texts && texts.length === rows.length ? [...String(texts[r]).replace(/\s+/g, '')] : null;
    const metaFits = chars && chars.length === ls.length;
    const rowH = median(ls.map(l => l.b.h));
    for (const [i, l] of ls.entries()) {
      const mine = l.items.map(s => pts.get(s));
      const got = recognise(mine, metaFits && templateOf(chars[i]) ? chars[i] : null, ls.length > 1 ? rowH : 0);
      if (!got) continue;
      const p = median(mine.flat().map(q => q[2] ?? 0.5));
      const gap = median(l.items.flatMap(s => s.pts.slice(1).map((q, i) => Math.hypot(q[0] - s.pts[i][0], q[1] - s.pts[i][1]))));
      replace.push({ items: l.items, ch: got.ch, by: got.by, strokes: cleanLetter(got.ch, bboxOf(mine.flat()), p, gap) });
    }
  });
  for (const s of strokes) pts.set(s, smoothStroke(pts.get(s), strength >= 1 ? 2 : 1));
  return { pts, replace, rows: rows.length, letters: replace.length };
}
