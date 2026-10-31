// Small UI helpers shared by the shelf and the notebook: toast, modal dialogs, popover menus.
const $ = s => document.querySelector(s);

let toastTimer;
export function toast(msg, action) {
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

export const COVER_COLORS = ['#2c4a8a', '#b8473b', '#3f7a5a', '#d29b2b', '#6b4fa0', '#2a8a8f', '#3a3a40', '#d0607a'];

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const k in props) {
    if (k === 'style') for (const [p, v] of Object.entries(props[k])) n.style.setProperty(p.replace(/[A-Z]/g, c => `-${c.toLowerCase()}`), v);
    else if (k.startsWith('on')) n[k] = props[k];
    else if (k in n && k !== 'list') n[k] = props[k];
    else n.setAttribute(k, props[k]);
  }
  n.append(...kids.flat().filter(k => k != null));
  return n;
}
export { el };

// Generic modal; resolves with the submitted value or null when dismissed.
function modal(build) {
  return new Promise(resolve => {
    const root = $('#modal');
    const done = v => { root.hidden = true; root.replaceChildren(); document.removeEventListener('keydown', onKey, true); resolve(v); };
    const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); done(null); } };
    const card = build(done);
    root.replaceChildren(card);
    root.hidden = false;
    root.onpointerdown = e => { if (e.target === root) done(null); };
    document.addEventListener('keydown', onKey, true);
    requestAnimationFrame(() => card.querySelector('input, button.primary')?.focus());
  });
}

// Title + cover colour (new notebook, rename).
export function notebookDialog({ heading, title = '', color = COVER_COLORS[0], submit = 'Create' }) {
  return modal(done => {
    let picked = color;
    const input = el('input', { type: 'text', value: title, placeholder: 'e.g. Calculus', maxLength: 40, autocomplete: 'off', spellcheck: false, 'aria-label': 'Notebook title' });
    const swatches = el('div', { className: 'cover-swatches', role: 'radiogroup', 'aria-label': 'Cover colour' },
      COVER_COLORS.map(c => el('button', { type: 'button', style: { '--c': c }, 'aria-pressed': String(c === picked), title: 'Cover colour', onclick(e) {
        picked = c; swatches.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', b === e.currentTarget));
        preview.style.setProperty('--cover', c);
      } })));
    const preview = el('div', { className: 'cover mini', style: { '--cover': picked } }, el('span', { className: 'label' }));
    const sync = () => { preview.querySelector('.label').textContent = input.value.trim() || 'Untitled'; };
    input.oninput = sync; sync();
    return el('form', { className: 'dialog', onsubmit(e) { e.preventDefault(); done({ title: input.value.trim() || 'Untitled', color: picked }); } },
      el('h2', {}, heading),
      el('div', { className: 'dialog-row' }, preview, el('div', { className: 'dialog-fields' }, el('label', {}, 'Title', input), el('div', { className: 'field-label' }, 'Cover'), swatches)),
      el('div', { className: 'dialog-actions' },
        el('button', { type: 'button', className: 'ghost', onclick: () => done(null) }, 'Cancel'),
        el('button', { type: 'submit', className: 'primary' }, submit)));
  });
}

export function confirmDialog({ heading, body, confirm = 'Delete', danger = true }) {
  return modal(done => el('form', { className: 'dialog small', onsubmit(e) { e.preventDefault(); done(true); } },
    el('h2', {}, heading),
    el('p', {}, body),
    el('div', { className: 'dialog-actions' },
      el('button', { type: 'button', className: 'ghost', onclick: () => done(false) }, 'Cancel'),
      el('button', { type: 'submit', className: danger ? 'primary danger' : 'primary' }, confirm))));
}

// A popover menu anchored to a button. items: [{ label, fn, danger }]
export function popover(anchor, items) {
  closePopover();
  const r = anchor.getBoundingClientRect();
  const m = el('div', { className: 'menu popover', role: 'menu' },
    items.map(it => el('button', { type: 'button', role: 'menuitem', className: it.danger ? 'danger' : '', onclick(e) { e.stopPropagation(); closePopover(); it.fn(); } }, it.label)));
  document.body.append(m);
  const w = m.offsetWidth;
  m.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, r.right - w))}px`;
  m.style.top = `${Math.min(innerHeight - m.offsetHeight - 8, r.bottom + 6)}px`;
  setTimeout(() => document.addEventListener('pointerdown', outside, true));
}
function outside(e) { if (!e.target.closest('.popover')) closePopover(); }
export function closePopover() {
  document.querySelectorAll('.popover').forEach(p => p.remove());
  document.removeEventListener('pointerdown', outside, true);
}
