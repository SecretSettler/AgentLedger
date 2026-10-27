/* AgentLedger core — shared by the report viewer and the dashboard. */
(function () {
  'use strict';
  const AL = (window.AL = window.AL || {});

  AL.FIELDS = ['id', 'reply_to', 'report', 'version', 'module', 'kind', 'quote', 'prefix', 'suffix',
    'body', 'author', 'role', 'status', 'created', 'updated', 'resolved_in'];
  AL.KINDS = ['question', 'issue', 'suggestion', 'note', 'praise'];

  /* ------------------------------------------------------------- DOM */
  AL.$ = (s, r) => (r || document).querySelector(s);
  AL.$$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  function append(el, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k == null || k === false) continue;
      el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
    }
  }

  // h('button.al-btn.is-small#id', {title, onClick}, 'text', child)
  AL.h = function (spec, attrs, ...kids) {
    const m = /^([a-z0-9]*)((?:[.#][\w-]+)*)$/i.exec(spec);
    const el = document.createElement(m[1] || 'div');
    (m[2].match(/[.#][\w-]+/g) || []).forEach((t) => (t[0] === '.' ? el.classList.add(t.slice(1)) : (el.id = t.slice(1))));
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'html') el.innerHTML = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'value') el.value = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
    append(el, kids);
    return el;
  };

  AL.svg = function (tag, attrs, ...kids) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else el.setAttribute(k, v);
    }
    append(el, kids);
    return el;
  };

  AL.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const ICONS = {
    comment: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    down: '<path d="m6 9 6 6 6-6"/>',
    left: '<path d="m15 18-6-6 6-6"/>',
    right: '<path d="m9 18 6-6-6-6"/>',
    up: '<path d="M12 19V5M5 12l7-7 7 7"/>',
    map: '<path d="M9 3 3 6v15l6-3 6 3 6-3V3l-6 3-6-3z"/><path d="M9 3v15M15 6v15"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    auto: '<circle cx="12" cy="12" r="9"/><path d="M12 3v18" /><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
    copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    bot: '<rect x="3" y="8" width="18" height="12" rx="3"/><path d="M12 8V4M9 4h6"/><circle cx="8.5" cy="14" r="1.2" fill="currentColor"/><circle cx="15.5" cy="14" r="1.2" fill="currentColor"/>',
    save: '<path d="M12 13v8"/><path d="m8 17 4-4 4 4"/><path d="M20.4 14.5A5 5 0 0 0 18 5h-1.3A8 8 0 1 0 4 15.3"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    reopen: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
    trash: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    reply: '<path d="m9 17-5-5 5-5"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>',
    go: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    joystick: '<circle cx="12" cy="7" r="3.2"/><path d="M12 10.2V16"/><path d="M5 16h14a1 1 0 0 1 1 1v2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-2a1 1 0 0 1 1-1z"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    layers: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
    collapse: '<path d="m7 15 5-5 5 5"/>',
    expand: '<path d="m7 9 5 5 5-5"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
    history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
  };
  AL.icon = (name, cls) => `<svg class="al-ico${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;
  AL.iconEl = (name, cls) => { const t = document.createElement('template'); t.innerHTML = AL.icon(name, cls); return t.content.firstChild; };

  /* ------------------------------------------------------------- time */
  const pad2 = (n) => String(n).padStart(2, '0');
  AL.nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
  AL.fmtTime = function (iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  };
  AL.fmtDate = (iso) => (AL.fmtTime(iso) || '').slice(0, 10);
  AL.relTime = function (iso) {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return '';
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    if (s < 86400 * 30) return Math.floor(s / 86400) + 'd ago';
    return AL.fmtDate(iso);
  };
  AL.newId = () => 'c_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);

  /* ---------------------------------------------------------- storage */
  // Every access is guarded: storage can be missing, full or blocked.
  AL.storage = {
    get(key, dflt) {
      try { const v = window.localStorage.getItem(key); return v == null ? dflt : JSON.parse(v); } catch (e) { return dflt; }
    },
    set(key, value) {
      try { window.localStorage.setItem(key, JSON.stringify(value)); return true; } catch (e) { return false; }
    },
    remove(key) { try { window.localStorage.removeItem(key); } catch (e) { /* ignore */ } },
    keys(prefix) {
      const out = [];
      try { for (let i = 0; i < window.localStorage.length; i++) { const k = window.localStorage.key(i); if (k && k.startsWith(prefix)) out.push(k); } } catch (e) { /* ignore */ }
      return out;
    },
  };
  AL.draftKey = (storeKey, reportId) => `agentledger:v1:${storeKey}:${reportId}`;

  /* -------------------------------------------------------------- CSV */
  AL.csvParse = function (text) {
    text = String(text || '').replace(/^﻿/, '');
    const rows = [];
    let row = [], field = '', q = false, i = 0;
    while (i < text.length) {
      const c = text[i];
      if (q) {
        if (c === '"') {
          if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
          q = false; i++; continue;
        }
        field += c; i++; continue;
      }
      if (c === '"') { q = true; i++; continue; }
      if (c === ',') { row.push(field); field = ''; i++; continue; }
      if (c === '\r') { i++; continue; }
      if (c === '\n') { row.push(field); rows.push(row); row = []; field = ''; i++; continue; }
      field += c; i++;
    }
    if (field !== '' || row.length) { row.push(field); rows.push(row); }
    return rows;
  };
  const num = (v) => { v = String(v == null ? '' : v).trim().replace(/^v/i, ''); return /^\d+$/.test(v) ? String(parseInt(v, 10)) : ''; };
  AL.normRow = function (r) {
    const o = {};
    for (const f of AL.FIELDS) o[f] = r[f] == null ? '' : String(r[f]);
    o.version = num(o.version);
    o.resolved_in = num(o.resolved_in);
    if (!o.reply_to && !o.status) o.status = 'open';
    if (!o.updated) o.updated = o.created;
    if (r._synced) o._synced = true;
    return o;
  };
  AL.rowsFromCsv = function (text) {
    const t = AL.csvParse(text);
    if (!t.length) return [];
    const head = t[0].map((s) => s.trim());
    return t.slice(1)
      .filter((r) => r.some((x) => x !== ''))
      .map((r) => { const o = {}; head.forEach((k, i) => (o[k] = r[i] == null ? '' : r[i])); return AL.normRow(o); })
      .filter((r) => r.id);
  };
  const cell = (v) => { v = v == null ? '' : String(v); return /[",\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  AL.sortRows = (rows) => rows.slice().sort((a, b) => (a.created < b.created ? -1 : a.created > b.created ? 1 : a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  AL.rowsToCsv = (rows) => [AL.FIELDS.join(','), ...AL.sortRows(rows).map((r) => AL.FIELDS.map((f) => cell(r[f])).join(','))].join('\n') + '\n';
  AL.mergeRows = function (base, incoming) {
    const m = new Map(base.map((r) => [r.id, AL.normRow(r)]));
    for (const r0 of incoming) {
      const r = AL.normRow(r0);
      const cur = m.get(r.id);
      if (!cur || r.updated > cur.updated) m.set(r.id, r);
    }
    return AL.sortRows([...m.values()]);
  };

  /* --------------------------------------------------------- comments */
  // remote: rows read from comments.csv (embedded at build time or fetched).
  // local:  rows edited in this browser, persisted until a reloaded page carries them.
  AL.Comments = class {
    constructor(storeKey, reportId) {
      this.key = reportId ? AL.draftKey(storeKey, reportId) : null;
      this.remote = new Map();
      this.local = new Map();
      if (this.key) for (const r of AL.storage.get(this.key, [])) this.local.set(r.id, AL.normRow(r));
      this.listeners = [];
    }
    onChange(fn) { this.listeners.push(fn); }
    emit() { this.listeners.forEach((fn) => fn()); }
    persist() { if (this.key) AL.storage.set(this.key, [...this.local.values()]); }
    addRemote(rows, { prune = true, silent = false } = {}) {
      for (const r0 of rows) {
        const r = AL.normRow(r0);
        delete r._synced;
        const cur = this.remote.get(r.id);
        if (!cur || r.updated >= cur.updated) this.remote.set(r.id, r);
      }
      if (prune) {
        for (const [id, l] of this.local) { const rem = this.remote.get(id); if (rem && rem.updated >= l.updated) this.local.delete(id); }
        this.persist();
      }
      if (!silent) this.emit();
    }
    addLocal(rows) {
      for (const r0 of rows) { const r = AL.normRow(r0); delete r._synced; this.local.set(r.id, r); }
      this.persist();
      this.emit();
    }
    put(row) {
      const r = AL.normRow(Object.assign({}, row, { updated: AL.nowIso() }));
      delete r._synced;
      this.local.set(r.id, r);
      this.persist();
      this.emit();
      return r;
    }
    get(id) {
      const l = this.local.get(id), r = this.remote.get(id);
      if (l && (!r || l.updated > r.updated)) return l;
      return r || l || null;
    }
    all() {
      const ids = new Set([...this.remote.keys(), ...this.local.keys()]);
      return AL.sortRows([...ids].map((id) => this.get(id)));
    }
    isDirty(id) {
      const l = this.local.get(id), r = this.remote.get(id);
      return !!l && !l._synced && (!r || l.updated > r.updated);
    }
    dirty() { return this.all().filter((r) => this.isDirty(r.id)); }
    markSynced(rows) {
      for (const r of rows) { const l = this.local.get(r.id); if (l && l.updated <= r.updated) l._synced = true; }
      this.persist();
      this.emit();
    }
    clearLocal() { this.local.clear(); this.persist(); this.emit(); }
  };

  AL.threads = function (rows) {
    const replies = new Map();
    for (const r of rows) if (r.reply_to && r.status !== 'deleted') {
      if (!replies.has(r.reply_to)) replies.set(r.reply_to, []);
      replies.get(r.reply_to).push(r);
    }
    return AL.sortRows(rows.filter((r) => !r.reply_to && r.status !== 'deleted'))
      .map((root) => ({ root, replies: AL.sortRows(replies.get(root.id) || []) }));
  };

  /* ------------------------------------------------------------- sync */
  AL.token = {
    mem: null,
    get() { return this.mem || AL.storage.get('agentledger:gh-token', null); },
    set(t, remember) { this.mem = t; if (remember) AL.storage.set('agentledger:gh-token', t); },
    forget() { this.mem = null; AL.storage.remove('agentledger:gh-token'); },
  };

  function b64encode(str) {
    const bytes = new TextEncoder().encode(str);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }
  function b64decode(b64) {
    const bin = atob(String(b64 || '').replace(/\s/g, ''));
    return new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
  }

  AL.Sync = class {
    constructor(store) {
      this.store = store;
      this.mode = 'none';
    }
    get http() { return /^https?:$/.test(location.protocol); }
    async detect() {
      if (this.http) {
        try {
          const r = await fetch(this.store.root + '__agentledger/ping', { cache: 'no-store' });
          if (r.ok) { const j = await r.json(); if (j && j.agentledger) return (this.mode = 'server'); }
        } catch (e) { /* not served by agentledger */ }
      }
      return (this.mode = this.store.github ? 'github' : 'none');
    }
    label() {
      return this.mode === 'server' ? 'Save to store' : this.mode === 'github' ? 'Save to GitHub' : '';
    }
    async fetchCsv(href) {
      if (!this.http) return null;
      try {
        const r = await fetch(href, { cache: 'no-store' });
        if (!r.ok) return null;
        return AL.rowsFromCsv(await r.text());
      } catch (e) { return null; }
    }
    async save(rows, pathFor) {
      if (this.mode === 'server') return this.saveServer(rows);
      if (this.mode === 'github') return this.saveGitHub(rows, pathFor);
      throw new Error('No save target: export the CSV instead.');
    }
    async saveServer(rows) {
      const r = await fetch(this.store.root + '__agentledger/comments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-AgentLedger': '1' },
        body: JSON.stringify({ rows }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) throw new Error(j.error || `server answered ${r.status}`);
      return j.rows || [];
    }
    async saveGitHub(rows, pathFor) {
      const gh = this.store.github;
      let token = AL.token.get();
      if (!token) {
        const got = await AL.askToken(gh);
        if (!got) throw new Error('cancelled');
        token = got;
      }
      const groups = new Map();
      for (const r of rows) {
        const p = pathFor(r);
        if (!p) throw new Error(`No comments.csv path for ${r.id}`);
        if (!groups.has(p)) groups.set(p, []);
        groups.get(p).push(r);
      }
      const out = [];
      for (const [path, group] of groups) out.push(...(await this.putFile(gh, token, path, group)));
      return out;
    }
    async putFile(gh, token, path, group, retry = true) {
      const url = `https://api.github.com/repos/${gh.repo}/contents/${path.split('/').map(encodeURIComponent).join('/')}`;
      const headers = { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' };
      let sha, existing = [];
      const g = await fetch(`${url}?ref=${encodeURIComponent(gh.branch)}`, { headers, cache: 'no-store' });
      if (g.status === 401 || g.status === 403) { AL.token.forget(); throw new Error(`GitHub refused the token (${g.status})`); }
      if (g.ok) { const j = await g.json(); sha = j.sha; existing = AL.rowsFromCsv(b64decode(j.content)); }
      else if (g.status !== 404) throw new Error(`GitHub read failed (${g.status})`);
      const merged = AL.mergeRows(existing, group);
      const authors = [...new Set(group.map((r) => r.author).filter(Boolean))].join(', ') || 'reviewer';
      const p = await fetch(url, {
        method: 'PUT', headers,
        body: JSON.stringify({
          message: `agentledger: ${group.length} comment update${group.length === 1 ? '' : 's'} on ${group[0].report} v${group[0].version} by ${authors}`,
          content: b64encode(AL.rowsToCsv(merged)), branch: gh.branch, sha,
        }),
      });
      if ((p.status === 409 || p.status === 422) && retry) return this.putFile(gh, token, path, group, false);
      if (!p.ok) { const j = await p.json().catch(() => ({})); throw new Error(`GitHub write failed (${p.status}${j.message ? ': ' + j.message : ''})`); }
      return merged;
    }
  };

  AL.askToken = function (gh) {
    return new Promise((resolve) => {
      const input = AL.h('input.al-input', { type: 'password', autocomplete: 'off', placeholder: 'github_pat_…' });
      const remember = AL.h('input', { type: 'checkbox' });
      const close = (val) => { back.remove(); resolve(val); };
      const ok = () => { const t = input.value.trim(); if (!t) return input.focus(); AL.token.set(t, remember.checked); close(t); };
      const back = AL.h('div.al-modal-backdrop', { onClick: (e) => { if (e.target === back) close(null); } },
        AL.h('div.al-modal', { role: 'dialog', 'aria-modal': 'true' },
          AL.h('h3', null, 'Save comments to GitHub'),
          AL.h('p', null, 'Comments are merged into ', AL.h('code', null, 'comments.csv'), ' in ', AL.h('strong', null, `${gh.repo}@${gh.branch}`),
            '. Use a fine-grained token limited to that repository with Contents: read and write.'),
          AL.h('label', null, 'Token'), input,
          AL.h('label.al-check', null, remember, AL.h('span', null, 'Remember on this device. Any page served from this same origin can read it, so leave this off on shared hosts such as githack.')),
          AL.h('div.al-modal-actions', null,
            AL.h('button.al-btn', { onClick: () => close(null) }, 'Cancel'),
            AL.h('button.al-btn.is-primary', { onClick: ok }, 'Save'))));
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok(); if (e.key === 'Escape') close(null); });
      document.body.appendChild(back);
      input.focus();
    });
  };

  /* ---------------------------------------------------- copy / export */
  AL.copy = async function (text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) {
      const ta = AL.h('textarea', { style: 'position:fixed;left:-9999px;top:0' });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
      ta.remove();
      return ok;
    }
  };
  AL.download = function (name, text, type) {
    const url = URL.createObjectURL(new Blob([text], { type: type || 'text/csv;charset=utf-8' }));
    const a = AL.h('a', { href: url, download: name });
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 0);
  };
  AL.pickFile = function (accept) {
    return new Promise((resolve) => {
      const input = AL.h('input', { type: 'file', accept: accept || '.csv,text/csv' });
      input.addEventListener('change', () => {
        const f = input.files && input.files[0];
        if (!f) return resolve(null);
        f.text().then(resolve, () => resolve(null));
      });
      input.click();
    });
  };

  // A Markdown brief an agent can act on directly (paste into Claude Code, Slack, an issue...).
  AL.agentBrief = function (threads, ctx) {
    const out = [];
    const dirty = [];
    out.push(`# Review threads — ${ctx.title}`, '');
    out.push(`${threads.length} thread${threads.length === 1 ? '' : 's'} from the ${ctx.storeName} ledger.`, '');
    const byReport = new Map();
    for (const t of threads) {
      if (!byReport.has(t.root.report)) byReport.set(t.root.report, []);
      byReport.get(t.root.report).push(t);
    }
    for (const [report, list] of byReport) {
      const info = ctx.reportInfo(report) || {};
      out.push(`## ${info.title || report}`, '', `- report id: \`${report}\``);
      if (info.source) out.push(`- latest source: \`${info.source}\``);
      out.push('');
      for (const { root, replies } of list) {
        out.push(`### ${root.id} — ${root.status}${root.kind ? ' · ' + root.kind : ''} · v${root.version}${root.module ? ' · module `' + root.module + '`' : ''}`);
        if (root.quote) out.push('', ...root.quote.split('\n').map((l) => '> ' + l));
        out.push('');
        for (const r of [root, ...replies]) {
          out.push(`- **${r.author || 'anonymous'}**${r.role === 'agent' ? ' (agent)' : ''}, ${AL.fmtTime(r.created)}: ${r.body.replace(/\n/g, '\n  ')}`);
          if (ctx.isDirty && ctx.isDirty(r.id)) dirty.push(r);
        }
        out.push('');
      }
    }
    if (dirty.length) {
      out.push('## Not yet saved to the store', '',
        'Some of these comments only exist in the reviewer\'s browser. Import them first:', '',
        '```sh', "agentledger comments import - <<'CSV'", AL.rowsToCsv(dirty).trimEnd(), 'CSV', '```', '');
    }
    out.push('## How to respond', '',
      '1. Investigate each thread against the report source and the underlying evidence.',
      '2. Answer in place: `agentledger comments reply <id> "<answer>"` (add `--resolve` when settled, `--wontfix` to decline).',
      '3. If the report itself must change: `agentledger draft <report-id> -o draft.md`, edit, then',
      '   `agentledger publish <report-id> draft.md --changes "<what changed>" --resolves <id>,<id>`.',
      '4. Commit and push the store so reviewers see the new version and replies.');
    return out.join('\n') + '\n';
  };

  /* ---------------------------------------------------------- UI bits */
  let toastTimer;
  AL.toast = function (msg, kind) {
    let t = AL.$('.al-toast');
    if (!t) { t = AL.h('div.al-toast', { role: 'status', 'aria-live': 'polite' }); document.body.appendChild(t); }
    t.textContent = msg;
    t.classList.toggle('is-error', kind === 'error');
    t.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('is-on'), kind === 'error' ? 5000 : 2600);
  };

  AL.modal = function (title, body, actions) {
    const back = AL.h('div.al-modal-backdrop');
    const close = () => { back.remove(); document.removeEventListener('keydown', onKey, true); };
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
    back.addEventListener('click', (e) => { if (e.target === back) close(); });
    const box = AL.h('div.al-modal', { role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
      AL.h('h3', null, title), body,
      AL.h('div.al-modal-actions', null, ...(actions || []).map((a) => AL.h('button.al-btn' + (a.primary ? '.is-primary' : ''), {
        onClick: () => { if (a.run && a.run() === false) return; close(); },
      }, a.label)), AL.h('button.al-btn', { onClick: close }, actions && actions.length ? 'Cancel' : 'Close')));
    back.appendChild(box);
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(back);
    const focusable = box.querySelector('.al-modal-actions .al-btn');
    if (focusable) focusable.focus();
    close.el = box;
    return close;
  };

  AL.theme = {
    order: ['auto', 'light', 'dark'],
    get() { return AL.storage.get('agentledger:theme', 'auto'); },
    apply(t) {
      if (t === 'auto') document.documentElement.removeAttribute('data-theme');
      else document.documentElement.setAttribute('data-theme', t);
    },
    button() {
      const btn = AL.h('button.al-iconbtn', { type: 'button' });
      const paint = () => { const t = this.get(); btn.innerHTML = AL.icon(t === 'auto' ? 'auto' : t === 'dark' ? 'moon' : 'sun'); btn.title = `Theme: ${t} (click to change)`; btn.setAttribute('aria-label', btn.title); };
      btn.addEventListener('click', () => { const o = this.order; const t = o[(o.indexOf(this.get()) + 1) % o.length]; AL.storage.set('agentledger:theme', t); this.apply(t); paint(); });
      paint();
      return btn;
    },
  };
  AL.theme.apply(AL.theme.get());

  // Version chain: circles joined by short strokes, drifting downwards like a path.
  AL.chain = function (versions, opts) {
    opts = opts || {};
    const r = opts.r || 11, gap = opts.gap || 16, drift = opts.drift == null ? 3 : opts.drift, padX = 6, padY = 6;
    const step = 2 * r + gap;
    const n = versions.length;
    const w = padX * 2 + n * 2 * r + Math.max(0, n - 1) * gap;
    const hgt = padY * 2 + 2 * r + Math.max(0, n - 1) * drift;
    const svg = AL.svg('svg', { class: 'al-chain', width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}`, role: 'list', 'aria-label': 'Versions' });
    const pos = (i) => [padX + r + i * step, padY + r + i * drift];
    for (let i = 0; i < n - 1; i++) {
      const [x1, y1] = pos(i), [x2, y2] = pos(i + 1);
      const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy), ux = dx / len, uy = dy / len, off = r + 4;
      svg.appendChild(AL.svg('line', { class: 'al-chain-link', x1: x1 + ux * off, y1: y1 + uy * off, x2: x2 - ux * off, y2: y2 - uy * off }));
    }
    versions.forEach((v, i) => {
      const [cx, cy] = pos(i);
      const g = AL.svg('g', {
        class: 'al-chain-node' + (v.n === opts.current ? ' is-current' : ''), tabindex: 0, role: 'listitem',
        'aria-label': `Version ${v.n}${v.n === opts.current ? ' (viewing)' : ''}`,
        onClick: () => opts.onPick && opts.onPick(v),
        onKeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); opts.onPick && opts.onPick(v); } },
      });
      g.appendChild(AL.svg('title', null, `v${v.n} · ${AL.fmtDate(v.created)}${v.author ? ' · ' + v.author : ''}${v.summary || v.changes ? '\n' + (v.changes || v.summary) : ''}`));
      if (v.n === opts.current) g.appendChild(AL.svg('circle', { class: 'al-chain-ring', cx, cy, r: r + 3.5 }));
      g.appendChild(AL.svg('circle', { class: 'al-fill', cx, cy, r, style: `fill: var(--al-node-${(v.n - 1) % 5 + 1})` }));
      g.appendChild(AL.svg('text', { x: cx, y: cy }, String(v.n)));
      if (opts.open && opts.open[v.n]) g.appendChild(AL.svg('circle', { class: 'al-chain-open', cx: cx + r * 0.75, cy: cy - r * 0.75, r: 3.6 }));
      svg.appendChild(g);
    });
    return svg;
  };
})();
