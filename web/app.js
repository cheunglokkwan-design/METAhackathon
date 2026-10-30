// Margin: wires the notebook page, Meta's ink, voice and the command pill together.
import { Notebook, PAGE_W, PAGE_H } from './ink.js';
import { MetaInk, assist } from './assistant.js';
import { Voice } from './voice.js';

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const page = $('#page'), desk = $('#desk'), sheet = $('#sheet');
let scale = 1;

const nb = new Notebook({
  page, ink: $('#ink'), live: $('#live'), scroller: desk,
  onErase: (pt, R) => meta.erase(pt, R),
  onChange: kind => { dismissCoach(); if (kind === 'stroke' || kind === 'text') scheduleAutoCheck(); },
});
const meta = new MetaInk($('#meta'), nb, {
  onReveal: reveal,
  onNoteTap: () => callMeta('more please, be more specific', 'hint', { escalate: true }),
});

// ---- layout: fixed 1000px page scaled to the window ------------------------------
function fit() {
  const gutter = desk.clientWidth < 700 ? 12 : 32;
  scale = Math.min(1.15, (desk.clientWidth - gutter * 2) / PAGE_W);
  sheet.style.width = `${PAGE_W * scale}px`;
  sheet.style.height = `${PAGE_H * scale}px`;
  page.style.transform = `scale(${scale})`;
  nb.setScale(scale);
}
new ResizeObserver(fit).observe(desk);
fit();

function reveal([, y, , h]) {
  const top = y * scale + sheet.offsetTop, bottom = (y + h) * scale + sheet.offsetTop;
  if (top < desk.scrollTop + 10 || bottom > desk.scrollTop + desk.clientHeight - 90)
    desk.scrollTo({ top: Math.max(0, top - desk.clientHeight * 0.35), behavior: 'smooth' });
}

// ---- toast -----------------------------------------------------------------------
let toastTimer;
function toast(msg, action) {
  const t = $('#toast');
  t.replaceChildren(msg);
  if (action) {
    const b = document.createElement('button');
    b.textContent = action.label;
    b.onclick = () => { action.fn(); t.classList.remove('show'); };
    t.append(b);
  }
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), action ? 5000 : 3600);
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
$('#undo').onclick = () => nb.undo();
$('#clear').onclick = clearPage;

function clearPage() {
  const a = nb.clear(), b = meta.clear(true);
  if (a && b) nb.mergeLast(2);
  if (a || b) toast('Page cleared.', { label: 'Undo', fn: () => nb.undo() });
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
  setMenu(false);
  if (act === 'demo') demo();
  if (act === 'clear-meta') clearMeta();
  if (act === 'hide-meta') {
    const hidden = page.classList.toggle('meta-hidden');
    e.target.closest('button').textContent = hidden ? 'Show Meta’s ink' : 'Hide Meta’s ink';
  }
};
function clearMeta() { if (meta.clear(true)) toast('Cleared Meta’s ink.', { label: 'Undo', fn: () => nb.undo() }); }

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
  if (busy) return;
  nb.commitInput();
  dismissCoach();
  const lines = nb.lines();
  if (!lines.length && !nb.hasStrokes()) { toast('Write something on the page first, then ask Meta.'); return; }
  if (/^(more|more help|another hint|next hint|a bit more)\b/i.test(command.trim())) { mode = 'hint'; escalate = true; }
  const sig = nb.signature();
  const payload = { lines, command, mode, hint_level: nextHintLevel(lines, sig, escalate) };
  if (nb.hasStrokes()) payload.image = nb.snapshot();

  busy = true;
  showReading(true);
  try {
    const resp = await assist(payload);
    showReading(false);
    const wrong = resp.first_wrong && ((resp.lines || []).find(l => l.id === resp.first_wrong) || lines.find(l => l.id === resp.first_wrong));
    hintMemo = resp.mode === 'hint' && wrong ? { sig, text: wrong.text, level: payload.hint_level } : null;
    await meta.render(resp, lines);
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
  reveal([0, clamp(y - 58, 70, PAGE_H - 60), 0, 40]);
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
  const typing = e.target.closest?.('input, textarea, [contenteditable]');
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === 'z' && !typing) { e.preventDefault(); nb.undo(); return; }
  if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); openCmd(); return; }
  if (typing || mod || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === '/') { e.preventDefault(); openCmd(); }
  else if (k === 'p') setTool('pen');
  else if (k === 'e') setTool('eraser');
  else if (k === 't') setTool('type');
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
const DEMO = ['∫ x cos(x) dx', '= x sin(x) − ∫ sin(x) dx', '= x sin(x) − cos(x) + C'];   // sign error on line 3
async function demo() {
  setMenu(false);
  nb.commitInput();
  const old = nb.items, hadMeta = meta.clear(true);
  nb.items = [];
  nb.redraw();
  desk.scrollTo({ top: 0, behavior: 'smooth' });
  let last;
  for (let i = 0; i < DEMO.length; i++) {
    last = nb.addText('', 150, i + 1);
    for (const ch of DEMO[i]) { last.text += ch; nb.prepare(last); nb.redraw(); await sleep(22); }
    await sleep(120);
  }
  nb.push(() => { nb.items = old; });
  if (hadMeta) nb.mergeLast(2);
  nb.lastPoint = { x: last.bbox[0] + last.bbox[2], y: last.bbox[1] };
  dismissCoach();
  toast('Demo loaded. Press / and pick “Help me”, or tap Hey Meta.');
}

// ---- server status ------------------------------------------------------------------------------
fetch('/api/health').then(r => r.json()).then(h => {
  $('#status').textContent = h.offline ? 'Meta: offline mode (reads typed lines only)' : `Meta: online · ${h.model || ''}`;
  $('#status').dataset.ok = !h.offline;
}).catch(() => { $('#status').textContent = 'Meta: server not reachable'; });

// Exposed for debugging and tests.
window.margin = { nb, meta, callMeta, demo };
