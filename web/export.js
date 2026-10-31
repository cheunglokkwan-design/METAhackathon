// Renders stored pages to canvas: sidebar thumbnails, "Export page as PNG", "Export notebook as PDF".
import { PAGE_W, PAGE_H, prepareItem, paintItems, normalizeItem } from './ink.js';
import { paintMeta } from './assistant.js';
import { loadItemImages } from './images.js';

const PAPER = '#fcf9f2', RULE_C = '#cddbea', GRID_C = '#dfe6ee', MARGIN_C = '#e6a29c';

// Paper drawn in device pixels so rules stay crisp and even at any zoom (no moiré).
export function paintPaper(c, paper = 'ruled') {
  const r = c.getTransform().a || 1, lw = Math.max(1, Math.round(r));
  const W = Math.round(PAGE_W * r), H = Math.round(PAGE_H * r), top = Math.round(100 * r);
  c.save();
  c.setTransform(1, 0, 0, 1, 0, 0);
  c.fillStyle = PAPER; c.fillRect(0, 0, W, H);
  if (paper === 'ruled') {
    c.fillStyle = RULE_C;
    for (let y = 132; y < PAGE_H; y += 44) c.fillRect(0, Math.round(y * r), W, lw);
    c.fillStyle = MARGIN_C; c.fillRect(Math.round(95 * r), 0, Math.max(1, Math.round(1.5 * r)), H);
  } else if (paper === 'grid') {
    c.fillStyle = GRID_C;
    for (let y = 110; y < PAGE_H; y += 22) c.fillRect(0, Math.round(y * r), W, lw);
    for (let x = 22; x < PAGE_W; x += 22) c.fillRect(Math.round(x * r), top, lw, H - top);
  }
  c.restore();
}

export function paintPage(c, page, { meta = true } = {}) {
  paintPaper(c, page.paper);
  paintItems(c, (page.items || []).map(it => prepareItem({ ...normalizeItem(it) })));
  if (meta) paintMeta(c, page.meta || []);
}

export function renderPage(page, scale = 2) {
  const cv = document.createElement('canvas');
  cv.width = Math.round(PAGE_W * scale); cv.height = Math.round(PAGE_H * scale);
  const c = cv.getContext('2d');
  c.scale(scale, scale);
  paintPage(c, page);
  return cv;
}

// Paints at once with the pictures already decoded, then again once the rest have loaded.
export function drawThumb(canvas, page) {
  paintThumb(canvas, page);
  loadItemImages(page.items).then(n => { if (n) paintThumb(canvas, page); });
}
function paintThumb(canvas, page) {
  const r = Math.min(2, window.devicePixelRatio || 1), w = canvas.clientWidth || 120;
  const s = (w * r) / PAGE_W;
  canvas.width = Math.round(PAGE_W * s); canvas.height = Math.round(PAGE_H * s);
  const c = canvas.getContext('2d');
  c.setTransform(s, 0, 0, s, 0, 0);
  paintPage(c, page);
}

const safeName = s => (s || 'Margin').replace(/[^\w\- ]+/g, '').trim().replace(/\s+/g, '-') || 'Margin';

// iPad: the share sheet (Save to Files / Photos); elsewhere: a normal download.
async function deliver(blob, filename) {
  const file = new File([blob], filename, { type: blob.type });
  const touch = matchMedia('(hover: none)').matches;
  if (touch && navigator.canShare?.({ files: [file] })) {
    try { await navigator.share({ files: [file], title: filename }); return; } catch (err) { if (err.name === 'AbortError') return; }
  }
  const url = URL.createObjectURL(blob), a = document.createElement('a');
  a.href = url; a.download = filename; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

export async function exportPNG(page, title, n) {
  await loadItemImages(page.items);
  const cv = renderPage(page, 2);
  const blob = await new Promise(res => cv.toBlob(res, 'image/png'));
  await deliver(blob, `${safeName(title)}-page-${n}.png`);
}

let jspdf;
function loadJsPDF() {
  if (window.jspdf) return Promise.resolve(window.jspdf);
  jspdf ??= new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = 'vendor/jspdf.umd.min.js';
    s.onload = () => res(window.jspdf);
    s.onerror = () => { jspdf = null; rej(new Error('jsPDF failed to load')); };
    document.head.append(s);
  });
  return jspdf;
}

export async function exportPDF(pages, title) {
  const { jsPDF } = await loadJsPDF();
  for (const p of pages) await loadItemImages(p.items);
  const W = 595, H = W * PAGE_H / PAGE_W;                  // A4 width, page aspect
  const doc = new jsPDF({ unit: 'pt', format: [W, H], orientation: 'portrait', compress: true });
  pages.forEach((p, i) => {
    if (i) doc.addPage([W, H], 'portrait');
    doc.addImage(renderPage(p, 1.6).toDataURL('image/jpeg', 0.9), 'JPEG', 0, 0, W, H);
  });
  await deliver(doc.output('blob'), `${safeName(title)}.pdf`);
}
