// Copyright (c) 2026 Writey contributors. MIT License, see ./LICENSE.
// Ported from Writey (MIT), github.com/lagoonitself/writey, packages/inkscript/src/font.ts:
// TypeScript types stripped, imports made browser ES modules (no build step).
import { GLYPHS } from './glyphs.js';
import { PRINT_GLYPHS } from './glyphs_print.js';   // Margin addition: upright print letters (EMS Readability)
/** Height of a lowercase letter, and of the math axis (where minus signs and fraction bars sit). */
export const X_HEIGHT = 0.35;
export const AXIS = 0.24;
export const CAP_HEIGHT = 0.66;
const cache = new Map();
// Margin addition: 'print' (default) writes letters, digits and punctuation in upright print;
// 'cursive' keeps Writey's EMS Allure. Maths operators and Greek always come from Allure.
const PRINT_CHARS = /^[A-Za-z0-9.,:;!?'"()\[\]%&]$/;
let style = 'print';
const PRINT_SCALE = 0.86;
export function setHandStyle(s) { if (s !== style) { style = s === 'cursive' ? 'cursive' : 'print'; cache.clear(); } }
export const handStyle = () => style;
const ALIASES = {
    '’': "'",
    '‘': "'",
    '“': '"',
    '”': '"',
    '−': '-',
    '·': '⋅',
    ϵ: 'ε',
    ϕ: 'φ',
    ϑ: 'θ',
    '∥': '‖',
    ' ': ' ',
};
export function glyph(ch) {
    const key = ALIASES[ch] ?? ch;
    if (cache.has(key))
        return cache.get(key) ?? null;
    const print = style === 'print' && PRINT_CHARS.test(key) && PRINT_GLYPHS[key];
    // print letters are drawn a little smaller so their tall x-height sits like Allure's on the line
    const raw = print ? [print[0] * PRINT_SCALE, print[1].map(st => st.map(n => n * PRINT_SCALE))] : GLYPHS[key];
    let g = null;
    if (raw) {
        const strokes = raw[1].map((s) => s.map((n) => n / 1000));
        let x0 = Number.POSITIVE_INFINITY;
        let x1 = Number.NEGATIVE_INFINITY;
        let y0 = 0;
        let y1 = 0;
        for (const s of strokes) {
            for (let i = 0; i < s.length; i += 2) {
                x0 = Math.min(x0, s[i]);
                x1 = Math.max(x1, s[i]);
                y0 = Math.min(y0, s[i + 1]);
                y1 = Math.max(y1, s[i + 1]);
            }
        }
        if (!strokes.length) {
            x0 = 0;
            x1 = raw[0] / 1000;
        }
        g = { w: raw[0] / 1000, x0, x1, h: -y0, d: y1, strokes };
    }
    cache.set(key, g);
    return g;
}
export const hasGlyph = (ch) => glyph(ch) !== null;
