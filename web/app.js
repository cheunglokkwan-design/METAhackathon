// Margin: wires the shelf, the notebook page, pages, Meta's ink, voice and the command pill together.
import { Notebook, PAGE_W, PAGE_H, PEN_SIZES, rowY, RULE, isMeta } from './ink.js';
import { saveImage, savePdfPages, isPdf } from './images.js';
import { MetaInk, assist, META_COLOR } from './assistant.js';
import { setHandStyle } from './handwriting.js';
import { refine } from './refine.js';
import { Suggest } from './suggest.js';
import { Voice } from './voice.js';
import { store, authClient, newPage, createNotebook } from './store.js';
import { Shelf } from './shelf.js';
import { Sidebar } from './pages.js';
import { Zoom } from './zoom.js';
import { exportPNG, exportPDF, paintPaper } from './export.js';
import { toast, confirmDialog, notebookDialog, editReadDialog, closePopover } from './ui.js';

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const page = $('#page'), desk = $('#desk'), sheet = $('#sheet');

const nb = new Notebook({
  page, ink: $('#ink'), live: $('#live'), paper: $('#paper'), paintPaper, scroller: desk,
  onErase: (pt, R) => meta.erase(pt, R),
  onChange: kind => { dismissCoach(); scheduleSave(); syncBg(); if (kind === 'stroke' || kind === 'text') scheduleAutoCheck(); if (kind === 'stroke') scheduleAutoRefine(); },
  onSelect: box => showSelBar(box),
  onLongPress: it => pickMetaMark(it),
  beforeStroke: (s, { tap, ptype }) => {
    if (tap && meta.writing) { meta.skip(); return true; }                 // tap while Meta writes: finish now
    const p0 = { x: s.pts[0][0], y: s.pts[0][1] };
    const pv = !s.hl && meta.previewAt(p0);
    if (pv && tap && ptype === 'pen') { meta.decide(pv.pid, 'accept'); return true; }   // Pencil tap on the faint ink
    const old = tap && nb.metaAt(p0);
    if (old?.dim) {                                                          // tap a faded hint: bring it back
      for (const it of hintGroup(old)) { delete it.dim; delete it.below; }
      nb.redraw(); scheduleSave(); return true;
    }
    return false;
  },
  afterStroke: s => {
    const pv = !s.hl && meta.previewAt({ x: s.pts[0][0], y: s.pts[0][1] });
    if (pv) meta.dismiss(pv.pid);                                            // writing over a preview dismisses it
    if (!s.hl) fadeOldHints(s);
  },
});
// A hint's marks: its note and the circle / underline / arrow written by the same reply, so they fade
// and come back together. Kept (moved, recoloured, accepted) ink never fades.
const FADES = new Set(['note', 'circle', 'underline']);
function hintGroup(it) {
  const same = x => it.rid ? x.rid === it.rid : x.grp === it.grp;
  return nb.items.filter(x => x.by === 'meta' && FADES.has(x.mk) && !x.kept && same(x));
}
// Old hints fade (to 40%) once the student has written two more strokes just under them (within
// three rows below the hint, across its width); a tap brings one back.
function fadeOldHints(s) {
  const [sx, top, sw] = s.bbox;
  for (const note of nb.items.filter(it => it.by === 'meta' && it.mk === 'note' && !it.kept && !it.dim)) {
    const g = hintGroup(note), [gx, gy, gw, gh] = unionRect(g.map(it => it.bbox));
    const bottom = gy + gh;
    if (top < bottom - 6 || top > bottom + 3 * RULE || sx + sw < gx - 40 || sx > gx + gw + 40) continue;
    note.below = (note.below || 0) + 1;
    if (note.below >= 2) for (const it of g) it.dim = true;
  }
}
function unionRect(bs) {
  const x0 = Math.min(...bs.map(b => b[0])), y0 = Math.min(...bs.map(b => b[1]));
  return [x0, y0, Math.max(...bs.map(b => b[0] + b[2])) - x0, Math.max(...bs.map(b => b[1] + b[3])) - y0];
}
// Meta's ink colour: one of a few pencils, or 'mine' (the student's pen colour at the time Meta writes).
const META_INKS = { '#d0452b': 'Pencil red', '#5b6470': 'Graphite', '#b0337a': 'Magenta', mine: 'My pen colour' };
let metaInk = META_COLOR;
try { const v = localStorage.getItem('margin.metaInk'); if (v in META_INKS) metaInk = v; } catch { /* private mode */ }
const metaColor = () => metaInk === 'mine' ? nb.color : metaInk;
const meta = new MetaInk($('#meta'), nb, {
  onReveal: reveal,
  inkColor: metaColor,
  onRedo: ask => ask ? callMeta(ask.command, ask.mode, { hintLevel: ask.hint_level }) : callMeta('just give me the answer', 'answer'),
  onCommit: () => scheduleSave(),
  onEditRead: async (line, ask) => {
    const fixed = await editReadDialog({ text: line.text });
    if (!fixed || fixed === line.text) return;
    const base = lastReadLines.length ? lastReadLines : nb.lines();
    const overrideLines = base.some(l => l.id === line.id)
      ? base.map(l => (l.id === line.id ? { ...l, text: fixed } : l))
      : [...base, { ...line, text: fixed }];
    callMeta(ask?.command || 'check my work', ask?.mode || 'auto', { hintLevel: ask?.hint_level, overrideLines });
  },
});
nb.ext = { save: () => meta.save(), load: m => meta.load(m) };    // Meta's ink is part of undo/redo

const zoom = new Zoom({ desk, sheet, page, nb, onChange: showZoom });

function reveal(box) {
  const r = zoom.deskBox(box);
  if (r.top < desk.scrollTop + 10 || r.top + r.height > desk.scrollTop + desk.clientHeight - 90)
    desk.scrollTo({ top: Math.max(0, r.top - desk.clientHeight * 0.35), behavior: 'smooth' });
  if (r.left < desk.scrollLeft || r.left + r.width > desk.scrollLeft + desk.clientWidth)
    desk.scrollTo({ left: Math.max(0, r.left - 40), behavior: 'smooth' });
}

let zoomPillTimer;
function showZoom(z) {
  const pct = `${Math.round(z * 100)}%`;
  $('#zoom-label').textContent = pct;
  const pill = $('#zoom-pill');
  pill.textContent = pct;
  pill.hidden = Math.abs(z - 1) < 0.01;
  clearTimeout(zoomPillTimer);
}
$('#zoom-pill').onclick = () => zoom.reset();

// ---- notebook state: which notebook and page is open ------------------------------
const state = { book: null, pages: [], idx: 0 };
const histories = new Map();                      // page id -> { history, future } (undo/redo per page, this session)
const histFor = id => { if (!histories.has(id)) histories.set(id, { history: [], future: [] }); return histories.get(id); };
const current = () => state.pages[state.idx];

let saveTimer;
function scheduleSave() { clearTimeout(saveTimer); saveTimer = setTimeout(saveNow, 400); }
async function saveNow() {
  clearTimeout(saveTimer);
  const p = current();
  if (!p || !state.book) return;
  p.items = nb.serialize();
  p.meta = meta.serialize();
  try {
    await store.putPage(p);
    state.book.updated = Date.now();
    await store.putNotebook(state.book);
  } catch (err) { console.warn('[margin] save failed', err); }
  sidebar.draw(state.idx, p);
}
addEventListener('pagehide', () => saveNow());
document.addEventListener('visibilitychange', () => { if (document.hidden) saveNow(); });

async function openNotebook(id, pageId) {
  const book = await store.getNotebook(id);
  if (!book) { showShelf(); return; }
  const pages = [];
  for (const pid of book.pages || []) { const p = await store.getPage(pid); if (p) pages.push(p); }
  if (!pages.length) { const p = newPage(book.id); await store.putPage(p); pages.push(p); }
  book.pages = pages.map(p => p.id);
  Object.assign(state, { book, pages });
  if (book.metaInk in META_INKS) metaInk = book.metaInk;              // Meta's colour follows the notebook
  showMetaInk();
  $('#nb-title').textContent = book.title;
  $('#paper-title').textContent = book.title;
  document.title = `${book.title} · Margin`;
  shelf.show(false);
  document.body.classList.remove('on-shelf');
  const i = Math.max(0, pages.findIndex(p => p.id === pageId));
  await showPage(i, { force: true });
  sidebar.render(state.pages, state.idx);
  firstRunTip();
}

// Once per browser, on a new notebook's empty first page, Meta writes how to use it in the margin.
async function firstRunTip() {
  try { if (localStorage.getItem('margin.tipShown')) return; } catch { return; }
  if (state.idx !== 0 || nb.items.length || current()?.meta?.length) return;
  try { localStorage.setItem('margin.tipShown', '1'); } catch { return; }
  const pageId = current().id;
  await sleep(500);
  if (current()?.id !== pageId || nb.items.length) return;
  if (await meta.writeTip() && current()?.id === pageId) saveNow();
}

async function showPage(i, { force = false } = {}) {
  i = clamp(i, 0, state.pages.length - 1);
  if (!force && i === state.idx) return;
  if (!force) await saveNow();
  closeCmd();
  suggest.cancel();
  state.idx = i;
  const p = current();
  const { live, items } = meta.split(p.meta);        // older pages: finished marks become notebook items
  meta.restore(live);
  nb.load([...(p.items || []), ...items], histFor(p.id));
  hintMemo = null;
  setPaper(p.paper, false);
  syncBg();
  syncPageDate(p);
  setMetaHidden(false);
  $('#page-num').textContent = `${i + 1} / ${state.pages.length}`;
  $('#prev-page').disabled = i === 0;
  $('#next-page').disabled = i === state.pages.length - 1;
  sidebar.setActive(i);
  desk.scrollTo({ top: 0, left: 0 });
  history.replaceState(null, '', `#/n/${state.book.id}/${p.id}`);
}

function syncPageDate(p = current()) {
  const el = $('#paper-date');
  if (!el) return;
  el.textContent = p?.date ? `Date ${p.date}` : 'Date ____________';
  el.classList.toggle('stamped', !!p?.date);
}
$('#paper-date')?.addEventListener('click', () => {
  const p = current();
  if (!p) return;
  const today = new Date().toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  p.date = p.date === today ? '' : today;
  syncPageDate(p);
  scheduleSave();
  if (p.date) toast(`Stamped ${p.date} on this page.`);
});
$('#paper-date')?.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#paper-date').click(); } });
$('#paper-title')?.addEventListener('click', async () => {
  if (!state.book) return;
  const v = await notebookDialog({ heading: 'Rename notebook', title: state.book.title, color: state.book.color, submit: 'Save' });
  if (!v) return;
  Object.assign(state.book, v);
  $('#nb-title').textContent = state.book.title;
  $('#paper-title').textContent = state.book.title;
  document.title = `${state.book.title} · Margin`;
  await store.putNotebook(state.book);
});

function setPaper(paper, save = true) {
  page.dataset.paper = paper;
  nb.setPaper(paper);
  $$('[data-paper]').forEach(b => b.setAttribute('aria-pressed', b.dataset.paper === paper));
  if (save && current()) { current().paper = paper; saveNow(); }
}
$$('#paper-seg [data-paper]').forEach(b => b.onclick = () => setPaper(b.dataset.paper));

const THEMES = new Set(['daylight', 'sepia', 'night']);
function setTheme(theme, save = true) {
  if (!THEMES.has(theme)) theme = 'daylight';
  document.documentElement.dataset.theme = theme;
  $$('#theme-seg [data-theme]').forEach(b => b.setAttribute('aria-pressed', b.dataset.theme === theme));
  nb.renderPaper();
  if (save) { try { localStorage.setItem('margin.theme', theme); } catch { /* private mode */ } }
}
try { setTheme(localStorage.getItem('margin.theme') || 'daylight', false); } catch { setTheme('daylight', false); }
$$('#theme-seg [data-theme]').forEach(b => b.onclick = e => { e.stopPropagation(); setTheme(b.dataset.theme); });

async function persistOrder() {
  state.book.pages = state.pages.map(p => p.id);
  await store.putNotebook(state.book);
  sidebar.render(state.pages, state.idx);
  $('#page-num').textContent = `${state.idx + 1} / ${state.pages.length}`;
  $('#prev-page').disabled = state.idx === 0;
  $('#next-page').disabled = state.idx === state.pages.length - 1;
}

async function addPage() {
  await saveNow();
  const p = newPage(state.book.id, current()?.paper || 'ruled');
  await store.putPage(p);
  state.pages.splice(state.idx + 1, 0, p);
  await persistOrder();
  await showPage(state.idx + 1);
  toast(`Added page ${state.idx + 1}.`);
}

async function movePage(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= state.pages.length) return;
  await saveNow();
  const cur = current();
  [state.pages[i], state.pages[j]] = [state.pages[j], state.pages[i]];
  state.idx = state.pages.indexOf(cur);
  await persistOrder();
}

async function deletePage(i) {
  await saveNow();
  const p = state.pages[i];
  const empty = !p.items?.length && !p.meta?.length;
  if (state.pages.length === 1 && empty) { toast('A notebook needs at least one page.'); return; }
  if (!empty && !(await confirmDialog({ heading: `Delete page ${i + 1}?`, body: 'Its ink and Meta’s notes are deleted. This can’t be undone.' }))) return;
  if (state.pages.length === 1) {               // last page: replace it with a fresh one
    const fresh = newPage(state.book.id, p.paper);
    await store.putPage(fresh);
    state.pages = [fresh];
  } else state.pages.splice(i, 1);
  await store.deletePage(p.id);
  histories.delete(p.id);
  const curId = i === state.idx ? null : current()?.id;
  state.idx = -1;
  await persistOrder();
  const ni = curId ? state.pages.findIndex(q => q.id === curId) : Math.min(i, state.pages.length - 1);
  await showPage(ni, { force: true });
  toast('Page deleted.');
}

const sidebar = new Sidebar($('#pages'), $('#toggle-pages'), {
  onSelect: i => showPage(i),
  onAdd: addPage,
  onMove: movePage,
  onDelete: deletePage,
});
$('#prev-page').onclick = () => showPage(state.idx - 1);
$('#next-page').onclick = () => showPage(state.idx + 1);

// ---- shelf -----------------------------------------------------------------------------
const shelf = new Shelf($('#shelf'), { onOpen: id => { location.hash = `#/n/${id}`; } });
async function showShelf() {
  if (state.book) await saveNow();
  closeCmd(); setMenu(false); closePopover();
  meta.restore([]);
  state.book = null; state.pages = [];
  document.title = 'Margin';
  document.body.classList.add('on-shelf');
  shelf.show(true);
}
$('#to-shelf').onclick = () => { location.hash = ''; };

async function route() {
  const m = /^#\/n\/([^/]+)(?:\/([^/]+))?/.exec(location.hash);
  if (!m) return showShelf();
  if (state.book?.id === m[1]) {
    const i = state.pages.findIndex(p => p.id === m[2]);
    if (i >= 0 && i !== state.idx) showPage(i);
    return;
  }
  if (state.book) await saveNow();
  return openNotebook(m[1], m[2]);
}
addEventListener('hashchange', route);

// ---- first run: a Calculus notebook with the demo problem ---------------------------------
const DEMO = ['∫ x cos(x) dx', '= x sin(x) − ∫ sin(x) dx', '= x sin(x) − cos(x) + C'];   // sign error on line 3
async function seed() {
  try {
    if (await store.getFlag('seeded')) return;
    if ((await store.listNotebooks()).length) { await store.setFlag('seeded', true); return; }
    const items = [{ type: 'textbox', x: 150, y: rowY(1) - RULE, w: 820, text: DEMO.join('\n'), color: '#1f3a7a' }];
    await createNotebook({ title: 'Calculus', color: '#2c4a8a', items });
    await createNotebook({ title: 'Physics', color: '#b8473b', paper: 'grid' });
    await store.setFlag('seeded', true);
  } catch (err) { console.warn('[margin] seeding failed', err); }
}

// ---- toolbar -----------------------------------------------------------------------
function setTool(t) {
  nb.setTool(t);
  page.dataset.tool = t;
  document.querySelector('.tools').dataset.tool = t;
  $$('[data-tool]').forEach(b => b.setAttribute('aria-pressed', b.dataset.tool === t));
}
$$('[data-tool]').forEach(b => b.onclick = () => setTool(b.dataset.tool));
$$('.swatches.pen [data-color]').forEach(b => b.onclick = () => {
  if (nb.tool === 'lasso' && nb.sel.length) { nb.recolorSelection(b.dataset.color, false); return; }
  nb.setColor(b.dataset.color);
  if (metaInk === 'mine') meta.syncColor();
  $$('.swatches.pen [data-color]').forEach(x => x.setAttribute('aria-pressed', x === b));
  if (nb.tool === 'eraser' || nb.tool === 'lasso' || nb.tool === 'highlighter') setTool('pen');
});
$$('.swatches.hl [data-color]').forEach(b => b.onclick = () => {
  nb.hlColor = b.dataset.color;
  $$('.swatches.hl [data-color]').forEach(x => x.setAttribute('aria-pressed', x === b));
  if (nb.tool !== 'highlighter') setTool('highlighter');
});

// ---- lasso selection: a small bar above the selection (recolour, delete) ------------------------
const selBar = $('#sel-bar');
selBar.addEventListener('pointerdown', e => e.stopPropagation());
selBar.onclick = e => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.recolor) nb.recolorSelection(b.dataset.recolor, b.closest('.hl') != null);
  if (b.dataset.sel === 'delete') nb.deleteSelection();
  if (b.dataset.sel === 'adopt' && nb.adoptSelection()) toast('It’s yours now: Meta will read it as your work.', { label: 'Undo', fn: () => nb.undo() });
  if (b.dataset.sel === 'refine') refineSelection();
  if (b.dataset.sel === 'convert') convertSelection();
  if (b.dataset.sel === 'more') { nb.clearSelection(); callMeta('more please, be more specific', 'hint', { escalate: true }); }
};
// ---- Tidy handwriting (like Apple Notes / Samsung Notes): a letter we're sure about is redrawn as a
// clean handwritten letter; everything else is only straightened, evened out and smoothed.
const studentStrokes = items => items.filter(it => it.type === 'stroke' && !it.hl && !isMeta(it) && it.pts?.length > 1);
let metaOnline = false, letterTipShown = false;
try { letterTipShown = localStorage.getItem('margin.letterTip') === '1'; } catch { /* private mode */ }
const tidied = new WeakSet();                       // strokes already tidied (and the letters we drew)
// Applies refine()'s result; returns the strokes that now stand for `strokes` (redrawn letters swapped in).
function applyRefined(strokes, res) {
  for (const s of strokes) { s.pts = res.pts.get(s).map(([x, y, p]) => [Math.round(x * 100) / 100, Math.round(y * 100) / 100, p]); nb.prepare(s); }
  let out = [...strokes];
  for (const rep of res.replace) {
    const at = nb.items.indexOf(rep.items[0]);
    if (at < 0 || !rep.items.every(it => nb.items.includes(it))) continue;
    const proto = rep.items[0];
    const fresh = rep.strokes.map(pts => nb.prepare({ type: 'stroke', color: proto.color, size: proto.size, sim: proto.sim,
      pts: pts.map(([x, y, p]) => [Math.round(x * 100) / 100, Math.round(y * 100) / 100, p]) }));
    fresh.forEach(f => tidied.add(f));
    nb.items = nb.items.filter(it => !rep.items.includes(it));
    nb.items.splice(Math.min(at, nb.items.length), 0, ...fresh);
    out = out.filter(it => !rep.items.includes(it)).concat(fresh);
  }
  nb.redraw(); scheduleSave();
  if (res.replace.length && !letterTipShown) {
    letterTipShown = true;
    try { localStorage.setItem('margin.letterTip', '1'); } catch { /* private mode */ }
    toast('Tidied your letters. Undo to keep them as written, or turn this off in ⋯ › Tidy my writing.', { label: 'Undo', fn: () => nb.undo() });
  }
  return out;
}
async function metaRead(strokes) {
  if (!metaOnline || !strokes.length) return null;
  try {
    const r = await fetch('/api/read', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image: nb.snapshotOf(strokes) }) });
    return r.ok ? (await r.json()).lines || null : null;
  } catch { return null; }
}
const whenIdle = fn => { const go = () => (nb.active || nb.cur ? setTimeout(go, 300) : fn()); go(); };
async function refineSelection() {
  const strokes = studentStrokes(nb.sel);
  if (!strokes.length) { toast('Select some handwriting first.'); return; }
  toast('Tidying your writing…');
  const texts = await metaRead(strokes);
  if (!strokes.every(s => nb.items.includes(s))) return;      // erased meanwhile
  nb.checkpoint();
  const res = refine(strokes, { texts });
  applyRefined(strokes, res);
  nb.clearSelection();
  toast(res.letters ? `Tidied. ${res.letters} letter${res.letters === 1 ? '' : 's'} redrawn clearly.` : 'Tidied.', { label: 'Undo', fn: () => nb.undo() });
}
// While "Tidy my writing" is on: a moment after the pen stops, tidy what was just written. Letters the
// app is sure of are redrawn at once; then Meta reads the words and the letters it is sure of are redrawn too.
let autoRefineT = null, metaBusy = false;
function scheduleAutoRefine() {
  clearTimeout(autoRefineT);
  if (!tidyOn) return;
  autoRefineT = setTimeout(() => whenIdle(async () => {
    const fresh = studentStrokes(nb.items).filter(s => !tidied.has(s) && !s.shaped);
    if (!fresh.length) return;
    fresh.forEach(s => tidied.add(s));
    nb.checkpoint();
    let group = applyRefined(fresh, refine(fresh, { strength: 0.6 }));
    if (metaBusy || group.length > 120) return;
    metaBusy = true;
    const texts = await metaRead(group);
    metaBusy = false;
    if (!texts?.length || !tidyOn) return;
    whenIdle(() => {
      group = group.filter(s => nb.items.includes(s));
      if (!group.length) return;
      const res = refine(group, { texts, strength: 0 });
      if (!res.replace.length) return;
      nb.checkpoint();
      applyRefined(group, res);
    });
  }), 1400);
}

// Lasso › To text: Meta reads the selected handwriting and it becomes a typed text box in its place.
async function convertSelection() {
  const strokes = nb.sel.filter(it => it.type === 'stroke' && !it.hl && !isMeta(it));
  if (!strokes.length) { toast('Select some handwriting first.'); return; }
  const image = nb.snapshotOf(strokes);
  const box = nb.selBox();
  toast('Meta is reading your writing…');
  let lines = [];
  try {
    const r = await fetch('/api/read', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    lines = j.lines || [];
  } catch (err) { toast(`Couldn’t read it: ${err.message}`); return; }
  if (!lines.length) { toast('Meta couldn’t read that writing.'); return; }
  nb.checkpoint();
  nb.items = nb.items.filter(it => !strokes.includes(it));
  const color = strokes[0].color || nb.color;
  const tb = nb.add({ type: 'textbox', x: box[0] + 6, y: box[1] + 6, w: Math.max(160, box[2]), text: lines.join('\n'), color });
  nb.clearSelection(); nb.setSelection?.([tb]); nb.redraw(); scheduleSave();
  toast('Turned into text.', { label: 'Undo', fn: () => nb.undo() });
}

function showSelBar(box) {
  const selBar = $('#sel-bar');                     // looked up here: zoom calls this while the app is starting
  if (!box) { selBar.hidden = true; return; }
  const metaSel = nb.sel.filter(isMeta);
  selBar.classList.toggle('has-meta', metaSel.length > 0);
  selBar.querySelector('[data-sel="more"]').hidden = !metaSel.some(it => it.mk === 'note');
  selBar.querySelector('[data-sel="delete"] span').textContent = metaSel.length && metaSel.length === nb.sel.length ? 'Remove' : 'Delete';
  const k = 1 / Math.max(0.3, nb.scale);
  selBar.hidden = false;
  const above = box[1] > 64 * k;
  Object.assign(selBar.style, {
    left: `${clamp(box[0], 4, PAGE_W - (selBar.offsetWidth || 330) * k - 4)}px`, top: `${above ? box[1] - 8 : box[1] + box[3] + 8}px`,
    transform: `scale(${k})${above ? ' translateY(-100%)' : ''}`,
  });
}

// ---- pictures: insert an image, or start a page from a photo / PDF ---------------------------
const pickImage = $('#pick-image'), pickPage = $('#pick-page');
const imgMenu = $('#img-menu'), imgBtn = $('#img-btn');
const setImgMenu = open => { imgMenu.hidden = !open; imgBtn.setAttribute('aria-expanded', open); };
imgBtn.onclick = e => { e.stopPropagation(); setImgMenu(imgMenu.hidden); };
imgMenu.onclick = e => {
  const act = e.target.closest('[data-img]')?.dataset.img;
  if (!act) return;
  setImgMenu(false);
  (act === 'insert' ? pickImage : pickPage).click();      // synchronous: iOS only opens pickers from a tap
};
pickImage.onchange = () => { const f = pickImage.files[0]; pickImage.value = ''; if (f) insertImage(f); };
pickPage.onchange = () => { const f = pickPage.files[0]; pickPage.value = ''; if (f) pageFromFile(f); };

async function insertImage(file) {
  if (!current()) return;
  nb.commitInput();
  let rec;
  try { rec = await saveImage(file); } catch (err) { console.warn('[margin] image failed', err); toast('Couldn’t open that picture.'); return; }
  const r = desk.getBoundingClientRect();
  const c = zoom.pagePoint({ x: r.left + desk.clientWidth / 2, y: r.top + desk.clientHeight / 2 });
  const k = Math.min(1, 460 / rec.w, 460 / rec.h), w = rec.w * k, h = rec.h * k;
  const x = clamp(c.x - w / 2, 20, PAGE_W - w - 20), y = clamp(c.y - h / 2, 110, PAGE_H - h - 20);
  nb.checkpoint();
  const it = nb.add({ type: 'image', blob: rec.id, x, y, w, h });
  setTool('lasso');
  nb.setSelection([it]);
  scheduleSave();
  toast('Picture added. Drag it to move; drag the corner to resize.');
}

function bgItem(rec) {
  let w = PAGE_W, h = PAGE_W * rec.h / rec.w;
  if (h > PAGE_H) { h = PAGE_H; w = PAGE_H * rec.w / rec.h; }
  return { type: 'image', blob: rec.id, x: (PAGE_W - w) / 2, y: 0, w, h, bg: true };
}
async function pageFromFile(file) {
  if (!state.book) return;
  await saveNow();
  let recs;
  try {
    if (isPdf(file)) {
      toast('Reading the PDF…');
      const out = await savePdfPages(file);
      recs = out.pages;
      if (out.total > recs.length) toast(`Added the first ${recs.length} of ${out.total} PDF pages.`);
    } else recs = [await saveImage(file)];
  } catch (err) { console.warn('[margin] page import failed', err); toast(isPdf(file) ? 'Couldn’t read that PDF.' : 'Couldn’t open that picture.'); return; }
  if (!recs.length) return;
  const at = state.idx + 1;
  const made = recs.map(rec => ({ ...newPage(state.book.id, 'blank'), items: [bgItem(rec)] }));
  for (const p of made) await store.putPage(p);
  state.pages.splice(at, 0, ...made);
  await persistOrder();
  await showPage(at);
  toast(made.length > 1 ? `Added ${made.length} pages from the PDF.` : 'New page from your picture. Write on it, then ask Meta.');
}
function syncBg() { page.classList.toggle('has-bg', nb.hasBackground()); }
$$('[data-size]').forEach(b => b.onclick = () => {
  nb.size = PEN_SIZES[b.dataset.size];
  $$('[data-size]').forEach(x => x.setAttribute('aria-pressed', x === b));
  if (nb.tool !== 'pen') setTool('pen');
});
$('#undo').onclick = () => nb.undo();
$('#redo').onclick = () => nb.redo();
$('#clear').onclick = clearPage;

function clearPage() {
  nb.commitInput();
  if (nb.isEmpty() && !meta.marks.length) return;
  nb.checkpoint();
  nb.clear(); meta.clear();
  scheduleSave();
  toast('Page cleared.', { label: 'Undo', fn: () => nb.undo() });
}

const menu = $('#menu'), more = $('#more');
const setMenu = open => { menu.hidden = !open; more.setAttribute('aria-expanded', open); };
more.onclick = e => { e.stopPropagation(); setMenu(menu.hidden); };
document.addEventListener('pointerdown', e => {
  if (!e.target.closest('.menu-wrap')) setMenu(false);
  if (!e.target.closest('.img-wrap')) setImgMenu(false);
  if (!e.target.closest('#cmd, #mic, #ask') && !voice.on && $('#mic').dataset.state !== 'transcribing') closeCmd();
});
menu.onclick = e => {
  const act = e.target.closest('[data-act]')?.dataset.act;
  if (!act) return;
  if (act.startsWith('zoom')) {                      // keep the menu open while zooming
    if (act === 'zoom-in') zoom.zoomBy(1.25);
    if (act === 'zoom-out') zoom.zoomBy(0.8);
    if (act === 'zoom-reset') zoom.reset();
    return;
  }
  setMenu(false);
  if (act === 'demo') demo();
  if (act === 'clear-meta') clearMeta();
  if (act === 'hide-meta') setMetaHidden(!nb.metaHidden);
  if (act === 'export-png') exportCurrentPNG();
  if (act === 'export-pdf') exportNotebookPDF();
};
function clearMeta() { if (meta.clear(true)) { scheduleSave(); toast('Cleared Meta’s ink.', { label: 'Undo', fn: () => nb.undo() }); } }

// Hide / show Meta's ink (menu and the toolbar eye): previews, labels and Meta's items on the canvas.
function setMetaHidden(hidden) {
  page.classList.toggle('meta-hidden', hidden);
  if (nb.metaHidden !== hidden) nb.setMetaHidden(hidden);
  $('[data-act="hide-meta"]').textContent = hidden ? 'Show Meta’s ink' : 'Hide Meta’s ink';
  $('#meta-eye').setAttribute('aria-pressed', !hidden);
  $('#meta-eye').setAttribute('aria-label', hidden ? 'Meta’s ink: hidden' : 'Meta’s ink: shown');
}
$('#meta-eye').onclick = () => setMetaHidden(!nb.metaHidden);

// Meta's ink colour (menu). Changing it recolours Meta's ink on this page in one undoable step.
function showMetaInk() {
  $$('[data-meta-ink]').forEach(b => b.setAttribute('aria-pressed', b.dataset.metaInk === metaInk));
  meta.syncColor();
}
// Tidy my writing: smooth finished strokes, and hold the pen still to snap a rough shape (like Apple Notes).
let tidyOn = true;
try { tidyOn = localStorage.getItem('margin.tidy') !== 'off'; } catch { /* private mode */ }
const SHAPE_NAMES = { line: 'a straight line', polyline: 'straight lines', circle: 'a circle', ellipse: 'an oval', rectangle: 'a rectangle', triangle: 'a triangle', quadrilateral: 'a four-sided shape' };
const applyTidy = () => { nb.tidy.smooth = nb.tidy.shapes = tidyOn; $$('[data-tidy]').forEach(b => b.setAttribute('aria-pressed', (b.dataset.tidy === 'on') === tidyOn)); };
applyTidy();
$$('[data-tidy]').forEach(b => b.onclick = () => {
  tidyOn = b.dataset.tidy === 'on'; applyTidy();
  try { localStorage.setItem('margin.tidy', tidyOn ? 'on' : 'off'); } catch { /* private mode */ }
  toast(tidyOn ? 'Letters are tidied as you write. Hold still at the end of a stroke to snap a shape.' : 'Your strokes stay exactly as drawn.');
});
let shapeTipShown = false;
try { shapeTipShown = localStorage.getItem('margin.shapeTip') === '1'; } catch { /* private mode */ }
nb.onShape = kind => {
  const what = SHAPE_NAMES[kind] || 'a shape';
  if (!shapeTipShown) {
    shapeTipShown = true;
    try { localStorage.setItem('margin.shapeTip', '1'); } catch { /* private mode */ }
    toast(`Snapped to ${what}. Lift the pen to keep it, or undo after. Turn this off in ⋯ › Tidy my writing.`);
  } else toast(`Snapped to ${what}.`);
};

// Meta's handwriting: upright print (default, easy to read) or Writey's cursive.
let handPref = 'print';
try { if (localStorage.getItem('margin.handStyle') === 'cursive') handPref = 'cursive'; } catch { /* private mode */ }
setHandStyle(handPref);
// Meta's writing size: S / M / L (a multiplier on "as big as the student's writing").
let inkSize = 0.85;
try { const v = parseFloat(localStorage.getItem('margin.inkSize')); if ([0.7, 0.85, 1].includes(v)) inkSize = v; } catch { /* private mode */ }
meta.inkScale = inkSize;
const syncSizeSeg = () => $$('[data-inksize]').forEach(b => b.setAttribute('aria-pressed', +b.dataset.inksize === inkSize));
syncSizeSeg();
$$('[data-inksize]').forEach(b => b.onclick = () => {
  inkSize = +b.dataset.inksize; meta.inkScale = inkSize; syncSizeSeg();
  try { localStorage.setItem('margin.inkSize', String(inkSize)); } catch { /* private mode */ }
  toast({ 0.7: 'Meta now writes small.', 0.85: 'Meta now writes a bit smaller than you.', 1: 'Meta now writes as big as you.' }[inkSize]);
});
const syncHandSeg = () => $$('[data-hand]').forEach(b => b.setAttribute('aria-pressed', b.dataset.hand === handPref));
syncHandSeg();
$$('[data-hand]').forEach(b => b.onclick = () => {
  handPref = b.dataset.hand; setHandStyle(handPref); syncHandSeg();
  try { localStorage.setItem('margin.handStyle', handPref); } catch { /* private mode */ }
  toast(handPref === 'print' ? 'Meta now writes in clear print.' : 'Meta now writes in cursive.');
});

$$('[data-meta-ink]').forEach(b => b.onclick = e => {
  e.stopPropagation();
  const prev = metaColor();
  metaInk = b.dataset.metaInk;
  try { localStorage.setItem('margin.metaInk', metaInk); } catch { /* private mode */ }
  if (state.book) { state.book.metaInk = metaInk; store.putNotebook(state.book).catch(() => {}); }
  const n = meta.recolor(prev, metaColor());
  showMetaInk();
  if (n) scheduleSave();
  toast(`Meta now writes in ${META_INKS[metaInk].toLowerCase()}.`, n ? { label: 'Undo', fn: () => nb.undo() } : undefined);
});

// Long-press (or right-click) on Meta's ink: pick up that whole mark with the lasso, so the bar
// offers Remove, Make it mine, a recolour and (for a hint) More help.
function pickMetaMark(it) {
  setTool('lasso');
  nb.setSelection(nb.items.filter(x => isMeta(x) && (x === it || (it.grp && x.grp === it.grp))));
}
page.addEventListener('contextmenu', e => {
  const it = nb.metaAt(zoom.pagePoint({ x: e.clientX, y: e.clientY }));
  if (it) { e.preventDefault(); pickMetaMark(it); }
});

async function exportCurrentPNG() {
  nb.commitInput();
  await saveNow();
  try { await exportPNG(current(), state.book.title, state.idx + 1); toast('Exported this page as PNG.'); }
  catch (err) { console.warn('[margin] export failed', err); toast('Couldn’t export this page.'); }
}
async function exportNotebookPDF() {
  nb.commitInput();
  await saveNow();
  toast('Making the PDF…');
  try { await exportPDF(state.pages, state.book.title); toast(`Exported ${state.pages.length} page${state.pages.length === 1 ? '' : 's'} as PDF.`); }
  catch (err) { console.warn('[margin] PDF export failed', err); toast('Couldn’t make the PDF. Try Export page as PNG.'); }
}

// ---- coach mark ----------------------------------------------------------------------
function dismissCoach() { $('#coach').classList.add('gone'); }
page.addEventListener('pointerdown', dismissCoach, { once: true });

// ---- calling Meta ------------------------------------------------------------------------
let busy = false, hintMemo = null, lastReadLines = [];
// Short-term memory: what Meta read from this page last time and the last few turns. A follow-up
// ("why?", "more", "show working") reuses it instead of rescanning, until the student's writing changes.
let readMemo = null;
const memoFor = (pageId, sig) => readMemo && readMemo.page === pageId && readMemo.sig === sig ? readMemo : null;
const touchOnly = () => matchMedia('(hover: none)').matches;

function nextHintLevel(lines, sig, escalate) {
  if (!hintMemo) return 1;
  const same = hintMemo.sig === sig || (hintMemo.text && lines.some(l => l.text === hintMemo.text));
  return same || escalate ? Math.min(3, hintMemo.level + 1) : 1;
}

let queued = null;
async function callMeta(command, mode = 'auto', { escalate = false, hintLevel = null, spare = false, overrideLines = null } = {}) {
  if (!current()) return;
  if (busy || meta.writing) {                        // Meta is busy: do it next instead of dropping it
    queued = [command, mode, { escalate, hintLevel, spare: true, overrideLines }];
    toast(`Meta is still writing. It’ll do “${command.length > 40 ? command.slice(0, 39) + '…' : command}” next.`);
    return;
  }
  nb.commitInput();
  dismissCoach();
  const lines = overrideLines || nb.lines();
  if (!lines.length && !nb.hasStrokes() && !nb.hasImages()) { toast('Write something on the page first, then ask Meta.'); return; }
  if (/^(more|more help|another hint|next hint|a bit more)\b/i.test(command.trim())) { mode = 'hint'; escalate = true; }
  const sig = nb.signature();
  suggest.cancel();
  const pageId = current().id;
  const payload = {
    lines,
    command,
    mode,
    hint_level: hintLevel || nextHintLevel(lines, sig, escalate),
    notebook_id: state.book?.id || null,
    page_id: pageId,
  };
  const memo = !overrideLines && !lines.length ? memoFor(pageId, sig) : null;
  if (memo) {
    payload.lines = memo.lines; payload.from_memory = true; payload.history = memo.history.slice(-4);
  } else if (!overrideLines && (nb.hasStrokes() || nb.hasImages())) { payload.image = nb.snapshot(); payload.ink_rows = nb.inkRows(); }

  busy = true;
  showReading(true);
  try {
    const resp = await assist(payload);
    if (current()?.id !== pageId) return;           // the student turned the page meanwhile
    lastReadLines = resp.lines?.length ? resp.lines : lines;
    const did = (resp.agent_trace || []).filter(t => t.tool !== 'read_page' && t.tool !== 'memory').map(t => t.summary).join('; ').slice(0, 160);
    if ((payload.image || memo) && resp.lines?.length) {
      const keep = memo ? memo.lines : resp.lines.map(({ id, text, bbox }) => ({ id, text, bbox: bbox || null }));
      readMemo = { page: pageId, sig, lines: keep, history: [...(memo?.history || []), { command: command || mode, did }].slice(-4) };
    } else if (!memo) readMemo = null;
    showReading('writing');
    showTrace(resp.agent_trace);
    const wrong = resp.first_wrong && ((resp.lines || []).find(l => l.id === resp.first_wrong) || lines.find(l => l.id === resp.first_wrong));
    hintMemo = resp.mode === 'hint' && wrong ? { sig, text: wrong.text, level: payload.hint_level } : null;
    nb.checkpoint();                                 // undo removes Meta's new ink
    setMetaHidden(false);
    await meta.render(resp, overrideLines ? [] : lines, { command, mode, hint_level: payload.hint_level }, { spare });
    if (current()?.id === pageId) {
      saveNow();
      // After a hint: offer "Why?" and the next step right by it.
      const note = resp.mode === 'hint' && meta.marks.findLast(m => m.kind === 'note');
      if (note && suggestOn()) { const b = note.box || note.boxes.at(-1); suggest.show(['why', 'more', 'next'], { x: b[0] + b[2] - 10, y: b[1] + b[3] + 22 }); }
    }
  } catch (err) {
    console.warn('[margin] assist failed:', err);
    toast(err.name === 'AbortError' ? 'Meta took too long. Try again in a moment.' : 'Can’t reach Meta right now. Keep writing; your page is safe.');
  } finally {
    busy = false;
    showReading(false);
    runQueued();
  }
}
// The queued command runs only once the reply before it has finished writing and is baked into the
// page, and its reply keeps that answer (render's spare) instead of replacing it.
function runQueued() {
  if (!queued) return;
  if (busy || meta.writing) { setTimeout(runQueued, 120); return; }
  const q = queued; queued = null;
  setTimeout(() => callMeta(...q), 60);
}

// What Meta did, as a tiny fading breadcrumb by the Hey Meta button: "Meta: read page → checked steps → wrote hint".
const plural = (n, w) => `${n} ${w}${n === '1' ? '' : 's'}`;
const TRACE_WORDS = {
  read_page: () => 'read page',
  memory: () => 'remembered page',
  check_steps: s => { const w = /(L\d+) wrong/.exec(s); return w ? `checked steps (${w[1]} wrong)` : 'checked steps'; },
  give_hint: () => 'wrote hint',
  finish_answer: s => { const n = /wrote (\d+)/.exec(s)?.[1]; return n ? `wrote ${plural(n, 'step')}` : 'wrote answer'; },
  typeset_lines: s => { const n = /typeset (\d+)/.exec(s)?.[1]; return n ? `typeset ${plural(n, 'line')}` : 'typeset lines'; },
  sketch_graph: s => /nothing/.test(s) ? 'nothing to graph' : 'sketched graph',
  differentiate_check: () => 'differentiated to check',
  none: () => 'nothing to check',
  route: () => '', 'muse-spark': () => '',
};
const traceWord = t => (TRACE_WORDS[t?.tool] || (() => String(t?.tool || '').replace(/_/g, ' ')))(String(t?.summary || ''));
let traceTimer;
function showTrace(trace) {
  const el = $('#trace');
  if (!Array.isArray(trace) || !trace.length) return;
  const steps = trace.map(traceWord).filter(Boolean);
  if (!steps.length) return;
  el.replaceChildren(Object.assign(document.createElement('b'), { textContent: 'Meta:' }));
  steps.slice(0, 5).forEach((s, i) => {
    if (i) el.append(Object.assign(document.createElement('i'), { textContent: '→', style: `animation-delay:${i * 0.22}s` }));
    el.append(Object.assign(document.createElement('span'), { textContent: s, style: `animation-delay:${i * 0.22}s` }));
  });
  el.title = trace.map(t => `${t.tool}: ${t.summary}`).join('\n');
  el.hidden = false;
  el.classList.remove('show'); void el.offsetWidth; el.classList.add('show');
  clearTimeout(traceTimer);
  traceTimer = setTimeout(() => { el.classList.remove('show'); traceTimer = setTimeout(() => { el.hidden = true; }, 600); }, 4000);
}

// on: true while Meta reads the page, 'writing' while it writes (tap the page to skip), false when done.
function showReading(on) {
  const r = $('#reading'), scan = $('#scan-band');
  ask.classList.toggle('thinking', !!on);
  page.classList.toggle('is-reading', on === true);
  r.querySelector('span').textContent = on === 'writing' ? 'Meta is writing… tap to skip' : 'Meta is reading your page…';
  askLabel.textContent = on === 'writing' ? 'Meta is writing…' : on ? 'Meta is reading…' : askIdle();
  if (scan) {
    if (on === true) {
      const rows = [...nb.inkRows(), ...nb.lines().map(l => l.bbox)].filter(b => b?.length === 4);
      const top = rows.length ? Math.max(90, Math.min(...rows.map(b => b[1])) - 18) : 110;
      const bot = rows.length ? Math.min(PAGE_H - 40, Math.max(...rows.map(b => b[1] + b[3])) + 24) : 360;
      Object.assign(scan.style, { top: `${top}px`, height: `${Math.max(90, bot - top)}px` });
      scan.hidden = false;
    } else scan.hidden = true;
  }
  if (!on) { r.hidden = true; return; }
  if (on === 'writing') { r.classList.add('writing'); return; }     // stays where it was, by the pen
  r.classList.remove('writing');
  const { x, y } = nb.lastPoint;
  r.style.left = `${clamp(x + 24, 110, PAGE_W - 270)}px`;
  r.style.top = `${clamp(y - 58, 70, PAGE_H - 60)}px`;
  r.hidden = false;
  reveal([clamp(x + 24, 110, PAGE_W - 270), clamp(y - 58, 70, PAGE_H - 60), 250, 40]);
}

// Pause suggestions (replaces Auto-check): ~2 s after the pen stops on a page with maths, a few
// small ranked chips appear by the pen. On by default; the toolbar toggle turns them off.
const autoBtn = $('#auto');
const suggestOn = () => autoBtn.getAttribute('aria-pressed') === 'true';
try { if (localStorage.getItem('margin.suggest') === 'off') autoBtn.setAttribute('aria-pressed', 'false'); } catch { /* private mode */ }
autoBtn.onclick = () => {
  const on = !suggestOn();
  autoBtn.setAttribute('aria-pressed', on);
  try { localStorage.setItem('margin.suggest', on ? 'on' : 'off'); } catch { /* private mode */ }
  if (!on) suggest.cancel();
};
const lastHint = () => meta.marks.some(m => m.kind === 'note' && m.note);
const suggest = new Suggest(page, {
  nb, enabled: suggestOn, hinted: lastHint,
  canShow: () => !busy && !queued && !!state.book && cmd.hidden,
  onPick: c => callMeta(c.say, c.mode),
});
function scheduleAutoCheck() { suggest.poke(); }

// ---- command pill ("/") -------------------------------------------------------------------
const cmd = $('#cmd'), cmdInput = $('#cmd input');
// focus: false when voice opens the bar, so iPad doesn't pop the keyboard over the page.
function openCmd(text = '', { focus = true } = {}) {
  if (!state.book) return;
  nb.commitInput();
  const wasOpen = !cmd.hidden;
  cmd.hidden = false;
  $('#toast').classList.remove('show');
  if (!wasOpen || text) cmdInput.value = text;
  requestAnimationFrame(() => cmd.classList.add('open'));
  if (focus) { cmdInput.focus(); if (text) cmdInput.setSelectionRange(text.length, text.length); }
}
function closeCmd() {
  if (cmd.hidden) return;
  if (voice.on) voice.stop();
  cmd.classList.remove('open'); cmd.hidden = true; cmdInput.blur(); voiceNote('');
}
function sendCmd() {
  // Meta's engine only has the words once the mic is off: stop, let them land in the bar, Enter or Send again sends.
  if (voice.on && voice.engine === 'meta') { voice.stop(); return; }
  voice.stop();
  const v = cmdInput.value.trim();
  closeCmd();
  if (v) callMeta(v, 'auto');
}
$('#cmd form').onsubmit = e => { e.preventDefault(); sendCmd(); };
cmdInput.addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Escape') closeCmd(); });
cmdInput.addEventListener('input', () => { if (voice.on && voice.engine === 'browser') voice.stop(); });   // typing takes over
$$('.chips button').forEach(b => b.onclick = () => { closeCmd(); callMeta(b.dataset.say, b.dataset.mode); });

addEventListener('keydown', e => {
  if (!state.book || !$('#modal').hidden) return;
  const typing = e.target.closest?.('input, textarea, [contenteditable]');
  const mod = e.metaKey || e.ctrlKey;
  const k = e.key.toLowerCase();
  if (mod && !typing && (k === 'y' || (k === 'z' && e.shiftKey))) { e.preventDefault(); nb.redo(); return; }
  if (mod && k === 'z' && !typing) { e.preventDefault(); nb.undo(); return; }
  if (!typing && !mod && (e.key === 'Delete' || e.key === 'Backspace') && nb.sel.length) { e.preventDefault(); nb.deleteSelection(); return; }
  if (mod && k === 'k') { e.preventDefault(); openCmd(); return; }
  if (mod && (k === '=' || k === '+')) { e.preventDefault(); zoom.zoomBy(1.25); return; }
  if (mod && k === '-') { e.preventDefault(); zoom.zoomBy(0.8); return; }
  if (mod && k === '0') { e.preventDefault(); zoom.reset(); return; }
  if (typing || mod || e.altKey) return;
  if (k === 'enter' && !cmd.hidden) { e.preventDefault(); sendCmd(); return; }
  if (k === 'enter' && meta.acceptLatest()) { e.preventDefault(); return; }     // Enter accepts Meta's preview
  if (k === '/' && voice.on) { e.preventDefault(); voice.stop(); return; }
  if (k === '/') { e.preventDefault(); openCmd(); }
  else if (k === 'p') setTool('pen');
  else if (k === 'e') setTool('eraser');
  else if (k === 't') setTool('type');
  else if (k === 'h') setTool('highlighter');
  else if (k === 'l') setTool('lasso');
  else if (e.key === 'PageDown') { e.preventDefault(); showPage(state.idx + 1); }
  else if (e.key === 'PageUp') { e.preventDefault(); showPage(state.idx - 1); }
  else if (k === 'escape') { if (!menu.hidden) setMenu(false); else if (nb.sel.length) nb.clearSelection(); else clearMeta(); }
});

// ---- voice ------------------------------------------------------------------------------------
// Two buttons: "Ask Meta" opens the command bar; the mic is its own plain on/off switch. Mic on: words go
// into the bar as you speak. Mic off: tap it again (or "/" / Enter); it also turns off after 10 s of quiet.
const mic = $('#mic'), micLabel = $('#mic .label'), wakeBtn = $('#wake');
const ask = $('#ask'), askLabel = $('#ask .label');
function askIdle() { return voice.wake ? 'Say “Hey Meta”' : 'Ask Meta'; }
let micTimer = null, micSince = 0;
function micClock(on) {
  clearInterval(micTimer);
  if (!on) return;
  micSince = Date.now();
  const tick = () => { const s = Math.floor((Date.now() - micSince) / 1000); micLabel.textContent = `Mic on · ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  tick(); micTimer = setInterval(tick, 500);
}
const vState = $('#cmd .vstate'), vEngine = $('#cmd .vengine'), vNote = $('#cmd .vnote'), vLog = $('#cmd .vlog');
function voiceNote(msg, bad = false) { vNote.textContent = msg; vNote.hidden = !msg; vNote.classList.toggle('bad', bad); }
function idleLabel() { return 'Mic off'; }
function showEngine() {
  vEngine.hidden = !voice.canChoose;
  vEngine.textContent = voice.engine === 'browser' ? 'Use Meta transcription' : 'Use browser speech';
}
const voice = new Voice({
  onText: text => { if (voice.on || !cmd.hidden) openCmd(text, { focus: false }); },   // a late final result after Send must not reopen the bar
  onCommand: text => openCmd(text, { focus: false }),          // wake word: "Hey Meta, …" lands in the bar too
  onState: (s, detail) => {
    mic.dataset.state = s === 'on' ? 'listening' : s === 'transcribing' ? 'transcribing' : 'idle';
    mic.setAttribute('aria-pressed', s === 'on');
    micClock(s === 'on');
    if (s !== 'on') micLabel.textContent = s === 'transcribing' ? 'Transcribing…' : idleLabel();
    vState.textContent = s === 'on' ? ((voice.engine === 'browser' || /live/.test(detail)) ? '● Mic on: words appear as you speak · tap Mic to stop' : '● Mic on: Meta writes the words when you stop')
      : s === 'transcribing' ? 'Meta is transcribing…' : `Mic off · ${voice.engineName}`;
    vState.classList.toggle('live', s === 'on');
    showEngine();
    if (s === 'on') { voiceNote(''); openCmd('', { focus: false }); }
  },
  onError: msg => { voiceNote(msg, !/^Mic turned off|^Two minutes/.test(msg)); openCmd('', { focus: false }); toast(msg); },
  onLog: line => { vLog.textContent = line; },
  onWake: on => showWake(on),                       // voice turned wake mode off by itself
});
vEngine.onclick = () => { voice.setEngine(voice.engine === 'browser' ? 'meta' : 'browser'); voiceNote(`Voice now uses ${voice.engineName}.`); };
function showWake(on) {
  wakeBtn.setAttribute('aria-pressed', on);
  ask.classList.toggle('armed', on);
  askLabel.textContent = askIdle();
}
// pointerup, not click: on iPad a click can be swallowed by the page's pen handling, which left the mic stuck on.
let micDown = false;
mic.addEventListener('pointerdown', e => { e.stopPropagation(); micDown = true; });
mic.addEventListener('pointerup', e => { e.stopPropagation(); if (!micDown) return; micDown = false; voice.toggle(); });
mic.addEventListener('pointercancel', () => { micDown = false; });
mic.addEventListener('click', e => { if (e.detail === 0) voice.toggle(); });   // keyboard (Enter/Space on the button)
ask.onclick = () => (cmd.hidden ? openCmd() : closeCmd());
showMetaInk();
showEngine(); micLabel.textContent = idleLabel(); askLabel.textContent = askIdle(); vState.textContent = `Mic off · ${voice.engineName}`;
wakeBtn.onclick = () => showWake(voice.setWake(!voice.wake));

// ---- demo ----------------------------------------------------------------------------------------
async function demo() {
  setMenu(false);
  nb.commitInput();
  nb.checkpoint();                                   // one undo brings the old page back
  meta.clear();
  nb.items = [];
  nb.redraw();
  desk.scrollTo({ top: 0, behavior: 'smooth' });
  const box = nb.addText('', 150, 1);
  for (let i = 0; i < DEMO.length; i++) {
    if (i) box.text += '\n';
    for (const ch of DEMO[i]) { box.text += ch; nb.prepare(box); nb.redraw(); await sleep(22); }
    await sleep(120);
  }
  nb.lastPoint = { x: box.bbox[0] + box.bbox[2], y: box.bbox[1] + box.bbox[3] - 32 };
  dismissCoach();
  saveNow();
  toast(touchOnly() ? 'Demo loaded. Tap the mic, then ask or pick “Help me”.' : 'Demo loaded. Press / and pick “Help me”, or tap the mic and ask.');
}

// ---- server status ------------------------------------------------------------------------------
fetch('/api/health').then(r => r.json()).then(h => {
  $('#status').textContent = h.offline ? 'Meta: offline mode (reads typed lines only)' : `Meta: online · ${h.model || ''}`;
  $('#status').dataset.ok = !h.offline;
  voice.serverOk = !h.offline; voice.realtime = !!h.realtime && !!window.WebSocket;
  metaOnline = !h.offline;
}).catch(() => { $('#status').textContent = 'Meta: server not reachable'; });

// ---- offline app shell ---------------------------------------------------------------------------
if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  addEventListener('load', () => navigator.serviceWorker.register('sw.js').then(r => r.update()).catch(err => console.info('[margin] no service worker', err)));
}

// ---- start ------------------------------------------------------------------------------------------
(async () => {
  await store.ready();
  if (!store.persistent) $('#storage-note').textContent = 'Storage is unavailable in this browser mode, so notebooks last until you close the tab.';
  await seed();
  await route();
})();

// Exposed for debugging and tests.
window.margin = { nb, meta, suggest, zoom, state, callMeta, demo, addPage, showPage, saveNow, store, authClient, setTool, insertImage, pageFromFile };
