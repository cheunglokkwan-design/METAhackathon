// Persistence: notebooks and pages in IndexedDB, with an in-memory fallback when IndexedDB
// is missing or broken (private mode, blocked storage). Every access is wrapped in try/catch.
//
// notebook: { id, title, color, pages: [pageId, ...], created, updated }
// page:     { id, notebookId, paper: 'ruled'|'grid'|'blank', items: [...], meta: [...], updated }

const DB_NAME = 'margin', DB_VERSION = 1;
const STORES = ['notebooks', 'pages', 'kv'];

const mem = Object.fromEntries(STORES.map(s => [s, new Map()]));
let dbp = null;
let persistent = false;

function openDb() {
  if (dbp) return dbp;
  dbp = new Promise(resolve => {
    try {
      if (!('indexedDB' in window)) return resolve(null);
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        for (const s of STORES) if (!db.objectStoreNames.contains(s)) db.createObjectStore(s, { keyPath: s === 'kv' ? 'key' : 'id' });
      };
      req.onsuccess = () => { persistent = true; resolve(req.result); };
      req.onerror = () => { console.warn('[margin] IndexedDB unavailable, keeping notes in memory', req.error); resolve(null); };
      req.onblocked = () => resolve(null);
    } catch (err) {
      console.warn('[margin] IndexedDB unavailable, keeping notes in memory', err);
      resolve(null);
    }
  });
  return dbp;
}

async function tx(store, mode, fn) {
  const db = await openDb();
  if (db) {
    try {
      return await new Promise((resolve, reject) => {
        const t = db.transaction(store, mode), s = t.objectStore(store);
        let out;
        const r = fn(s);
        if (r) r.onsuccess = () => { out = r.result; };
        t.oncomplete = () => resolve(out);
        t.onerror = t.onabort = () => reject(t.error);
      });
    } catch (err) {
      console.warn('[margin] IndexedDB call failed, using memory', err);
    }
  }
  return null;
}

// Memory is always kept in sync, so a failing IndexedDB never loses the session's work.
async function put(store, value) {
  const key = value.id ?? value.key;
  mem[store].set(key, structuredClone(value));
  await tx(store, 'readwrite', s => s.put(value));
  return value;
}
async function get(store, key) {
  const v = await tx(store, 'readonly', s => s.get(key));
  if (v) return v;
  const m = mem[store].get(key);
  return m ? structuredClone(m) : null;
}
async function all(store) {
  const v = await tx(store, 'readonly', s => s.getAll());
  if (v && v.length) return v;
  return [...mem[store].values()].map(x => structuredClone(x));
}
async function del(store, key) {
  mem[store].delete(key);
  await tx(store, 'readwrite', s => s.delete(key));
}

export const uid = () => (crypto.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`);

export const store = {
  get persistent() { return persistent; },
  ready: () => openDb(),

  listNotebooks: async () => (await all('notebooks')).sort((a, b) => (a.created ?? 0) - (b.created ?? 0)),
  getNotebook: id => get('notebooks', id),
  putNotebook: nb => put('notebooks', { ...nb, updated: Date.now() }),
  async deleteNotebook(id) {
    const nb = await get('notebooks', id);
    for (const pid of nb?.pages ?? []) await del('pages', pid);
    await del('notebooks', id);
  },

  getPage: id => get('pages', id),
  putPage: page => put('pages', { ...page, updated: Date.now() }),
  deletePage: id => del('pages', id),

  async getFlag(key) { return (await get('kv', key))?.value; },
  setFlag: (key, value) => put('kv', { key, value }),
};

export function newPage(notebookId, paper = 'ruled') {
  return { id: uid(), notebookId, paper, items: [], meta: [], updated: Date.now() };
}

export async function createNotebook({ title, color, paper = 'ruled', items = [] }) {
  const id = uid(), page = { ...newPage(id, paper), items };
  const now = Date.now();
  const nb = { id, title, color, pages: [page.id], created: now, updated: now };
  await store.putPage(page);
  await store.putNotebook(nb);
  return nb;
}
