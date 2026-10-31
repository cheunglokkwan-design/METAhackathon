// The shelf (home screen): a grid of notebook covers. Create, rename, recolour, delete, open.
import { store, createNotebook } from './store.js';
import { el, notebookDialog, confirmDialog, popover, toast, COVER_COLORS } from './ui.js';

function ago(t) {
  if (!t) return '';
  const s = (Date.now() - t) / 1000;
  if (s < 60) return 'Edited just now';
  if (s < 3600) return `Edited ${Math.round(s / 60)} min ago`;
  if (s < 86400) return `Edited ${Math.round(s / 3600)} h ago`;
  const d = new Date(t);
  return `Edited ${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`;
}

export class Shelf {
  constructor(root, { onOpen }) {
    this.root = root; this.onOpen = onOpen;
    this.grid = root.querySelector('.shelf-grid');
    root.querySelector('#new-notebook').onclick = () => this.create();
  }

  show(on) { this.root.hidden = !on; if (on) this.render(); }

  async render() {
    const books = await store.listNotebooks();
    const cards = books.map(nb => {
      const n = nb.pages?.length ?? 0;
      const more = el('button', { type: 'button', className: 'icon book-more', title: 'Notebook options', 'aria-label': `Options for ${nb.title}` });
      more.innerHTML = '<svg><use href="#i-more"/></svg>';
      more.onclick = e => {
        e.stopPropagation();
        popover(more, [
          { label: 'Rename / cover colour', fn: () => this.rename(nb) },
          { label: 'Delete notebook', danger: true, fn: () => this.remove(nb) },
        ]);
      };
      return el('div', { className: 'book', 'data-id': nb.id },
        el('button', { type: 'button', className: 'cover', style: { '--cover': nb.color }, title: `Open ${nb.title}`, onclick: () => this.onOpen(nb.id) },
          el('span', { className: 'label' }, nb.title)),
        el('div', { className: 'book-meta' },
          el('div', {}, el('b', {}, nb.title), el('small', {}, `${n} page${n === 1 ? '' : 's'} · ${ago(nb.updated)}`)),
          more));
    });
    const add = el('div', { className: 'book new' },
      el('button', { type: 'button', className: 'cover add', title: 'New notebook', onclick: () => this.create() }, el('span', {}, '+')),
      el('div', { className: 'book-meta' }, el('div', {}, el('b', {}, 'New notebook'), el('small', {}, 'Ruled, grid or blank pages'))));
    this.grid.replaceChildren(...cards, add);
  }

  async create() {
    const used = new Set((await store.listNotebooks()).map(b => b.color));
    const v = await notebookDialog({ heading: 'New notebook', color: COVER_COLORS.find(c => !used.has(c)) || COVER_COLORS[0] });
    if (!v) return;
    const nb = await createNotebook(v);
    await this.render();
    this.onOpen(nb.id);
  }

  async rename(nb) {
    const v = await notebookDialog({ heading: 'Rename notebook', title: nb.title, color: nb.color, submit: 'Save' });
    if (!v) return;
    const fresh = await store.getNotebook(nb.id);
    await store.putNotebook({ ...fresh, ...v });
    this.render();
  }

  async remove(nb) {
    const n = nb.pages?.length ?? 0;
    const ok = await confirmDialog({ heading: `Delete “${nb.title}”?`, body: `This deletes ${n} page${n === 1 ? '' : 's'} and Meta’s notes on them. This can’t be undone.` });
    if (!ok) return;
    await store.deleteNotebook(nb.id);
    toast(`Deleted “${nb.title}”.`);
    this.render();
  }
}
