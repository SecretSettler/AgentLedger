/* AgentLedger — dashboard: every project, report, version and review thread in one place. */
(function () {
  'use strict';
  const AL = window.AL, h = AL.h, $$ = AL.$$;
  const D = JSON.parse(document.getElementById('al-data').textContent);
  const app = document.getElementById('al-app');
  const sync = new AL.Sync(D.store);
  const state = { view: 'reports', q: '', status: 'open', project: '', kind: '', target: null };

  // Flatten the tree once: report id -> {report, project}.
  const reports = new Map();
  for (const p of D.projects) (function walk(list) { for (const r of list) { reports.set(r.id, { r, project: p }); walk(r.children || []); } })(p.reports);
  const versionOf = (rid, n) => { const e = reports.get(rid); return e && e.r.versions.find((v) => v.n === Number(n)); };

  // One Comments object per report, so local drafts from report pages show up (and can be saved) here.
  const perReport = new Map();
  const prefix = `agentledger:v1:${D.store.key}:`;
  const commentsFor = (rid) => { if (!perReport.has(rid)) perReport.set(rid, new AL.Comments(D.store.key, rid)); return perReport.get(rid); };
  for (const k of AL.storage.keys(prefix)) { const rid = k.slice(prefix.length); if (reports.has(rid)) commentsFor(rid); }
  const embedded = new Map();
  for (const row of D.comments) { if (!embedded.has(row.report)) embedded.set(row.report, []); embedded.get(row.report).push(row); }
  for (const [rid, rows] of embedded) commentsFor(rid).addRemote(rows, { silent: true });
  const allRows = () => [...perReport.values()].flatMap((c) => c.all());
  const isDirty = (row) => { const c = perReport.get(row.report); return !!c && c.isDirty(row.id); };
  const dirtyRows = () => [...perReport.values()].flatMap((c) => c.dirty());

  const iconBtn = (icon, label, onClick, cls) => h('button.al-btn' + (cls || ''), { type: 'button', onClick, html: AL.icon(icon) + `<span class="al-lbl">${AL.esc(label)}</span>` });
  const matches = (text) => !state.q || String(text || '').toLowerCase().includes(state.q);

  /* ---------------------------------------------------------- shell */
  const statsBox = h('div.al-stats');
  const tabsBox = h('div.al-viewtabs', { role: 'tablist' });
  const search = h('input.al-input', { type: 'search', placeholder: 'Search reports and threads', 'aria-label': 'Search' });
  const main = h('main');
  search.addEventListener('input', () => { state.q = search.value.trim().toLowerCase(); render(); });
  app.replaceChildren(h('div.al-dash', null,
    h('header.al-dash-head', null,
      AL.logo(56, '.al-dash-logo'),
      h('div', null, h('h1', null, D.store.name), h('p', null, 'Agent reports, their versions and the review threads on them.')),
      h('div.al-spacer'), AL.theme.button()),
    statsBox,
    h('div.al-toolbar', null, tabsBox, h('label.al-search', null, h('span', { html: AL.icon('search') }), search)),
    main));

  function threadsAll() { return AL.threads(allRows()).filter((t) => reports.has(t.root.report)); }

  function renderStats() {
    const ts = threadsAll();
    let nReports = 0, nVersions = 0;
    for (const { r } of reports.values()) { nReports++; nVersions += r.versions.length; }
    const open = ts.filter((t) => t.root.status === 'open').length;
    const closed = ts.filter((t) => t.root.status === 'resolved' || t.root.status === 'wontfix').length;
    const awaiting = ts.filter((t) => t.root.status === 'open' && (t.replies.length ? t.replies[t.replies.length - 1].role !== 'agent' : true)).length;
    const dirty = dirtyRows().length;
    const tiles = [
      [D.projects.length, 'projects'], [nReports, 'reports'], [nVersions, 'versions'],
      [open, 'open threads', open ? '.is-alert' : ''], [awaiting, 'waiting on the agent', awaiting ? '.is-alert' : ''], [closed, 'resolved'],
    ];
    if (dirty) tiles.push([dirty, 'unsaved in this browser', '.is-warn']);
    statsBox.replaceChildren(...tiles.map(([n, label, cls]) => h('div.al-stat' + (cls || ''), null, h('b', null, String(n)), h('span', null, label))));
    tabsBox.replaceChildren(...[['reports', 'Reports', nReports], ['threads', 'Review threads', open], ['activity', 'Activity', null]].map(([k, label, n]) =>
      h('button.al-viewtab' + (state.view === k ? '.is-on' : ''), { type: 'button', role: 'tab', 'aria-selected': String(state.view === k), onClick: () => { state.view = k; render(); } },
        label, n != null ? h('small', null, String(n)) : null)));
  }

  /* -------------------------------------------------------- reports */
  function reportRow(r, depth) {
    const ts = AL.threads(commentsFor(r.id).all());
    const open = ts.filter((t) => t.root.status === 'open').length;
    const openBy = {};
    ts.forEach((t) => { if (t.root.status === 'open') openBy[t.root.version] = (openBy[t.root.version] || 0) + 1; });
    const latest = r.versions[r.versions.length - 1];
    return h('div.al-rrow' + (state.target === r.id ? '.is-target' : ''), { dataset: { depth: String(depth), rid: r.id }, style: `--depth:${depth}` },
      h('div.al-rtitle', null,
        r.href ? h('a', { href: r.href, title: r.id }, r.title) : h('span', { title: r.id }, r.title),
        r.about ? h('small', null, r.about) : null,
        r.tags && r.tags.length ? h('span.al-tags', null, ...r.tags.map((t) => h('span.al-pill', null, t))) : null),
      h('div.al-rchain', null, r.versions.length ? AL.chain(r.versions, { current: latest && latest.n, r: 10, gap: 22, open: openBy, onPick: (v) => { location.href = v.href; } }) : h('span.al-muted', null, 'no versions yet')),
      h('div.al-rstate', null,
        open ? h('span.al-pill.is-open', null, `${open} open`) : ts.length ? h('span.al-pill.is-resolved', null, 'all resolved') : null,
        r.status ? h('span.al-pill.is-status', null, r.status) : null),
      h('div.al-rtime', { title: AL.fmtTime(r.updated) }, r.updated ? AL.relTime(r.updated) : ''));
  }

  function reportMatches(r) {
    if (!state.q) return true;
    if (matches(r.title) || matches(r.about) || matches(r.id) || (r.tags || []).some(matches)) return true;
    return (r.children || []).some(reportMatches);
  }

  function renderReports() {
    if (!D.projects.length) {
      return main.replaceChildren(h('div.al-empty-big', null, h('p', null, 'No reports yet.'),
        h('p', null, 'Have an agent publish one: ', h('code', null, 'agentledger publish <project>/<report> report.md'))));
    }
    const sections = [];
    for (const p of D.projects) {
      const rows = [];
      (function walk(list, depth) {
        for (const r of list) {
          if (!reportMatches(r)) continue;
          rows.push(reportRow(r, depth));
          walk(r.children || [], depth + 1);
        }
      })(p.reports, 0);
      if (!rows.length && state.q && !matches(p.title)) continue;
      const all = [];
      (function walk(list) { for (const r of list) { all.push(r); walk(r.children || []); } })(p.reports);
      const open = all.reduce((n, r) => n + AL.threads(commentsFor(r.id).all()).filter((t) => t.root.status === 'open').length, 0);
      sections.push(h('section.al-proj' + (state.target === 'project:' + p.id ? '.is-target' : ''), { id: `project-${p.id}` },
        h('div.al-proj-head', null, h('h2', null, p.title), h('span.al-proj-id', null, `projects/${p.id}`),
          h('span.al-muted', null, `${all.length} report${all.length === 1 ? '' : 's'}${open ? ` · ${open} open thread${open === 1 ? '' : 's'}` : ''}`),
          p.description ? h('p', null, p.description) : null),
        ...(rows.length ? rows : [h('div.al-rrow', null, h('span.al-muted', null, 'No matching reports'))])));
    }
    main.replaceChildren(...(sections.length ? sections : [h('div.al-empty-big', null, 'Nothing matches your search.')]));
  }

  /* -------------------------------------------------------- threads */
  function filteredThreads() {
    return threadsAll().filter((t) => {
      const r = t.root, e = reports.get(r.report);
      if (state.status === 'open' && r.status !== 'open') return false;
      if (state.status === 'waiting' && (r.status !== 'open' || (t.replies.length && t.replies[t.replies.length - 1].role === 'agent'))) return false;
      if (state.status === 'closed' && r.status === 'open') return false;
      if (state.project && e.project.id !== state.project) return false;
      if (state.kind && r.kind !== state.kind) return false;
      if (state.q && ![r.body, r.quote, r.author, r.module, e.r.title, ...t.replies.map((x) => x.body)].some(matches)) return false;
      return true;
    }).sort((a, b) => {
      const la = (a.replies[a.replies.length - 1] || a.root).updated, lb = (b.replies[b.replies.length - 1] || b.root).updated;
      return la < lb ? 1 : -1;
    });
  }

  function threadCard(t) {
    const r = t.root, e = reports.get(r.report), v = versionOf(r.report, r.version);
    const last = t.replies[t.replies.length - 1];
    const dirty = [r, ...t.replies].some(isDirty);
    return h('article.al-tcard', null,
      h('div.al-tcard-top', null,
        h('span.al-pill.is-' + r.status, null, r.status === 'wontfix' ? "won't fix" : r.status),
        r.kind ? h('span.al-pill', null, r.kind) : null,
        v ? h('a', { href: `${v.href}#c=${encodeURIComponent(r.id)}` }, e.r.title) : h('span', null, e.r.title),
        h('span', null, `v${r.version}${r.module ? ' · ' + r.module : ''}`),
        r.resolved_in ? h('span', null, `· resolved in v${r.resolved_in}`) : null,
        dirty ? h('span.al-pill.is-local', { title: 'Only in this browser' }, 'unsaved') : null),
      r.quote ? h('blockquote.al-quote', null, r.quote) : null,
      h('div.al-tcard-body', null, h('b', null, (r.author || 'anonymous') + ': '), r.body),
      last ? h('div.al-tcard-last' + (last.role === 'agent' ? '.is-agent' : ''), null, `${t.replies.length} repl${t.replies.length === 1 ? 'y' : 'ies'} · last by `, h('b', null, last.author || 'anonymous'),
        last.role === 'agent' ? ' (agent)' : '', `: ${last.body.length > 140 ? last.body.slice(0, 140) + '…' : last.body}`) : null,
      h('div.al-tcard-side', null,
        h('time', { datetime: r.created, title: AL.fmtTime(r.created) }, AL.relTime(r.created)),
        v ? h('a.al-btn.is-small', { href: `${v.href}#c=${encodeURIComponent(r.id)}`, html: AL.icon('go') + '<span class="al-lbl">open</span>' }) : null));
  }

  function renderThreads() {
    const sel = (value, opts, onChange, label) => {
      const s = h('select', { 'aria-label': label, onChange: (ev) => { onChange(ev.target.value); render(); } }, ...opts.map(([v, l]) => h('option', { value: v }, l)));
      s.value = value;
      return s;
    };
    const list = filteredThreads();
    const filters = h('div.al-filters', null,
      sel(state.status, [['open', 'Open'], ['waiting', 'Waiting on the agent'], ['closed', 'Resolved / won\'t fix'], ['all', 'All']], (v) => { state.status = v; }, 'Status'),
      sel(state.project, [['', 'All projects'], ...D.projects.map((p) => [p.id, p.title])], (v) => { state.project = v; }, 'Project'),
      sel(state.kind, [['', 'Any kind'], ...AL.KINDS.map((k) => [k, k])], (v) => { state.kind = v; }, 'Kind'),
      h('span.al-spacer'),
      iconBtn('download', 'Export CSV', () => {
        const ids = new Set(list.map((t) => t.root.id));
        AL.download(`${D.store.key}-threads.csv`, AL.rowsToCsv(allRows().filter((r) => ids.has(r.id) || ids.has(r.reply_to))));
      }, '.is-small'),
      iconBtn('bot', 'Copy for agent', () => {
        if (!list.length) return AL.toast('No threads in this view', 'error');
        const brief = AL.agentBrief(list, {
          title: `${D.store.name} (${list.length} thread${list.length === 1 ? '' : 's'})`, storeName: D.store.name,
          reportInfo: (rid) => { const e = reports.get(rid); const v = e && e.r.versions[e.r.versions.length - 1]; return e && { title: e.r.title, source: v && v.source_path }; },
          isDirty: (id) => allRows().some((r) => r.id === id && isDirty(r)),
        });
        AL.copy(brief).then((ok) => AL.toast(ok ? `Copied ${list.length} thread${list.length === 1 ? '' : 's'} as an agent brief` : 'Copy failed', ok ? '' : 'error'));
      }, '.is-small'));
    const dirty = dirtyRows();
    if (dirty.length && sync.mode !== 'none') {
      filters.append(iconBtn('save', `${sync.label()} (${dirty.length})`, saveAll, '.is-small.is-primary'));
    }
    main.replaceChildren(filters, list.length ? h('div.al-tlist', null, ...list.map(threadCard))
      : h('div.al-empty-big', null, state.status === 'open' ? 'No open threads. Open a report, select text and press Comment.' : 'No threads match.'));
  }

  async function saveAll() {
    const rows = dirtyRows();
    try {
      const saved = await sync.save(rows, (r) => { const v = versionOf(r.report, r.version); return v && v.comments_path; });
      for (const [rid, c] of perReport) {
        const mine = rows.filter((r) => r.report === rid);
        if (!mine.length) continue;
        c.markSynced(mine);
        c.addRemote(saved.filter((r) => r.report === rid), { prune: sync.mode === 'server', silent: true });
      }
      AL.toast(`Saved ${rows.length} change${rows.length === 1 ? '' : 's'}`);
      render();
    } catch (e) {
      if (e.message !== 'cancelled') AL.toast(`Save failed: ${e.message}`, 'error');
    }
  }

  /* ------------------------------------------------------- activity */
  function renderActivity() {
    const events = [];
    for (const { r } of reports.values()) {
      for (const v of r.versions) events.push({ at: v.created, icon: 'history', cls: '.is-version', href: v.href,
        title: h('span', null, h('b', null, `v${v.n}`), ' of ', h('a', { href: v.href }, r.title), ' published', v.author ? ` by ${v.author}` : ''),
        detail: v.changes || v.summary, text: `${r.title} ${v.changes} ${v.summary}` });
    }
    for (const row of allRows()) {
      if (row.status === 'deleted' || !reports.has(row.report)) continue;
      const e = reports.get(row.report), v = versionOf(row.report, row.version);
      const link = v ? h('a', { href: `${v.href}#c=${encodeURIComponent(row.reply_to || row.id)}` }, e.r.title) : e.r.title;
      const who = row.author || 'anonymous';
      events.push({ at: row.created, icon: row.role === 'agent' ? 'bot' : 'comment', cls: row.role === 'agent' ? '.is-agent' : '',
        title: h('span', null, h('b', null, who), row.reply_to ? ' replied on ' : ' commented on ', link),
        detail: row.body, text: `${who} ${row.body} ${e.r.title}` });
      if (!row.reply_to && row.status === 'resolved' && row.updated && row.updated !== row.created) {
        events.push({ at: row.updated, icon: 'check', cls: '.is-resolved',
          title: h('span', null, 'Thread resolved on ', link, row.resolved_in ? ` in v${row.resolved_in}` : ''), detail: row.body, text: `${row.body} ${e.r.title}` });
      }
    }
    const list = events.filter((e) => e.at && matches(e.text)).sort((a, b) => (a.at < b.at ? 1 : -1)).slice(0, 300);
    if (!list.length) return main.replaceChildren(h('div.al-empty-big', null, 'No activity yet.'));
    const out = [];
    let day = '';
    for (const e of list) {
      const d = AL.fmtDate(e.at);
      if (d !== day) { day = d; out.push(h('div.al-day', null, d)); }
      out.push(h('div.al-event', null,
        h('span.al-event-ico' + e.cls, { html: AL.icon(e.icon) }),
        h('div', null, e.title, e.detail ? h('small', null, e.detail.length > 220 ? e.detail.slice(0, 220) + '…' : e.detail) : null),
        h('time', { datetime: e.at, title: e.at }, AL.fmtTime(e.at).slice(11))));
    }
    main.replaceChildren(h('div.al-feed', null, ...out));
  }

  /* --------------------------------------------------------- render */
  function render() {
    renderStats();
    if (state.view === 'reports') renderReports();
    else if (state.view === 'threads') renderThreads();
    else renderActivity();
  }

  function applyHash() {
    const m = /^#(project|report)=(.+)$/.exec(location.hash);
    state.target = null;
    if (m) {
      const id = decodeURIComponent(m[2]);
      state.view = 'reports';
      state.target = m[1] === 'project' ? 'project:' + id : id;
    } else if (location.hash === '#threads') state.view = 'threads';
    else if (location.hash === '#activity') state.view = 'activity';
    render();
    if (m) {
      const el = m[1] === 'project' ? document.getElementById(`project-${decodeURIComponent(m[2])}`) : main.querySelector(`[data-rid="${CSS.escape(decodeURIComponent(m[2]))}"]`);
      if (el) el.scrollIntoView({ block: 'start' });
    }
  }

  async function refresh() {
    if (!sync.http) return;
    const jobs = [];
    for (const { r } of reports.values()) for (const v of r.versions) jobs.push(sync.fetchCsv(v.comments_href).then((rows) => ({ r, v, rows })));
    for (const { r, v, rows } of await Promise.all(jobs)) {
      if (rows && rows.length) commentsFor(r.id).addRemote(rows.map((x) => Object.assign(x, { report: x.report || r.id, version: x.version || String(v.n) })), { silent: true });
    }
    render();
  }

  window.addEventListener('hashchange', applyHash);
  applyHash();
  sync.detect().then(render);
  refresh();
  window.AgentLedger = { data: D, sync };
})();
