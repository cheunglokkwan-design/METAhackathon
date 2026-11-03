// Meta's pen: text and maths as real pen strokes, from Writey's handwriting engine
// (web/vendor/writey, MIT; glyphs from EMS Allure, SIL OFL 1.1, and Hershey).
// Everything the engine returns is in em (1 em = font size), y down, origin at the left end
// of the baseline; this module scales it to page pixels and turns it into SVG path data.
import { seeded, layoutMath, writeText, shiftPaths } from './vendor/writey/layout.js';
import { layoutParagraph } from './vendor/writey/text.js';
import { penTimes } from './vendor/writey/hand.js';

export { penTimes };
export { setHandStyle, handStyle } from './vendor/writey/font.js';
export const newSeed = () => (Math.random() * 2 ** 32) >>> 0;

/** Pen width for a writing size, as Writey's writer picks it (writer.ts penWidth). */
export const penWidth = em => Math.min(2.3, Math.max(1.2, em * 0.075));

/** Writing size (em, px) that matches the student's line height (after Writey's sizeFromLines). */
export const emFor = (lineH, lo = 26, hi = 40) => Math.min(hi, Math.max(lo, (lineH || 32) * 1.15));

// ---- unicode maths -> simple LaTeX ------------------------------------------------------
const SUP = { '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4', '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9', '⁺': '+', '⁻': '-', '⁼': '=', '⁽': '(', '⁾': ')', 'ⁿ': 'n', 'ˣ': 'x', 'ᵃ': 'a', 'ᵇ': 'b', 'ᵏ': 'k' };
const SUB = { '₀': '0', '₁': '1', '₂': '2', '₃': '3', '₄': '4', '₅': '5', '₆': '6', '₇': '7', '₈': '8', '₉': '9', '₊': '+', '₋': '-', '₌': '=', '₍': '(', '₎': ')', 'ₓ': 'x', 'ₙ': 'n', 'ₐ': 'a', 'ₖ': 'k' };
const SYM = { '∫': '\\int', 'π': '\\pi', 'θ': '\\theta', 'α': '\\alpha', 'β': '\\beta', 'λ': '\\lambda', 'φ': '\\phi', '·': '\\cdot ', '⋅': '\\cdot ', '×': '\\times ', '÷': '\\div ', '−': '-', '–': '-', '∞': '\\infty', '→': '\\to ', '≤': '\\le ', '≥': '\\ge ', '≠': '\\ne ', '≈': '\\approx ', '±': '\\pm ' };
export const FUNCS = new Set(['sin', 'cos', 'tan', 'sec', 'csc', 'cot', 'arcsin', 'arccos', 'arctan', 'sinh', 'cosh', 'tanh', 'ln', 'log', 'exp', 'lim', 'max', 'min']);
const SHORT_WORDS = new Set(['is', 'so', 'at', 'or', 'of', 'to', 'if', 'as', 'by', 'on', 'in', 'we', 'be', 'it', 'an', 'no']);
const OPS = new Set(['+', '-', '=', ',', '<', '>', '\\le ', '\\ge ', '\\ne ', '\\approx ', '\\to ', '\\pm ', '\\cdot ', '\\times ', '\\div ']);
const MATHY = /[\d=+^_∫√/²³¹⁰⁴⁵⁶⁷⁸⁹⁻ⁿ₀₁₂₃₄₅₆₇₈₉π·×≤≥≠→∞()]/;

function tokenize(s) {
  const out = [];
  const re = /(\s+)|(\d+(?:\.\d+)?)|([A-Za-z]+)|([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿˣᵃᵇᵏ]+)|([₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₓₙₐₖ]+)|(.)/gu;
  for (const m of s.matchAll(re)) {
    if (m[1]) out.push({ t: 'ws' });
    else if (m[2]) out.push({ t: 'num', v: m[2] });
    else if (m[3]) out.push({ t: 'word', v: m[3] });
    else if (m[4]) out.push({ t: 'sup', v: [...m[4]].map(c => SUP[c]).join('') });
    else if (m[5]) out.push({ t: 'sub', v: [...m[5]].map(c => SUB[c]).join('') });
    else out.push({ t: 'ch', v: m[6] });
  }
  return out;
}

const brace = s => `{${s}}`;
const stripParens = a => a.inner != null ? a.inner : a.tex;

// Recursive descent over the tokens: atoms (with their scripts), then a pass that turns
// `a/b` into \frac{a}{b} with the term on each side as its operands.
class Conv {
  constructor(toks, prose) { this.toks = toks; this.i = 0; this.prose = prose; }
  peek(k = 0) { return this.toks[this.i + k]; }
  seq(close) {
    const atoms = [];
    while (this.i < this.toks.length) {
      const t = this.peek();
      if (close && t.t === 'ch' && t.v === close) break;
      if (t.t === 'ch' && (t.v === ')' || t.v === ']') && !close) { this.i++; atoms.push({ tex: t.v === ')' ? ')' : ']', op: false }); continue; }
      const a = this.atom();
      if (a) atoms.push(this.post(a));
    }
    return fracs(atoms);
  }
  group(open, close) {                               // after the opener
    const inner = this.seq(close);
    if (this.peek()?.v === close) this.i++;
    return inner;
  }
  atom() {
    const t = this.toks[this.i++];
    if (t.t === 'ws') return { ws: true, tex: ' ' };
    if (t.t === 'num') return { tex: t.v };
    if (t.t === 'sup') return { tex: `{}^${brace(t.v)}` };
    if (t.t === 'sub') return { tex: `{}_${brace(t.v)}` };
    if (t.t === 'word') {
      const w = t.v;
      if (w === 'sqrt' && this.peek()?.v === '(') { this.i++; return { tex: `\\sqrt${brace(this.group('(', ')'))}` }; }
      if (FUNCS.has(w)) return { tex: `\\${w} `, fn: true };
      if (this.prose(w)) {                           // a run of words: one \text{…}
        let txt = w;
        while (this.peek()?.t === 'ws' && this.peek(1)?.t === 'word' && this.prose(this.peek(1).v)) { txt += ' ' + this.peek(1).v; this.i += 2; }
        return { tex: `\\text{${txt}}`, text: true };
      }
      if (/^d[a-z]$/.test(w) && (!this.toks[this.i - 2] || this.toks[this.i - 2].t === 'ws')) return { tex: `\\,d${w[1]}` };
      return { tex: w };
    }
    const c = t.v;
    if (c === '\\') return { tex: '\\backslash ' };
    if (c === '(') { const inner = this.group('(', ')'); return { tex: `\\left(${inner}\\right)`, inner }; }
    if (c === '[') { const inner = this.group('[', ']'); return { tex: `\\left[${inner}\\right]`, inner }; }
    if (c === '{') { const inner = this.group('{', '}'); return { tex: brace(inner), inner }; }
    if (c === '√') {
      if (this.peek()?.v === '(') { this.i++; return { tex: `\\sqrt${brace(this.group('(', ')'))}` }; }
      const a = this.peek() && this.peek().t !== 'ws' ? this.post(this.atom()) : { tex: '' };
      return { tex: `\\sqrt${brace(a.tex)}` };
    }
    if (c === '^' || c === '_') return { tex: '', script: c };
    if (c in SYM) { const tex = SYM[c]; return { tex, op: OPS.has(tex) }; }
    if (c === '/') return { tex: '/', slash: true };
    if (c === '%' || c === '#' || c === '&' || c === '$') return { tex: `\\${c}` };
    return { tex: c, op: OPS.has(c) };
  }
  // Scripts after an atom: ^2, ^(2x), ^-1, ², _0, ₀¹, and primes.
  post(a) {
    for (;;) {
      const t = this.peek();
      if (!t) break;
      if (t.t === 'sup') { this.i++; a = { tex: `${a.tex}^${brace(t.v)}` }; continue; }
      if (t.t === 'sub') { this.i++; a = { tex: `${a.tex}_${brace(t.v)}` }; continue; }
      if (t.t === 'ch' && (t.v === '^' || t.v === '_')) {
        this.i++;
        let arg = '';
        const n = this.peek();
        if (n?.v === '(' || n?.v === '{') { this.i++; arg = this.group(n.v, n.v === '(' ? ')' : '}'); }
        else if (n && (n.v === '-' || n.v === '−') && this.peek(1) && this.peek(1).t !== 'ws') { this.i++; arg = '-' + this.post(this.atom()).tex; }
        else if (n && n.t !== 'ws') arg = this.atom().tex;
        a = { tex: `${a.tex}${t.v}${brace(arg)}` };
        continue;
      }
      break;
    }
    return a;
  }
}

// `a/b` -> \frac{a}{b}: each operand is the run of atoms up to a space or an operator.
function fracs(atoms) {
  const out = [];
  for (let i = 0; i < atoms.length; i++) {
    const a = atoms[i];
    if (!a.slash) { out.push(a); continue; }
    const num = [];
    while (out.length && !out.at(-1).ws && !out.at(-1).op && !out.at(-1).text) num.unshift(out.pop());
    const den = [];
    let j = i + 1;
    while (j < atoms.length && !atoms[j].ws && !atoms[j].op && !atoms[j].slash && !atoms[j].text) den.push(atoms[j++]);
    if (!num.length || !den.length) { out.push(...num, a, ...den); i = j - 1; continue; }
    const side = xs => xs.length === 1 ? stripParens(xs[0]) : xs.map(x => x.tex).join('');
    out.push({ tex: `\\frac${brace(side(num))}${brace(side(den))}` });
    i = j - 1;
  }
  return out.map(a => a.tex).join('').replace(/\s+/g, ' ').trim();
}

/** Converts a line of unicode maths (`∫₀¹ x² dx`, `x^3/3 + C`, `[x³/3]₀¹`) into LaTeX. */
export function toLatex(text) {
  const s = String(text ?? '').trim();
  if (!s) return '';
  if (/\\[a-zA-Z]+/.test(s)) return s;                       // already LaTeX
  const toks = tokenize(s);
  const words = toks.filter(t => t.t === 'word').map(t => t.v);
  const longProse = words.some(w => w.length >= 3 && !FUNCS.has(w) && w !== 'sqrt');
  const prose = w => (w.length >= 3 && !FUNCS.has(w) && w !== 'sqrt') || (longProse && SHORT_WORDS.has(w.toLowerCase())) || (longProse && /^[A-Z][a-z]+$/.test(w));
  return new Conv(toks, prose).seq(null);
}

/** True when a line reads as maths rather than as a sentence. */
export function isMath(text) {
  const s = String(text ?? '');
  if (!MATHY.test(s)) return false;
  const words = s.match(/[A-Za-z]{3,}/g) || [];
  const prose = words.filter(w => !FUNCS.has(w) && w !== 'sqrt');
  return prose.length <= 2 || prose.length * 4 < s.replace(/\s/g, '').length / 2;
}

// ---- boxes --------------------------------------------------------------------------------
/** A formula as a box in em (Writey's TeX-like layout: fractions, scripts, ∫ with limits, √). */
export const mathBox = (latex, seed = newSeed()) => layoutMath(latex, seeded(seed));
/** A line of joined handwriting in em. */
export const textBox = (text, seed = newSeed()) => writeText(String(text), seeded(seed));

/** Sentence with any maths in it marked as $…$, so it is laid out as maths inline. */
export function markInlineMath(text) {
  if (/\$/.test(text)) return text;
  return String(text).split(/(\s+)/).map(w => {
    if (!w.trim()) return w;
    const core = w.replace(/[.,;:!?]+$/, ''), tail = w.slice(core.length);
    if (/[²³¹⁰⁴⁵⁶⁷⁸⁹⁻ⁿ₀₁₂₃₄₅₆₇₈₉^_∫√=/]|\d[a-z]|[a-z]\(|\)/.test(core)) return `$${toLatex(core)}$${tail}`;
    return w;
  }).join('');
}

/** Wraps a sentence (with inline maths) to `maxEm` wide lines. Returns em boxes. */
export const paragraph = (text, maxEm, seed = newSeed()) => layoutParagraph(markInlineMath(text), Math.max(3, maxEm), seeded(seed));

/** Box for one line of Meta's ink: maths when it reads as maths (or `latex` is given). */
export function lineBox({ text, latex }, seed = newSeed()) {
  if (latex) return mathBox(latex, seed);
  if (isMath(text)) return mathBox(toLatex(text), seed);
  return paragraph(text, 1e4, seed)[0] || textBox(text, seed);
}

export const shift = shiftPaths;

/** SVG path data for one em path, scaled by `k` and moved to (x, base). Smoothed through midpoints. */
export function pathD(p, k, x, base) {
  const n = p.length / 2;
  const X = i => (x + p[2 * i] * k).toFixed(1), Y = i => (base + p[2 * i + 1] * k).toFixed(1);
  if (n === 1) return `M${X(0)} ${Y(0)} l0.4 0.3`;
  let d = `M${X(0)} ${Y(0)}`;
  if (n === 2) return `${d} L${X(1)} ${Y(1)}`;
  for (let i = 1; i < n - 1; i++) {
    const mx = (x + (p[2 * i] + p[2 * i + 2]) / 2 * k).toFixed(1), my = (base + (p[2 * i + 1] + p[2 * i + 3]) / 2 * k).toFixed(1);
    d += ` Q${X(i)} ${Y(i)} ${mx} ${my}`;
  }
  return `${d} L${X(n - 1)} ${Y(n - 1)}`;
}
