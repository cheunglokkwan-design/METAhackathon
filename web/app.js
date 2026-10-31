// Margin: wires the shelf, the notebook page, pages, Meta's ink, voice and the command pill together.
import { Notebook, PAGE_W, PAGE_H, PEN_SIZES, baselineOf } from './ink.js';
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
  onChange: kind => { dismissCoach(); scheduleSave(); if (kind === 'stroke' || kind === 'text') scheduleAutoCheck(); },
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
    const items = DEMO.map((text, i) => ({ type: 'text', text, x: 150, row: i + 1, baseline: baselineOf(i + 1), color: '#1f3a7a' }));
    await createNotebook({ title: 'Calculus', color: '#2c4a8a', items });
    await createNotebook({ title: 'Physics', color: '#b8473b', paper: 'grid' });
    await store.setFlag('seeded', true);
  } catch (err) { console.warn('[margin] seeding failed', err); }
}

// ---- toolbar -----------------------------------------------------------------------
function setTool(t) {
  nb.setTool(t);
  page.dataset.tool = t;
  $$('[data-tool]').forEach(b => b.setAttribute('aria-pressed', b.dataset.tool === t));
}
$$('[data-tool]').forEach(b => b.onclick = () => setTool(b.dataset.tool));
$$('[data-color]').forEach(b => b.onclick = () => {
  nb.setColor(b.dataset.color);
  $$('[data-color]').forEach(x => x.setAttribute('aria-pressed', x === b));
  if (nb.tool === 'eraser') setTool('pen');
});
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
  if (!lines.length && !nb.hasStrokes()) { toast('Write something on the page first, then ask Meta.'); return; }
  if (/^(more|more help|another hint|next hint|a bit more)\b/i.test(command.trim())) { mode = 'hint'; escalate = true; }
  const sig = nb.signature();
  const payload = { lines, command, mode, hint_level: nextHintLevel(lines, sig, escalate) };
  if (nb.hasStrokes()) payload.image = nb.snapshot();
  const pageId = current().id;

  busy = true;
  showReading(true);
  try {
    const resp = await assist(payload);
    showReading(false);
    if (current()?.id !== pageId) return;           // the student turned the page meanwhile
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
  if (mod && k === 'k') { e.preventDefault(); openCmd(); return; }
  if (mod && (k === '=' || k === '+')) { e.preventDefault(); zoom.zoomBy(1.25); return; }
  if (mod && k === '-') { e.preventDefault(); zoom.zoomBy(0.8); return; }
  if (mod && k === '0') { e.preventDefault(); zoom.reset(); return; }
  if (typing || mod || e.altKey) return;
  if (k === '/') { e.preventDefault(); openCmd(); }
  else if (k === 'p') setTool('pen');
  else if (k === 'e') setTool('eraser');
  else if (k === 't') setTool('type');
  else if (e.key === 'PageDown') { e.preventDefault(); showPage(state.idx + 1); }
  else if (e.key === 'PageUp') { e.preventDefault(); showPage(state.idx - 1); }
  else if (k === 'escape') { if (!menu.hidden) setMenu(false); else clearMeta(); }
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
});
mic.onclick = () => voice.listenOnce();
wakeBtn.onclick = () => {
  const on = voice.setWake(!voice.wake);
  wakeBtn.setAttribute('aria-pressed', on);
  mic.classList.toggle('armed', on);
  micLabel.textContent = idleLabel();
};

// ---- demo ----------------------------------------------------------------------------------------
async function demo() {
  setMenu(false);
  nb.commitInput();
  nb.checkpoint();                                   // one undo brings the old page back
  meta.clear();
  nb.items = [];
  nb.redraw();
  desk.scrollTo({ top: 0, behavior: 'smooth' });
  let last;
  for (let i = 0; i < DEMO.length; i++) {
    last = nb.addText('', 150, i + 1);
    for (const ch of DEMO[i]) { last.text += ch; nb.prepare(last); nb.redraw(); await sleep(22); }
    await sleep(120);
  }
  nb.lastPoint = { x: last.bbox[0] + last.bbox[2], y: last.bbox[1] };
  dismissCoach();
  saveNow();
  toast('Demo loaded. Press / and pick “Help me”, or tap Hey Meta.');
}

// ---- server status ------------------------------------------------------------------------------
fetch('/api/health').then(r => r.json()).then(h => {
  $('#status').textContent = h.offline ? 'Meta: offline mode (reads typed lines only)' : `Meta: online · ${h.model || ''}`;
  $('#status').dataset.ok = !h.offline;
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
window.margin = { nb, meta, zoom, state, callMeta, demo, addPage, showPage, saveNow, store };
