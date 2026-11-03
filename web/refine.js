// Refine handwriting, the way Apple Notes ("Refine") and Samsung Notes tidy it: the writing stays
// the student's own. Nothing is swapped for a typed font.
//   1. Each row is straightened onto its baseline, letters of about the same size are evened out,
//      and the gaps between letters and words are evened out.
//   2. Only when the letters are known for sure (Meta read the row, and the row has exactly one
//      stroke group per letter, with the right number of strokes) is each letter blended part of
//      the way towards a clean print letter. A messy C becomes a readable c that still looks
//      handwritten. A letter that doesn't match its print shape closely enough is left alone.
// Works on stroke items ({ pts: [[x, y, p], ...] }); returns new point arrays and never mutates its input.

import { PRINT_GLYPHS } from './vendor/writey/glyphs_print.js';
import { smoothStroke } from './shapes.js';

const N = 32;                      // points per stroke when comparing shapes
const BLEND = 0.42;                // how far a recognised letter moves towards the print letter
const MATCH = 0.3;                 // largest shape difference (in letter boxes) still counted as "sure"
const NEAT = 0.06;                 // below this the letter is already neat: leave it

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

// The print letter's strokes fitted to the student's letter box, paired with the student's strokes
// (nearest centres, either direction); null if the stroke counts differ.
function pairUp(strokes, tmpl, box) {
  if (strokes.length !== tmpl.length) return null;
  const tb = bboxOf(tmpl.flat());
  const sy = box.h / Math.max(1, tb.h), sx = tb.w < 40 || box.w < 3 ? sy : box.w / tb.w;
  const tcx = (tb.x0 + tb.x1) / 2, bcx = (box.x0 + box.x1) / 2;
  const fit = tmpl.map(s => resample(s.map(([x, y]) => [bcx + (x - tcx) * sx, box.y0 + (y - tb.y0) * sy, 0.5])));
  const mine = strokes.map(s => resample(s));
  const c = s => [s.reduce((a, p) => a + p[0], 0) / s.length, s.reduce((a, p) => a + p[1], 0) / s.length];
  const used = new Set(), pairs = [];
  for (const m of mine) {
    const cm = c(m);
    let best = -1, bd = Infinity;
    fit.forEach((t, i) => { if (used.has(i)) return; const ct = c(t), d = Math.hypot(ct[0] - cm[0], ct[1] - cm[1]); if (d < bd) { bd = d; best = i; } });
    used.add(best);
    const t = fit[best], rev = [...t].reverse();
    const dist = s => s.reduce((a, p, i) => a + Math.hypot(p[0] - m[i][0], p[1] - m[i][1]), 0) / N;
    const [tt, d] = dist(t) <= dist(rev) ? [t, dist(t)] : [rev, dist(rev)];
    pairs.push({ m, t: tt, d: d / Math.max(box.w, box.h, 1) });
  }
  return pairs;
}

function blendLetter(letter, ch, pts) {
  const tmpl = templateOf(ch);
  if (!tmpl) return false;
  const strokes = letter.items.map(s => pts.get(s));
  const box = bboxOf(strokes.flat());
  if (box.w < 3 && box.h < 3) return false;
  const pairs = pairUp(strokes, tmpl, box);
  if (!pairs) return false;
  const d = Math.max(...pairs.map(p => p.d));
  if (d > MATCH || d < NEAT) return false;                       // not sure what it is, or already neat
  letter.items.forEach((s, i) => {
    const { m, t } = pairs[i];
    pts.set(s, m.map((p, k) => [p[0] + (t[k][0] - p[0]) * BLEND, p[1] + (t[k][1] - p[1]) * BLEND, p[2]]));
  });
  return true;
}

// ---- entry points -------------------------------------------------------------------------------

// Map stroke -> refined points. `texts` (optional): what Meta read, one string per row, top to bottom.
// strength 1 = the Refine button; auto-refine while writing uses a gentler 0.6 and no letters.
export function refine(strokes, { texts = null, strength = 1 } = {}) {
  const pts = new Map(strokes.map(s => [s, s.pts.map(p => [...p])]));
  const rows = rowsOf(strokes);
  let letters = 0;
  rows.forEach((row, r) => {
    geometry(row, pts, strength);
    const chars = texts && texts.length === rows.length ? [...String(texts[r]).replace(/\s+/g, '')] : null;
    const ls = lettersOf(row);
    if (chars && chars.length === ls.length) ls.forEach((l, i) => { if (blendLetter(l, chars[i], pts)) letters++; });
  });
  for (const s of strokes) pts.set(s, smoothStroke(pts.get(s), strength >= 1 ? 2 : 1));
  return { pts, rows: rows.length, letters };
}
