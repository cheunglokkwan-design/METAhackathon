// Photos and PDF pages on the page: stored once in IndexedDB (store.blobs) as Blobs, decoded
// into an in-memory cache so the ink canvas, thumbnails and exports can draw them synchronously.
import { store, uid } from './store.js';

const cache = new Map();          // blob id -> { img, w, h } once decoded
const pending = new Map();        // blob id -> Promise
const MAX_DIM = 2200;             // longer side kept for photos (keeps storage and snapshots sane)

function decode(blob) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob), img = new Image();
    img.onload = () => { resolve(img); setTimeout(() => URL.revokeObjectURL(url), 1000); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('image decode failed')); };
    img.src = url;
  });
}

// Synchronous lookup for painting: the decoded image, or null while it loads.
export const imageFor = id => cache.get(id)?.img ?? null;

export function loadImage(id) {
  if (!id) return Promise.resolve(null);
  if (cache.has(id)) return Promise.resolve(cache.get(id));
  if (!pending.has(id)) pending.set(id, (async () => {
    try {
      const rec = await store.getBlob(id);
      if (!rec?.blob) return null;
      const img = await decode(rec.blob);
      const v = { img, w: rec.w || img.naturalWidth, h: rec.h || img.naturalHeight };
      cache.set(id, v);
      return v;
    } catch (err) { console.warn('[margin] image load failed', id, err); return null; }
    finally { pending.delete(id); }
  })());
  return pending.get(id);
}

// Loads every image a list of stored items refers to. Resolves true when something new arrived.
export async function loadItemImages(items = []) {
  const ids = [...new Set(items.filter(it => it.type === 'image' && it.blob && !cache.has(it.blob)).map(it => it.blob))];
  if (!ids.length) return false;
  await Promise.all(ids.map(loadImage));
  return true;
}

const toBlob = (canvas, type, q) => new Promise(res => canvas.toBlob(res, type, q));

// Stores a picture (resized when huge). Returns { id, w, h }.
export async function saveImage(blob) {
  let img = await decode(blob), w = img.naturalWidth, h = img.naturalHeight;
  if (!w || !h) throw new Error('empty image');
  if (Math.max(w, h) > MAX_DIM || blob.size > 3e6 || !/^image\/(png|jpeg|webp|gif)$/.test(blob.type)) {
    const k = Math.min(1, MAX_DIM / Math.max(w, h));
    const c = document.createElement('canvas');
    c.width = Math.round(w * k); c.height = Math.round(h * k);
    const g = c.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height);
    g.drawImage(img, 0, 0, c.width, c.height);
    blob = await toBlob(c, 'image/jpeg', 0.88);
    w = c.width; h = c.height;
    img = await decode(blob);
  }
  const id = uid();
  await store.putBlob({ id, blob, w, h });
  cache.set(id, { img, w, h });
  return { id, w, h };
}

// ---- PDF: pdf.js (vendored, Apache-2.0, web/vendor/pdfjs/LICENSE), loaded on first use --------
let pdfjs;
async function loadPdfjs() {
  if (!pdfjs) pdfjs = import('./vendor/pdfjs/pdf.min.js').then(m => {
    m.GlobalWorkerOptions.workerSrc = new URL('./vendor/pdfjs/pdf.worker.min.js', import.meta.url).href;
    return m;
  }).catch(err => { pdfjs = null; throw err; });
  return pdfjs;
}

// Renders the PDF's pages (up to maxPages) to stored images. Returns [{ id, w, h }], plus the total count.
export async function savePdfPages(file, maxPages = 12) {
  const lib = await loadPdfjs();
  const doc = await lib.getDocument({ data: new Uint8Array(await file.arrayBuffer()), isEvalSupported: false }).promise;
  const out = [], total = doc.numPages;
  try {
    for (let n = 1; n <= Math.min(doc.numPages, maxPages); n++) {
      const pg = await doc.getPage(n);
      const vp1 = pg.getViewport({ scale: 1 });
      const vp = pg.getViewport({ scale: Math.min(3, 2000 / vp1.width) });
      const c = document.createElement('canvas');
      c.width = Math.round(vp.width); c.height = Math.round(vp.height);
      const g = c.getContext('2d');
      g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height);
      await pg.render({ canvasContext: g, viewport: vp }).promise;
      out.push(await saveImage(await toBlob(c, 'image/jpeg', 0.9)));
      pg.cleanup();
    }
  } finally { doc.destroy(); }
  return { pages: out, total };
}

export const isPdf = f => f.type === 'application/pdf' || /\.pdf$/i.test(f.name || '');
