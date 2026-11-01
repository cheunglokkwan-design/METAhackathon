// Margin: wires the shelf, the notebook page, pages, Meta's ink, voice and the command pill together.
import { Notebook, PAGE_W, PAGE_H, PEN_SIZES, rowY, RULE } from './ink.js';
import { saveImage, savePdfPages, isPdf } from './images.js';
import { MetaInk, assist } from './assistant.js';
import { Voice } from './voice.js';
import { store, newPage, createNotebook } from './store.js';
import { Shelf } from './shelf.js';
import { Sidebar } from './pages.js';
import { Zoom } from './zoom.js';
import { exportPNG, exportPDF, paintPaper } from './export.js';
import { toast, confirmDialog, closePopover } from './ui.js';

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const page = $('#page'), desk = $('#desk'), sheet = $('#sheet');

const nb = new Notebook({
  page, ink: $('#ink'), live: $('#live'), paper: $('#paper'), paintPaper, scroller: desk,
  onErase: (pt, R) => meta.erase(pt, R),
  onChange: kind => { dismissCoach(); scheduleSave(); syncBg(); if (kind === 'stroke' || kind === 'text') scheduleAutoCheck(); },
  onSelect: box => showSelBar(box),
});
const meta = new MetaInk($('#meta'), nb, {
  onReveal: reveal,
  onNoteTap: () => callMeta('more please, be more specific', 'hint', { escalate: true }),
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
  $('#nb-title').textContent = book.title;
  $('#paper-title').textContent = book.title;
  document.title = `${book.title} · Margin`;
  shelf.show(false);
  document.body.classList.remove('on-shelf');
  const i = Math.max(0, pages.findIndex(p => p.id === pageId));
  await showPage(i, { force: true });
  sidebar.render(state.pages, state.idx);
}

async function showPage(i, { force = false } = {}) {
  i = clamp(i, 0, state.pages.length - 1);
  if (!force && i === state.idx) return;
  if (!force) await saveNow();
  closeCmd();
  state.idx = i;
  const p = current();
  meta.restore(p.meta);
  nb.load(p.items, histFor(p.id));
  hintMemo = null;
  setPaper(p.paper, false);
  syncBg();
  page.classList.remove('meta-hidden');
  $('[data-act="hide-meta"]').textContent = 'Hide Meta’s ink';
  $('#page-num').textContent = `${i + 1} / ${state.pages.length}`;
  $('#prev-page').disabled = i === 0;
  $('#next-page').disabled = i === state.pages.length - 1;
  sidebar.setActive(i);
  desk.scrollTo({ top: 0, left: 0 });
  history.replaceState(null, '', `#/n/${state.book.id}/${p.id}`);
}

function setPaper(paper, save = true) {
  page.dataset.paper = paper;
  nb.setPaper(paper);
  $$('[data-paper]').forEach(b => b.setAttribute('aria-pressed', b.dataset.paper === paper));
  if (save && current()) { current().paper = paper; saveNow(); }
}
$$('#paper-seg [data-paper]').forEach(b => b.onclick = () => setPaper(b.dataset.paper));

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
};
function showSelBar(box) {
  const selBar = $('#sel-bar');                     // looked up here: zoom calls this while the app is starting
  if (!box) { selBar.hidden = true; return; }
  const k = 1 / Math.max(0.3, nb.scale);
  selBar.hidden = false;
  const above = box[1] > 64 * k;
  Object.assign(selBar.style, {
    left: `${clamp(box[0], 4, PAGE_W - 330 * k)}px`, top: `${above ? box[1] - 8 : box[1] + box[3] + 8}px`,
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
  if (!e.target.closest('#cmd, #mic')) closeCmd();
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
  if (act === 'hide-meta') {
    const hidden = page.classList.toggle('meta-hidden');
    e.target.closest('button').textContent = hidden ? 'Show Meta’s ink' : 'Hide Meta’s ink';
  }
  if (act === 'export-png') exportCurrentPNG();
  if (act === 'export-pdf') exportNotebookPDF();
};
function clearMeta() { if (meta.clear(true)) { scheduleSave(); toast('Cleared Meta’s ink.', { label: 'Undo', fn: () => nb.undo() }); } }

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
let busy = false, hintMemo = null;

function nextHintLevel(lines, sig, escalate) {
  if (!hintMemo) return 1;
  const same = hintMemo.sig === sig || (hintMemo.text && lines.some(l => l.text === hintMemo.text));
  return same || escalate ? Math.min(3, hintMemo.level + 1) : 1;
}

async function callMeta(command, mode = 'auto', { escalate = false } = {}) {
  if (busy || !current()) return;
  nb.commitInput();
  dismissCoach();
  const lines = nb.lines();
  if (!lines.length && !nb.hasStrokes() && !nb.hasImages()) { toast('Write something on the page first, then ask Meta.'); return; }
  if (/^(more|more help|another hint|next hint|a bit more)\b/i.test(command.trim())) { mode = 'hint'; escalate = true; }
  const sig = nb.signature();
  const payload = { lines, command, mode, hint_level: nextHintLevel(lines, sig, escalate) };
  if (nb.hasStrokes() || nb.hasImages()) { payload.image = nb.snapshot(); payload.ink_rows = nb.inkRows(); }
  const pageId = current().id;

  busy = true;
  showReading(true);
  try {
    const resp = await assist(payload);
    showReading(false);
    if (current()?.id !== pageId) return;           // the student turned the page meanwhile
    showTrace(resp.agent_trace);
    const wrong = resp.first_wrong && ((resp.lines || []).find(l => l.id === resp.first_wrong) || lines.find(l => l.id === resp.first_wrong));
    hintMemo = resp.mode === 'hint' && wrong ? { sig, text: wrong.text, level: payload.hint_level } : null;
    nb.checkpoint();                                 // undo removes Meta's new ink
    page.classList.remove('meta-hidden');
    await meta.render(resp, lines);
    if (current()?.id === pageId) saveNow();
  } catch (err) {
    console.warn('[margin] assist failed:', err);
    toast(err.name === 'AbortError' ? 'Meta took too long. Try again in a moment.' : 'Can’t reach Meta right now. Keep writing; your page is safe.');
  } finally {
    busy = false;
    showReading(false);
  }
}

// What Meta did, as a tiny fading breadcrumb by the Hey Meta button: "Meta: read page → checked steps → wrote hint".
const plural = (n, w) => `${n} ${w}${n === '1' ? '' : 's'}`;
const TRACE_WORDS = {
  read_page: () => 'read page',
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

function showReading(on) {
  const r = $('#reading');
  $('#mic').classList.toggle('thinking', on);
  if (!on) { r.hidden = true; return; }
  const { x, y } = nb.lastPoint;
  r.style.left = `${clamp(x + 24, 110, PAGE_W - 270)}px`;
  r.style.top = `${clamp(y - 58, 70, PAGE_H - 60)}px`;
  r.hidden = false;
  reveal([clamp(x + 24, 110, PAGE_W - 270), clamp(y - 58, 70, PAGE_H - 60), 250, 40]);
}

// Optional: check automatically ~2.5 s after the pen is lifted.
let autoTimer, autoSig = '';
const autoBtn = $('#auto');
autoBtn.onclick = () => autoBtn.setAttribute('aria-pressed', autoBtn.getAttribute('aria-pressed') !== 'true');
function scheduleAutoCheck() {
  clearTimeout(autoTimer);
  if (autoBtn.getAttribute('aria-pressed') !== 'true') return;
  autoTimer = setTimeout(() => {
    const sig = nb.signature();
    if (sig === autoSig || nb.active || nb.input) return;
    autoSig = sig;
    callMeta('check my work', 'check');
  }, 2500);
}

// ---- command pill ("/") -------------------------------------------------------------------
const cmd = $('#cmd'), cmdInput = $('#cmd input');
function openCmd() {
  if (!state.book) return;
  nb.commitInput();
  cmd.hidden = false;
  $('#toast').classList.remove('show');
  cmdInput.value = '';
  requestAnimationFrame(() => cmd.classList.add('open'));
  cmdInput.focus();
}
function closeCmd() { if (cmd.hidden) return; cmd.classList.remove('open'); cmd.hidden = true; cmdInput.blur(); }
$('#cmd form').onsubmit = e => {
  e.preventDefault();
  const v = cmdInput.value.trim();
  closeCmd();
  if (v) callMeta(v, 'auto');
};
cmdInput.addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Escape') closeCmd(); });
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
const mic = $('#mic'), micLabel = $('#mic .label'), wakeBtn = $('#wake');
let heardTimer;
const short = t => (t.length > 38 ? `…${t.slice(-37)}` : t);
function idleLabel() { return voice.wake ? 'Say “Hey Meta”' : 'Hey Meta'; }
const voice = new Voice({
  onCommand: text => {
    mic.dataset.state = 'heard';
    micLabel.textContent = `“${short(text)}”`;
    clearTimeout(heardTimer);
    heardTimer = setTimeout(() => { mic.dataset.state = 'idle'; micLabel.textContent = idleLabel(); }, 2600);
    callMeta(text, 'auto');
  },
  onState: (state, text) => {
    if (mic.dataset.state === 'heard' && state === 'idle') return;
    mic.dataset.state = state;
    micLabel.textContent = state === 'listening' ? (text ? `“${short(text)}”` : 'Listening…') : state === 'transcribing' ? 'One sec…' : idleLabel();
  },
  onError: msg => toast(msg),
  onWake: on => showWake(on),                       // voice turned wake mode off by itself
});
function showWake(on) {
  wakeBtn.setAttribute('aria-pressed', on);
  mic.classList.toggle('armed', on);
  micLabel.textContent = idleLabel();
}
mic.onclick = () => voice.listenOnce();
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
  toast('Demo loaded. Press / and pick “Help me”, or tap Hey Meta.');
}

// ---- server status ------------------------------------------------------------------------------
fetch('/api/health').then(r => r.json()).then(h => {
  $('#status').textContent = h.offline ? 'Meta: offline mode (reads typed lines only)' : `Meta: online · ${h.model || ''}`;
  $('#status').dataset.ok = !h.offline;
  voice.useServer = voice.serverOk = !h.offline;   // Muse Voice Transcribe works on every browser
}).catch(() => { $('#status').textContent = 'Meta: server not reachable'; });

// ---- offline app shell ---------------------------------------------------------------------------
if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  addEventListener('load', () => navigator.serviceWorker.register('sw.js').catch(err => console.info('[margin] no service worker', err)));
}

// ---- start ------------------------------------------------------------------------------------------
(async () => {
  await store.ready();
  if (!store.persistent) $('#storage-note').textContent = 'Storage is unavailable in this browser mode, so notebooks last until you close the tab.';
  await seed();
  await route();
})();

// Exposed for debugging and tests.
window.margin = { nb, meta, zoom, state, callMeta, demo, addPage, showPage, saveNow, store, setTool, insertImage, pageFromFile };
