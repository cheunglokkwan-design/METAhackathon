// Left sidebar of page thumbnails: jump to a page, add, move up/down, delete.
import { el } from './ui.js';
import { drawThumb } from './export.js';

const PREF = 'margin.sidebar';
const pref = {
  get() { try { return localStorage.getItem(PREF); } catch { return null; } },
  set(v) { try { localStorage.setItem(PREF, v); } catch { /* storage blocked: fine */ } },
};

const icon = id => { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.innerHTML = `<use href="#${id}"/>`; return s; };

export class Sidebar {
  // handlers: { onSelect(i), onAdd(), onMove(i, dir), onDelete(i) }
  constructor(root, toggleBtn, handlers) {
    Object.assign(this, { root, toggleBtn, h: handlers });
    this.list = root.querySelector('.thumbs');
    root.querySelector('#add-page').onclick = () => this.h.onAdd();
    toggleBtn.onclick = () => this.toggle();
    this.setOpen(pref.get() !== 'closed' && innerWidth >= 900);
  }

  setOpen(on) {
    this.open = on;
    this.root.hidden = !on;
    this.toggleBtn.setAttribute('aria-pressed', on);
    if (on) this.redrawAll();
  }
  toggle() { this.setOpen(!this.open); pref.set(this.open ? 'open' : 'closed'); }

  // pages: page records in notebook order.
  render(pages, active) {
    this.pages = pages;
    this.list.replaceChildren(...pages.map((p, i) => {
      const canvas = el('canvas', { className: 'thumb-canvas', 'aria-hidden': 'true' });
      const btn = (id, title, fn, disabled) => {
        const b = el('button', { type: 'button', className: 'icon tiny', title, 'aria-label': title, disabled: !!disabled, onclick(e) { e.stopPropagation(); fn(); } });
        b.append(icon(id));
        return b;
      };
      return el('div', { className: 'thumb', 'data-i': i },
        el('button', { type: 'button', className: 'thumb-btn', title: `Page ${i + 1}`, 'aria-label': `Go to page ${i + 1}`, onclick: () => this.h.onSelect(i) }, canvas),
        el('div', { className: 'thumb-foot' },
          el('span', { className: 'num' }, String(i + 1)),
          el('div', { className: 'thumb-actions' },
            btn('i-up', 'Move page up', () => this.h.onMove(i, -1), i === 0),
            btn('i-down', 'Move page down', () => this.h.onMove(i, 1), i === pages.length - 1),
            btn('i-trash', 'Delete page', () => this.h.onDelete(i)))));
    }));
    this.setActive(active);
    if (this.open) this.redrawAll();
  }

  setActive(i) {
    this.active = i;
    [...this.list.children].forEach((t, k) => t.classList.toggle('active', k === i));
    this.list.children[i]?.scrollIntoView?.({ block: 'nearest' });
  }

  redrawAll() { (this.pages || []).forEach((p, i) => this.draw(i, p)); }
  draw(i, page) {
    if (this.pages) this.pages[i] = page;
    const c = this.list.children[i]?.querySelector('canvas');
    if (c && this.open) { try { drawThumb(c, page); } catch (err) { console.warn('[margin] thumbnail failed', err); } }
  }
}
