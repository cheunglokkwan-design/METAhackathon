// Zoom and pan for the page: pinch / two-finger pan on touch, ctrl+wheel (and trackpad pinch)
// on laptops, keyboard shortcuts via setZoom. The page stays a fixed 1000 x 1400 box in page
// coordinates scaled with a CSS transform, so student ink, Meta's ink and hit-testing all
// keep working at any zoom: only Notebook.scale (screen px per page px) changes.
import { PAGE_W, PAGE_H } from './ink.js';

const MIN = 0.5, MAX = 4;                      // relative to "fit to width"
const TOP_GAP = 28, BOTTOM_GAP = 140;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

export class Zoom {
  constructor({ desk, sheet, page, nb, onChange }) {
    Object.assign(this, { desk, sheet, page, nb, onChange });
    this.fit = 1; this.zoom = 1;
    this.touches = new Map(); this.mode = null;
    new ResizeObserver(() => this.layout()).observe(desk);
    this.layout();

    desk.addEventListener('pointerdown', e => this.down(e), true);
    desk.addEventListener('pointermove', e => this.move(e), true);
    desk.addEventListener('pointerup', e => this.up(e), true);
    desk.addEventListener('pointercancel', e => this.up(e), true);
    desk.addEventListener('wheel', e => this.wheel(e), { passive: false });
    // Safari: keep its own page zoom out of the way (iPad pinch, Mac trackpad).
    for (const t of ['gesturestart', 'gesturechange']) document.addEventListener(t, e => e.preventDefault());
  }

  get scale() { return this.fit * this.zoom; }
  get gutter() { return this.desk.clientWidth < 700 ? 12 : 32; }

  layout({ resolution = true } = {}) {
    const g = this.gutter, dw = this.desk.clientWidth;
    if (!dw) return;
    this.fit = Math.min(1.15, (dw - g * 2) / PAGE_W);
    const s = this.scale, w = PAGE_W * s, h = PAGE_H * s;
    const sheetW = w + g * 2;
    Object.assign(this.sheet.style, { width: `${sheetW}px`, height: `${h + TOP_GAP + BOTTOM_GAP}px`, marginLeft: `${Math.max(0, (dw - sheetW) / 2)}px` });
    Object.assign(this.page.style, { left: `${g}px`, top: `${TOP_GAP}px`, transform: `scale(${s})` });
    this.nb.setScale(s, { resolution });
  }

  // Zoom so that page point `p` (page coords) ends up under client point `at`.
  setZoom(z, at, p, { resolution = true } = {}) {
    const r0 = this.desk.getBoundingClientRect();
    at ??= { x: r0.left + this.desk.clientWidth / 2, y: r0.top + this.desk.clientHeight / 2 };
    p ??= this.pagePoint(at);
    this.zoom = clamp(z, MIN, MAX);
    this.layout({ resolution });
    const r = this.page.getBoundingClientRect(), s = this.scale;
    this.desk.scrollLeft += r.left + p.x * s - at.x;
    this.desk.scrollTop += r.top + p.y * s - at.y;
    if (!resolution) this.settleSoon();
    this.onChange?.(this.zoom);
  }
  reset() { this.setZoom(1); }
  zoomBy(f) { this.setZoom(this.zoom * f); }
  settleSoon() { clearTimeout(this.settle); this.settle = setTimeout(() => this.nb.setResolution(this.scale), 160); }

  pagePoint({ x, y }) {
    const r = this.page.getBoundingClientRect();
    return { x: (x - r.left) / this.scale, y: (y - r.top) / this.scale };
  }
  // Rect of a page-coords box in the desk's scroll coordinates.
  deskBox([x, y, w, h]) {
    const pr = this.page.getBoundingClientRect(), dr = this.desk.getBoundingClientRect(), s = this.scale;
    return { left: pr.left - dr.left + this.desk.scrollLeft + x * s, top: pr.top - dr.top + this.desk.scrollTop + y * s, width: w * s, height: h * s };
  }

  // ---- touch: one finger draws (until a stylus is seen) or pans; two fingers pinch and pan ----
  down(e) {
    if (e.pointerType !== 'touch') return;
    this.touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (this.touches.size === 2) {
      this.nb.cancelActive();
      const [a, b] = [...this.touches.values()];
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      this.mode = { kind: 'pinch', dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, zoom: this.zoom, p: this.pagePoint(mid) };
      e.stopPropagation(); e.preventDefault();
      return;
    }
    if (this.touches.size > 2) { e.stopPropagation(); return; }
    const onPage = this.page.contains(e.target);
    if (e.target.closest('.tbox-ui, .tap-target, .sel-bar')) return;
    if (onPage && !this.nb.penSeen) return;              // finger writes, like a stylus
    this.mode = { kind: 'pan', id: e.pointerId, x: e.clientX, y: e.clientY };
    e.stopPropagation();
  }

  move(e) {
    const t = this.touches.get(e.pointerId);
    if (!t) return;
    t.x = e.clientX; t.y = e.clientY;
    const m = this.mode;
    if (m?.kind === 'pinch') {
      const [a, b] = [...this.touches.values()];
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      this.setZoom(m.zoom * Math.hypot(a.x - b.x, a.y - b.y) / m.dist, mid, m.p, { resolution: false });
      e.stopPropagation();
    } else if (m?.kind === 'pan' && m.id === e.pointerId) {
      this.desk.scrollLeft -= e.clientX - m.x; this.desk.scrollTop -= e.clientY - m.y;
      m.x = e.clientX; m.y = e.clientY;
      e.stopPropagation();
    }
  }

  up(e) {
    if (!this.touches.delete(e.pointerId)) return;
    const m = this.mode;
    if (m?.kind === 'pinch') {
      e.stopPropagation();
      if (this.touches.size === 1) {                    // keep panning with the finger that stayed
        const [[id, t]] = [...this.touches.entries()];
        this.mode = { kind: 'pan', id, x: t.x, y: t.y };
      } else if (!this.touches.size) this.mode = null;
      this.settleSoon();
    } else if (m?.kind === 'pan' && m.id === e.pointerId) {
      this.mode = null;
      e.stopPropagation();
    }
  }

  // ---- ctrl/cmd + wheel (trackpad pinch arrives as ctrl+wheel in Chrome) ----
  wheel(e) {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const d = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    this.setZoom(this.zoom * Math.exp(-clamp(d, -60, 60) * 0.01), { x: e.clientX, y: e.clientY }, undefined, { resolution: false });
  }
}
