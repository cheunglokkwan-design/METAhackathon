// Persistence: notebooks and pages in IndexedDB, with an in-memory fallback when IndexedDB
// is missing or broken (private mode, blocked storage). Every access is wrapped in try/catch.
//
// notebook: { id, title, color, pages: [pageId, ...], created, updated }
// page:     { id, notebookId, paper: 'ruled'|'grid'|'blank', items: [...], meta: [...], updated }
// blob:     { id, blob, w, h }  photos and PDF pages placed on pages (image items point here by id)

const DB_NAME = 'margin', DB_VERSION = 2;
const STORES = ['notebooks', 'pages', 'kv', 'blobs'];

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

// ---- Cloud Auth, JWT Session Manager & Background DB Sync ------------------------------
// Accounts and notebooks can live on another server (the Railway backend with the database)
// while the page and Meta calls stay here. /api/config.js sets window.MARGIN_API_URL from the
// host's MARGIN_API_URL / NEXT_PUBLIC_API_URL; empty means this same server.
const API_BASE = (() => {
  try {
    const u = String(window.MARGIN_API_URL || '').trim().replace(/\/+$/, '');
    return /^https?:\/\/[^/]+/.test(u) ? u : '';
  } catch { return ''; }
})();
const DATA_PATH = /^\/api\/(auth|notebooks)(\/|\?|$)/;
const OFFLINE_MSG = "Can't reach the Margin server right now. Your notebooks still save on this device.";

// Account and notebook calls go to API_BASE with cookies (credentials: 'include'), and give up
// after 15 s so a sleeping backend never hangs the page. Everything else is left as it was.
function dataFetch(url, options = {}) {
  if (!DATA_PATH.test(url)) return fetch(url, options);
  const opts = { ...options, credentials: 'include' };
  delete opts._retried;
  if (!opts.signal && typeof AbortSignal !== 'undefined' && AbortSignal.timeout) opts.signal = AbortSignal.timeout(15000);
  return fetch(API_BASE + url, opts);
}

const AUTH_STORAGE_KEY = 'margin.auth.session';
let sessionState = null;
try {
  const raw = sessionStorage.getItem(AUTH_STORAGE_KEY) || localStorage.getItem(AUTH_STORAGE_KEY);
  if (raw) sessionState = JSON.parse(raw);
} catch { /* private mode or blocked storage */ }

function saveSessionState(next) {
  sessionState = next;
  try {
    if (next) localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(next));
    else localStorage.removeItem(AUTH_STORAGE_KEY);
  } catch { /* ignore */ }
}

function parseAuthError(data, status) {
  const d = data?.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d) && d.length) {
    const first = d[0];
    const field = first?.loc?.at?.(-1);
    const msg = first?.msg || 'Invalid input';
    return field && typeof field === 'string' ? `${field[0].toUpperCase() + field.slice(1)}: ${msg}` : msg;
  }
  return `Authentication error (HTTP ${status})`;
}

export const authClient = {
  get user() { return sessionState?.user || null; },
  get accessToken() { return sessionState?.access_token || null; },
  get csrfToken() { return sessionState?.csrf_token || null; },
  isAuthenticated() { return !!(sessionState?.access_token); },

  headers(extra = {}) {
    const h = { 'Content-Type': 'application/json', ...extra };
    if (this.accessToken) h['Authorization'] = `Bearer ${this.accessToken}`;
    if (this.csrfToken) h['X-CSRF-Token'] = this.csrfToken;
    return h;
  },

  async fetchWithAuth(url, options = {}) {
    const res = await dataFetch(url, { ...options, headers: this.headers(options.headers) });
    if (res.status === 401 && sessionState?.refresh_token && !options._retried) {
      const refreshed = await this.refresh();
      if (refreshed) return this.fetchWithAuth(url, { ...options, _retried: true });
    }
    return res;
  },

  async register(email, password, displayName = '') {
    const r = await dataFetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email.trim(), password, display_name: displayName.trim() }),
    }).catch(() => { throw new Error(OFFLINE_MSG); });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(parseAuthError(data, r.status));
    saveSessionState(data);
    return data.user;
  },

  async login(email, password) {
    const r = await dataFetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email.trim(), password }),
    }).catch(() => { throw new Error(OFFLINE_MSG); });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(parseAuthError(data, r.status));
    saveSessionState(data);
    return data.user;
  },

  async fetchMe() {
    if (!this.isAuthenticated()) return null;
    try {
      const r = await this.fetchWithAuth('/api/auth/me');
      if (!r.ok) return this.user;
      const { user } = await r.json();
      if (user && sessionState) saveSessionState({ ...sessionState, user: { ...sessionState.user, ...user } });
      return this.user;
    } catch {
      return this.user;
    }
  },

  async refresh() {
    if (!sessionState?.refresh_token) return false;
    try {
      const r = await dataFetch('/api/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: sessionState.refresh_token }),
      });
      if (r.status === 401 || r.status === 403) { saveSessionState(null); return false; }
      if (!r.ok) return false;  // server busy or asleep: keep the session, try again later
      const data = await r.json();
      saveSessionState({ ...sessionState, ...data });
      return true;
    } catch {
      return false;
    }
  },

  async logout() {
    try {
      if (this.isAuthenticated()) await this.fetchWithAuth('/api/auth/logout', { method: 'POST' });
    } finally {
      saveSessionState(null);
    }
  },

  async listSessions() {
    if (!this.isAuthenticated()) return [];
    const r = await this.fetchWithAuth('/api/auth/sessions');
    if (!r.ok) return [];
    return (await r.json()).sessions || [];
  },

  async revokeSession(sessionId) {
    const r = await this.fetchWithAuth(`/api/auth/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
    return r.ok;
  },
};

async function syncNotebookToCloud(nb) {
  if (!authClient.isAuthenticated() || !nb?.id) return;
  try {
    const r = await authClient.fetchWithAuth(`/api/notebooks/${encodeURIComponent(nb.id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ title: nb.title, color: nb.color, pages: nb.pages || [] }),
    });
    if (r.status === 404) {
      await authClient.fetchWithAuth('/api/notebooks', {
        method: 'POST',
        body: JSON.stringify({ id: nb.id, title: nb.title, color: nb.color, initial_page_id: nb.pages?.[0] }),
      });
      if ((nb.pages?.length || 0) > 1) {
        await authClient.fetchWithAuth(`/api/notebooks/${encodeURIComponent(nb.id)}`, {
          method: 'PATCH',
          body: JSON.stringify({ pages: nb.pages }),
        });
      }
    }
  } catch (err) { console.warn('[margin] cloud notebook sync skipped', err); }
}

async function syncPageToCloud(page) {
  if (!authClient.isAuthenticated() || !page?.id || !page?.notebookId) return;
  try {
    await authClient.fetchWithAuth(
      `/api/notebooks/${encodeURIComponent(page.notebookId)}/pages/${encodeURIComponent(page.id)}`,
      {
        method: 'PUT',
        body: JSON.stringify({ paper: page.paper || 'ruled', items: page.items || [], meta: page.meta || [] }),
      },
    );
  } catch (err) { console.warn('[margin] cloud page sync skipped', err); }
}

export const store = {
  get persistent() { return persistent; },
  ready: () => openDb(),

  listNotebooks: async () => (await all('notebooks')).sort((a, b) => (a.created ?? 0) - (b.created ?? 0)),
  getNotebook: id => get('notebooks', id),
  async putNotebook(nb) {
    const saved = await put('notebooks', { ...nb, updated: Date.now() });
    void syncNotebookToCloud(saved);
    return saved;
  },
  async deleteNotebook(id) {
    const nb = await get('notebooks', id);
    for (const pid of nb?.pages ?? []) await this.deletePage(pid);
    await del('notebooks', id);
    if (authClient.isAuthenticated()) {
      authClient.fetchWithAuth(`/api/notebooks/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
    }
  },

  getPage: id => get('pages', id),
  async putPage(page) {
    const saved = await put('pages', { ...page, updated: Date.now() });
    void syncPageToCloud(saved);
    return saved;
  },
  async deletePage(id) {
    const p = await get('pages', id);
    for (const it of p?.items ?? []) if (it.type === 'image' && it.blob) await del('blobs', it.blob);
    await del('pages', id);
    if (authClient.isAuthenticated() && p?.notebookId) {
      authClient.fetchWithAuth(`/api/notebooks/${encodeURIComponent(p.notebookId)}/pages/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
    }
  },

  getBlob: id => get('blobs', id),
  putBlob: rec => put('blobs', rec),

  async getFlag(key) { return (await get('kv', key))?.value; },
  setFlag: (key, value) => put('kv', { key, value }),

  // Pull cloud notebooks & pages into IndexedDB and push local unsynced notebooks to PostgreSQL
  async syncWithCloud() {
    if (!authClient.isAuthenticated()) return false;
    try {
      const localBooks = await this.listNotebooks();
      for (const lb of localBooks) {
        await syncNotebookToCloud(lb);
        for (const pid of lb.pages || []) {
          const lp = await this.getPage(pid);
          if (lp) await syncPageToCloud(lp);
        }
      }
      const r = await authClient.fetchWithAuth('/api/notebooks');
      if (!r.ok) return false;
      const { notebooks = [] } = await r.json();
      for (const remoteNb of notebooks) {
        const detailRes = await authClient.fetchWithAuth(`/api/notebooks/${encodeURIComponent(remoteNb.id)}?include_pages=true`);
        if (!detailRes.ok) continue;
        const { notebook: full } = await detailRes.json();
        for (const pg of full.page_records || []) {
          await put('pages', {
            id: pg.id,
            notebookId: full.id,
            paper: pg.paper || 'ruled',
            items: pg.items || [],
            meta: pg.meta || [],
            updated: pg.updated || Date.now(),
          });
        }
        await put('notebooks', {
          id: full.id,
          title: full.title,
          color: full.color,
          pages: full.pages || [],
          created: full.created || Date.now(),
          updated: full.updated || Date.now(),
        });
      }
      return true;
    } catch (err) {
      console.warn('[margin] cloud sync error', err);
      return false;
    }
  },
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
