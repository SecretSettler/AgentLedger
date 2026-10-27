/* AgentLedger — report viewer: modules, review comments, rail, pad, map. */
(function () {
  'use strict';
  const AL = window.AL, h = AL.h, $ = AL.$, $$ = AL.$$;
  const D = JSON.parse(document.getElementById('al-data').textContent);
  const article = document.getElementById('al-article');
  const modules = $$('.al-module', article);
  const modInfo = new Map(D.modules.map((m) => [m.id, m]));
  const V = D.version.n;
  const comments = new AL.Comments(D.store.key, D.report.id);
  const sync = new AL.Sync(D.store);
  const ui = { panel: false, filter: 'open', scope: 'all', focus: null, compose: null, kind: 'question', anchors: new Map(), active: 0, asking: new Map() };
  let reviewer = AL.storage.get('agentledger:name', '');

  const moduleById = (id) => modules.find((s) => s.dataset.module === id);
  const iconBtn = (icon, title, onClick, cls) => h('button.al-iconbtn' + (cls || ''), { type: 'button', title, 'aria-label': title, onClick, html: AL.icon(icon) });
  const btn = (icon, label, onClick, cls, title) => h('button.al-btn' + (cls || ''), { type: 'button', onClick, title: title || label, html: (icon ? AL.icon(icon) : '') + `<span class="al-lbl">${AL.esc(label)}</span>` });
  const versionOf = (n) => D.versions.find((v) => v.n === Number(n));
  const typing = (e) => { const t = e.target; return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)); };

  /* =========================================================== chrome */
  function buildTopbar() {
    const crumbs = h('nav.al-crumbs', { 'aria-label': 'Breadcrumb' });
    for (const c of D.crumbs) {
      crumbs.append(c.href ? h('a', { href: c.href }, c.title) : h('span', null, c.title), h('span.al-crumb-sep', null, '/'));
    }
    crumbs.append(h('span.al-crumb-here', null, D.report.title));
    ui.countBadge = h('span.al-badge.is-zero', null, '0');
    const commentsBtn = h('button.al-btn', { type: 'button', title: 'Review comments (P)', onClick: () => togglePanel() },
      AL.iconEl('comment'), h('span.al-lbl', null, 'Comments'), ui.countBadge);
    const bar = h('header.al-topbar', null, crumbs,
      h('div.al-top-actions', null,
        h('button.al-verbtn', { type: 'button', title: 'Versions and related reports (M)', onClick: openMap },
          `v${V}`, h('span.al-muted', null, `/${D.versions.length}`)),
        iconBtn('map', 'Project map (M)', openMap, '.al-hide-sm'),
        commentsBtn,
        iconBtn('help', 'Shortcuts (?)', showHelp, '.al-hide-sm'),
        AL.theme.button()));
    document.body.prepend(bar);
    if (V < D.latest) {
      const latest = versionOf(D.latest);
      bar.after(h('div.al-banner', { role: 'note' },
        h('span', null, `You are reading v${V}. The latest version is v${D.latest} (${AL.fmtDate(latest.created)}).`),
        h('a', { href: versionHref(latest) }, 'Open latest →')));
    }
  }

  function decorateContent() {
    $$('table', article).forEach((t) => {
      if (t.parentElement.classList.contains('al-table-wrap')) return;
      const w = h('div.al-table-wrap');
      t.replaceWith(w);
      w.appendChild(t);
    });
    $$('a[href]', article).forEach((a) => {
      if (/^https?:/i.test(a.getAttribute('href')) && !a.target) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
    });
    $$('time[datetime]', article).forEach((t) => { if (t.dateTime) t.title = AL.fmtTime(t.dateTime); });

    ui.modCount = new Map();
    modules.forEach((sec) => {
      const id = sec.dataset.module, info = modInfo.get(id) || {};
      const h2 = sec.querySelector(':scope > h2');
      const tags = h('span.al-mod-tags.al-noanchor');
      if (info.state === 'new') tags.append(h('span.al-pill.is-new', { title: `Not in v${V - 1}` }, 'new'));
      if (info.state === 'changed') tags.append(h('span.al-pill.is-changed', { title: `Text differs from v${V - 1}` }, `changed since v${V - 1}`));
      if (info.status) tags.append(h('span.al-pill.is-status', null, info.status));
      const count = h('span.al-pill.is-open', { hidden: true });
      ui.modCount.set(id, count);
      tags.append(count);
      if (h2) h2.append(tags);
      const prev = versionOf(V - 1);
      const actions = h('span.al-mod-actions.al-noanchor', null,
        iconBtn('comment', 'Comment on this module', () => startCompose({ module: id })),
        iconBtn('link', 'Copy link to this module', () => {
          AL.copy(location.href.split('#')[0] + '#' + id).then((ok) => AL.toast(ok ? 'Link copied' : 'Copy failed', ok ? '' : 'error'));
        }),
        info.state === 'changed' && prev ? iconBtn('history', `Open this module in v${V - 1}`, () => { location.href = prev.href + '#' + id; }) : null,
        iconBtn('collapse', 'Collapse / expand', () => toggleCollapse(sec), '.al-act-collapse'));
      sec.appendChild(actions);
    });
  }

  function toggleCollapse(sec) {
    sec.classList.toggle('is-collapsed');
    relayout();
  }

  /* ===================================================== text anchors */
  function textIndex(root) {
    const nodes = [];
    let text = '';
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        const p = n.parentElement;
        return !p || p.closest('.al-noanchor, script, style') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      },
    });
    while (walker.nextNode()) {
      const n = walker.currentNode;
      nodes.push({ node: n, start: text.length });
      text += n.nodeValue;
    }
    return { nodes, text };
  }

  function rangeOffsets(range, root) {
    const { nodes, text } = textIndex(root);
    let s = -1, e = -1;
    for (const { node, start } of nodes) {
      if (!range.intersectsNode(node)) continue;
      const a = node === range.startContainer ? range.startOffset : 0;
      const b = node === range.endContainer ? range.endOffset : node.nodeValue.length;
      if (b <= a) continue;
      if (s < 0) s = start + a;
      e = start + b;
    }
    if (s < 0) return null;
    while (s < e && /\s/.test(text[s])) s++;
    while (e > s && /\s/.test(text[e - 1])) e--;
    return e > s ? { start: s, end: e, text } : null;
  }

  const commonSuffix = (a, b) => { let n = 0; while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n++; return n; };
  const commonPrefix = (a, b) => { let n = 0; while (n < a.length && n < b.length && a[n] === b[n]) n++; return n; };

  function locate(text, quote, prefix, suffix) {
    if (!quote) return null;
    prefix = prefix || ''; suffix = suffix || '';
    let best = null, bestScore = -1, i = text.indexOf(quote);
    while (i !== -1) {
      const score = commonSuffix(text.slice(Math.max(0, i - prefix.length), i), prefix) +
        commonPrefix(text.slice(i + quote.length, i + quote.length + suffix.length), suffix);
      if (score > bestScore) { bestScore = score; best = i; }
      i = text.indexOf(quote, i + 1);
    }
    if (best !== null) return { start: best, end: best + quote.length };
    // Fall back to a whitespace-insensitive match (re-wrapped lines, changed indentation).
    const map = [];
    let norm = '';
    for (let k = 0; k < text.length; k++) {
      const ws = /\s/.test(text[k]);
      if (ws && norm.endsWith(' ')) continue;
      norm += ws ? ' ' : text[k];
      map.push(k);
    }
    const nq = quote.replace(/\s+/g, ' ').trim();
    const j = nq ? norm.indexOf(nq) : -1;
    return j < 0 ? null : { start: map[j], end: map[j + nq.length - 1] + 1 };
  }

  function wrapRange(root, start, end, make) {
    const marks = [];
    for (const { node, start: ns } of textIndex(root).nodes) {
      const len = node.nodeValue.length;
      if (ns + len <= start || ns >= end) continue;
      const a = Math.max(0, start - ns), b = Math.min(len, end - ns);
      if (!node.nodeValue.slice(a, b).trim()) continue;
      let target = node;
      if (b < len) target.splitText(b);
      if (a > 0) target = target.splitText(a);
      const m = make();
      target.parentNode.insertBefore(m, target);
      m.appendChild(target);
      marks.push(m);
    }
    return marks;
  }

  function unwrapMarks(selector) {
    $$(selector, article).forEach((m) => {
      const p = m.parentNode;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
      p.normalize();
    });
  }

  function paintHighlights() {
    unwrapMarks('mark.al-hl');
    ui.anchors = new Map();
    for (const { root } of AL.threads(comments.all())) {
      if (!root.quote) { ui.anchors.set(root.id, { module: root.module, marks: [] }); continue; }
      let target = moduleById(root.module), loc = null;
      if (target) loc = locate(textIndex(target).text, root.quote, root.prefix, root.suffix);
      if (!loc) {
        for (const sec of modules) {
          loc = locate(textIndex(sec).text, root.quote, root.prefix, root.suffix);
          if (loc) { target = sec; break; }
        }
      }
      if (!loc) { ui.anchors.set(root.id, { module: root.module, marks: [], orphan: true }); continue; }
      const cls = 'mark.al-hl' + (root.status === 'open' ? '' : '.is-resolved') + (ui.focus === root.id ? '.is-active' : '');
      const marks = wrapRange(target, loc.start, loc.end, () => h(cls, { dataset: { cid: root.id } }));
      ui.anchors.set(root.id, { module: target.dataset.module, marks });
    }
    if (ui.compose && ui.compose.quote) paintDraft();
  }

  function paintDraft() {
    unwrapMarks('mark.al-hl.is-draft');
    const c = ui.compose;
    const sec = c && moduleById(c.module);
    if (!sec || !c.quote) return;
    const loc = locate(textIndex(sec).text, c.quote, c.prefix, c.suffix);
    if (loc) wrapRange(sec, loc.start, loc.end, () => h('mark.al-hl.is-draft'));
  }

  function blockFor(anchor) {
    const sec = moduleById(anchor.module);
    let block = anchor.marks.length ? anchor.marks[0].closest('p,li,td,th,pre,blockquote,h2,h3,h4,h5,h6,figure,dd,dt,summary,div') : null;
    if (!block || !article.contains(block)) block = sec ? sec.querySelector(':scope > h2') || sec : null;
    if (block && block.closest('.al-module.is-collapsed') && block.tagName !== 'H2') {
      const s = block.closest('.al-module');
      block = s.querySelector(':scope > h2') || s;
    }
    return block;
  }

  function paintGutter() {
    $$('.al-gutter', article).forEach((n) => n.remove());
    const groups = new Map();
    for (const [id, a] of ui.anchors) {
      const row = comments.get(id);
      if (!row || row.status === 'deleted' || a.orphan) continue;
      const block = blockFor(a);
      if (!block) continue;
      if (!groups.has(block)) groups.set(block, []);
      groups.get(block).push(id);
    }
    const top0 = article.getBoundingClientRect().top;
    for (const [block, ids] of groups) {
      const open = ids.some((id) => comments.get(id).status === 'open');
      article.appendChild(h('button.al-gutter.al-noanchor' + (open ? '' : '.is-resolved'), {
        type: 'button',
        style: `top:${Math.round(block.getBoundingClientRect().top - top0)}px`,
        title: `${ids.length} review thread${ids.length > 1 ? 's' : ''}`,
        'aria-label': `${ids.length} review thread${ids.length > 1 ? 's' : ''}`,
        onClick: () => { openPanel(); ensureVisible(ids[0]); focusThread(ids[0], false); },
      }, AL.iconEl('comment'), ids.length > 1 ? h('span', null, String(ids.length)) : null));
    }
  }

  /* ======================================================= selection */
  let selBtn;
  function setupSelection() {
    const pick = (mode) => () => { if (ui.pendingSel) startCompose(ui.pendingSel, mode); hideSel(); };
    selBtn = h('div.al-selbar.al-noanchor', { hidden: true, onMousedown: (e) => e.preventDefault() },
      h('button.al-selbtn', { type: 'button', title: 'Comment on the selection (c)', onClick: pick('comment') }, AL.iconEl('comment'), 'Comment'),
      h('button.al-selbtn.is-ask', { type: 'button', title: 'Ask the agent about the selection (a)', onClick: pick('ask') }, AL.iconEl('spark'), 'Ask'));
    document.body.appendChild(selBtn);
    const later = (ms) => () => setTimeout(onSelection, ms);
    document.addEventListener('mouseup', later(0));
    document.addEventListener('touchend', later(300));
    document.addEventListener('keyup', (e) => { if (e.shiftKey || e.key === 'Shift') onSelection(); });
    document.addEventListener('mousedown', (e) => { if (!selBtn.contains(e.target)) hideSel(); });
    article.addEventListener('click', (e) => {
      const m = e.target.closest('mark.al-hl[data-cid]');
      if (m && window.getSelection().isCollapsed) { openPanel(); ensureVisible(m.dataset.cid); focusThread(m.dataset.cid, false); }
    });
  }
  function hideSel() { if (selBtn) selBtn.hidden = true; ui.pendingSel = null; }
  function currentSelection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    const startEl = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
    if (!startEl || !article.contains(startEl) || startEl.closest('.al-noanchor')) return null;
    const sec = startEl.closest('.al-module');
    if (!sec) return null;
    const off = rangeOffsets(range, sec);
    if (!off) return null;
    return {
      range,
      anchor: {
        module: sec.dataset.module,
        quote: off.text.slice(off.start, off.end).slice(0, 1500),
        prefix: off.text.slice(Math.max(0, off.start - 40), off.start),
        suffix: off.text.slice(off.end, off.end + 40),
      },
    };
  }
  function onSelection() {
    const cur = currentSelection();
    if (!cur) return hideSel();
    ui.pendingSel = cur.anchor;
    const rects = cur.range.getClientRects();
    const r = rects.length ? rects[rects.length - 1] : cur.range.getBoundingClientRect();
    selBtn.hidden = false;
    const w = selBtn.offsetWidth || 110;
    const left = Math.min(Math.max(8, r.left), document.documentElement.clientWidth - w - 8);
    selBtn.style.left = `${left + window.scrollX}px`;
    selBtn.style.top = `${r.bottom + window.scrollY + 10}px`;
  }

  /* ========================================================== panel */
  let panel, threadsBox, composerBox, syncBar, tabsBox, nameInput;

  function buildPanel() {
    nameInput = h('input.al-input', { placeholder: 'your name', 'aria-label': 'Your name', value: reviewer, autocomplete: 'name' });
    nameInput.addEventListener('input', () => {
      reviewer = nameInput.value.trim();
      AL.storage.set('agentledger:name', reviewer);
      nameInput.classList.remove('is-missing');
    });
    syncBar = h('div.al-syncbar');
    tabsBox = h('div.al-tabs', { role: 'tablist' });
    composerBox = h('div.al-composer', { hidden: true });
    threadsBox = h('div.al-threads', { 'aria-live': 'polite' });
    panel = h('aside.al-panel', { hidden: true, 'aria-label': 'Review comments' },
      h('div.al-panel-head', null,
        h('div.al-panel-title', null, h('h2', null, 'Review comments'), nameInput, iconBtn('x', 'Close (Esc)', closePanel)),
        h('div.al-panel-actions', null,
          btn('download', 'Export CSV', exportCsv, '.is-small'),
          btn('copy', 'Copy CSV', copyCsv, '.is-small'),
          btn('upload', 'Import CSV', importCsv, '.is-small'),
          btn('bot', 'Copy for agent', copyForAgent, '.is-small', 'Copy the listed threads as a Markdown brief for an agent'),
          btn('trash', 'Clear', clearLocal, '.is-small.is-ghost', 'Discard comments that only exist in this browser')),
        syncBar, tabsBox),
      composerBox, threadsBox);
    document.body.appendChild(panel);
  }

  function openPanel() {
    if (ui.panel) return;
    ui.panel = true;
    panel.hidden = false;
    if (pad.body && !pad.body.hidden && window.innerWidth < 1600) togglePad(false, true);
    document.body.classList.add('al-panel-open');
    renderPanel();
    setTimeout(relayout, 220);
  }
  function closePanel() {
    ui.panel = false;
    panel.hidden = true;
    document.body.classList.remove('al-panel-open');
    ui.compose = null;
    unwrapMarks('mark.al-hl.is-draft');
    setTimeout(relayout, 220);
  }
  function togglePanel() { ui.panel ? closePanel() : openPanel(); }

  function reportRows() { return comments.all().filter((r) => r.report === D.report.id); }

  function counts() {
    const ts = AL.threads(reportRows());
    return {
      open: ts.filter((t) => t.root.status === 'open').length,
      closed: ts.filter((t) => t.root.status === 'resolved' || t.root.status === 'wontfix').length,
      all: ts.length,
      here: ts.filter((t) => t.root.status === 'open' && t.root.version === String(V)).length,
    };
  }

  function listedThreads() {
    const modOrder = new Map(modules.map((s, i) => [s.dataset.module, i]));
    return AL.threads(reportRows())
      .filter((t) => ui.filter === 'all' || (ui.filter === 'open' ? t.root.status === 'open' : t.root.status !== 'open'))
      .filter((t) => ui.scope === 'all' || t.root.version === String(V))
      .map((t) => {
        const a = ui.anchors.get(t.root.id) || {};
        const sec = moduleById(a.module || t.root.module);
        let pos = modOrder.has(a.module) ? modOrder.get(a.module) * 1e6 : 9e9;
        if (a.marks && a.marks.length && sec) pos += textIndex(sec).text.indexOf(a.marks[0].textContent) / 1e3;
        return { t, pos };
      })
      .sort((x, y) => x.pos - y.pos || (x.t.root.created < y.t.root.created ? -1 : 1))
      .map((x) => x.t);
  }

  function renderPanel() {
    const c = counts();
    ui.countBadge.textContent = String(c.open);
    ui.countBadge.classList.toggle('is-zero', !c.open);
    if (!ui.panel) return;

    // sync status
    const dirty = comments.dirty().length;
    syncBar.replaceChildren();
    const where = sync.mode === 'server' ? h('span', null, 'Saving to the store via ', h('code', null, 'agentledger serve'))
      : sync.mode === 'github' ? h('span', null, 'Saving to ', h('code', null, `${D.store.github.repo}@${D.store.github.branch}`))
        : h('span', null, 'Offline copy: export the CSV, then ', h('code', null, 'agentledger comments import'));
    syncBar.append(dirty ? h('span.al-pill.is-local', null, `${dirty} unsaved`) : h('span.al-pill.is-resolved', null, 'all saved'), where);
    if (sync.mode !== 'none') {
      const b = btn('save', sync.label(), saveComments, '.is-small.is-primary');
      b.disabled = !dirty;
      syncBar.append(b);
    }

    tabsBox.replaceChildren(
      ...[['open', 'Open', c.open], ['closed', 'Resolved', c.closed], ['all', 'All', c.all]].map(([k, label, n]) =>
        h('button.al-tab' + (ui.filter === k ? '.is-on' : ''), { type: 'button', role: 'tab', 'aria-selected': String(ui.filter === k), onClick: () => { ui.filter = k; renderPanel(); } },
          label, h('small', null, String(n)))),
      (() => {
        const s = h('select', { 'aria-label': 'Version scope', onChange: (e) => { ui.scope = e.target.value; renderPanel(); } },
          h('option', { value: 'all' }, 'All versions'), h('option', { value: 'here' }, `Opened on v${V}`));
        s.value = ui.scope;
        return s;
      })());

    renderComposer();
    const list = listedThreads();
    threadsBox.replaceChildren(...(list.length ? list.map(threadEl) : [h('div.al-empty', null,
      ui.filter === 'open' ? 'No open threads. Select any text in the report and press ' : 'Nothing here yet. Select text and press ',
      h('strong', null, 'Comment'), ', or use the ', AL.iconEl('comment'), ' button on a module.')]));
    if (ui.focus) { const el = threadsBox.querySelector(`[data-cid="${CSS.escape(ui.focus)}"]`); if (el) el.classList.add('is-focus'); }

  }

  function msgEl(m, isReply) {
    const body = h('div.al-msg-body', { html: AL.mdLite(m.body) });
    return h('div.al-msg' + (isReply ? '.is-reply' : '') + (m.role === 'agent' ? '.is-agent' : ''), { dataset: { mid: m.id } },
      h('div.al-msg-head', null,
        h('b', null, m.author || 'anonymous'),
        m.role === 'agent' ? h('span.al-pill.is-agent', null, 'agent') : null,
        h('time', { datetime: m.created, title: m.created }, AL.fmtTime(m.created)),
        comments.isDirty(m.id) ? h('span.al-pill.is-local', { title: 'Only in this browser — save or export' }, 'unsaved') : null),
      body);
  }

  function threadEl(t) {
    const r = t.root;
    const a = ui.anchors.get(r.id) || {};
    const mod = modInfo.get(a.module || r.module);
    const replyBox = h('div.al-replybox', { hidden: true });
    const el = h('div.al-thread' + (r.status !== 'open' ? '.is-closed' : ''), { dataset: { cid: r.id } },
      h('div.al-thread-top', null,
        h('span.al-pill.is-' + r.status, null, r.status === 'wontfix' ? "won't fix" : r.status),
        r.kind ? h('span.al-pill', null, r.kind) : null,
        r.version !== String(V) ? h('span.al-pill.is-status', { title: `Opened on v${r.version}` }, `from v${r.version}`) : null,
        r.resolved_in ? h('span', null, `resolved in v${r.resolved_in}`) : null,
        a.orphan ? h('span.al-pill.is-wontfix', { title: 'The quoted text is not in this version' }, 'text changed') : null,
        h('span.al-thread-mod', { title: mod ? mod.title : r.module }, mod ? mod.title : r.module || '')),
      r.quote ? h('blockquote.al-quote', { title: 'Show in report', onClick: () => go(r.id) }, r.quote) : null,
      msgEl(r, false), ...t.replies.map((x) => msgEl(x, true)),
      askingEl(r.id),
      h('div.al-thread-actions', null,
        btn('go', 'go', () => go(r.id), '.is-small', 'Scroll to the commented text'),
        btn('reply', 'reply', () => openReply(replyBox, r), '.is-small'),
        ui.asking.has(r.id) ? null : btn('spark', 'ask', () => askAgent(r.id), '.is-small', `Have ${askName()} answer this thread`),
        r.status === 'open'
          ? btn('check', 'resolve', () => setStatus(r, 'resolved'), '.is-small')
          : btn('reopen', 'reopen', () => setStatus(r, 'open'), '.is-small'),
        (r.author && r.author === reviewer) || comments.isDirty(r.id) ? btn('edit', 'edit', () => editMsg(el, r), '.is-small.is-ghost') : null,
        btn('trash', 'delete', () => removeThread(r), '.is-small.is-ghost.is-danger')),
      replyBox);
    return el;
  }

  function needName() {
    if (reviewer) return false;
    openPanel();
    nameInput.classList.add('is-missing');
    nameInput.focus();
    AL.toast('Add your name first, so the agent knows who asked.', 'error');
    return true;
  }

  function openReply(box, root) {
    if (!box.hidden) { box.hidden = true; return; }
    const ta = h('textarea.al-textarea', { placeholder: 'Reply… (Ctrl/⌘+Enter to send)' });
    const send = () => {
      if (needName() || !ta.value.trim()) return false;
      const ts = AL.nowIso();
      comments.put({ id: AL.newId(), reply_to: root.id, report: root.report, version: root.version, module: root.module,
        body: ta.value.trim(), author: reviewer, role: 'human', status: '', created: ts, updated: ts });
      ui.focus = root.id;
      return true;
    };
    ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send(); if (e.key === 'Escape') box.hidden = true; });
    box.replaceChildren(ta, h('div.al-composer-row', null, btn(null, 'Cancel', () => { box.hidden = true; }, '.is-small'),
      btn('reply', 'Reply', send, '.is-small'),
      btn('spark', 'Reply & ask', () => { if (!sync.askBackend) return askUnavailable(); if (send()) askAgent(root.id); }, '.is-small.is-primary',
        `Send your reply and have ${askName()} answer it`)));
    box.hidden = false;
    ta.focus();
  }

  function editMsg(el, row) {
    const msg = el.querySelector(`[data-mid="${CSS.escape(row.id)}"] .al-msg-body`);
    if (!msg || msg.querySelector('textarea')) return;
    const ta = h('textarea.al-textarea');
    ta.value = row.body;
    const save = () => { if (ta.value.trim() && ta.value.trim() !== row.body) comments.put(Object.assign({}, comments.get(row.id), { body: ta.value.trim() })); else renderPanel(); };
    ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save(); if (e.key === 'Escape') renderPanel(); });
    msg.replaceChildren(ta, h('div.al-composer-row', null, btn(null, 'Cancel', renderPanel, '.is-small'), btn('check', 'Save', save, '.is-small.is-primary')));
    ta.focus();
  }

  function setStatus(root, status) {
    const cur = comments.get(root.id);
    comments.put(Object.assign({}, cur, { status, resolved_in: status === 'resolved' ? String(V) : '' }));
    AL.toast(status === 'resolved' ? 'Thread resolved' : 'Thread reopened');
  }

  function removeThread(root) {
    AL.modal('Delete this thread?', h('p', null, 'It is kept as a deleted tombstone so the deletion survives merges and imports.'), [{
      label: 'Delete', primary: true,
      run: () => {
        const rows = [root, ...reportRows().filter((r) => r.reply_to === root.id)];
        comments.addLocal(rows.map((r) => Object.assign({}, comments.get(r.id), { status: 'deleted', updated: AL.nowIso() })));
      },
    }]);
  }

  /* ============================================================= ask */
  const askName = () => sync.askBackend || 'the agent';
  function askUnavailable() {
    AL.modal('Ask the agent', h('div', null,
      h('p', null, 'Ask runs Claude Code or Codex on the machine that holds the store, read-only, with the report, its traces and the repository it describes as context. The answer is saved as a reply in the thread.'),
      h('p', null, 'Open this page through ', h('code', null, 'agentledger serve'), ' on that machine, with ', h('code', null, 'claude'), ' or ',
        h('code', null, 'codex'), ' installed. For a remote host: ', h('code', null, 'ssh -L 8765:localhost:8765 host'), ', then http://localhost:8765.')), []);
  }
  async function askAgent(threadId) {
    if (!sync.askBackend) return askUnavailable();
    const cur = ui.asking.get(threadId);
    if (cur && cur.status === 'running') return;
    const rows = comments.dirty();
    ui.asking.set(threadId, { status: 'running', progress: [`Sending to ${askName()}…`] });
    ui.focus = threadId;
    ui.reveal = threadId;
    renderPanel();
    try {
      const r = await sync.ask(rows, threadId);
      ui.asking.get(threadId).job = r.job;
      comments.markSynced(rows);
      comments.addRemote((r.rows || []).filter((x) => x.report === D.report.id));
      pollAsk(threadId);
    } catch (e) {
      ui.asking.set(threadId, { status: 'error', error: e.message, progress: [] });
      renderPanel();
    }
  }
  function pollAsk(threadId) {
    const entry = ui.asking.get(threadId);
    if (!entry || !entry.job) return;
    setTimeout(async () => {
      let st;
      try { st = await sync.askStatus(entry.job); } catch (e) {
        entry.status = 'error'; entry.error = e.message;
        return renderPanel();
      }
      entry.progress = st.progress || [];
      entry.elapsed = st.elapsed;
      if (st.status === 'done') {
        ui.asking.delete(threadId);
        ui.focus = threadId;
        ui.reveal = threadId;
        if (st.reply) comments.addRemote([st.reply]);
        AL.toast(`${askName()} answered`);
        return;
      }
      if (st.status === 'error') {
        entry.status = 'error'; entry.error = st.error;
        return renderPanel();
      }
      updateAsking(threadId);
      pollAsk(threadId);
    }, 1200);
  }
  function askingEl(threadId) {
    const e = ui.asking.get(threadId);
    if (!e) return null;
    if (e.status === 'error') {
      return h('div.al-asking.is-error', null, h('b', null, `${askName()} could not answer`), h('div.al-asking-step', null, e.error),
        btn('reopen', 'try again', () => { ui.asking.delete(threadId); askAgent(threadId); }, '.is-small'));
    }
    return h('div.al-asking', { 'aria-live': 'polite' },
      h('div.al-asking-h', null, h('span.al-spinner'), h('b', null, `${askName()} is working`), e.elapsed ? h('span.al-muted', null, `${e.elapsed}s`) : null),
      ...(e.progress || []).slice(-3).map((step) => h('div.al-asking-step', null, step)));
  }
  function updateAsking(threadId) {
    const card = threadsBox.querySelector(`.al-thread[data-cid="${CSS.escape(threadId)}"]`);
    const fresh = askingEl(threadId);
    if (!card || !fresh) return;
    const old = card.querySelector('.al-asking');
    if (old) old.replaceWith(fresh);
    else card.querySelector('.al-thread-actions').before(fresh);
  }

  /* ======================================================== composer */
  function startCompose(anchor, mode) {
    if (mode === 'ask' && !sync.askBackend) return askUnavailable();
    openPanel();
    ui.compose = Object.assign({}, anchor, { mode: mode || 'comment' });
    renderComposer();
    paintDraft();
    const ta = composerBox.querySelector('textarea');
    if (ta) ta.focus();
    window.getSelection().removeAllRanges();
  }

  function renderComposer() {
    const c = ui.compose;
    composerBox.hidden = !c;
    if (!c) { composerBox.replaceChildren(); return; }
    if (composerBox.dataset.for === JSON.stringify(c)) return;
    composerBox.dataset.for = JSON.stringify(c);
    const mod = modInfo.get(c.module);
    const asking = c.mode === 'ask';
    const ta = h('textarea.al-textarea', {
      placeholder: asking ? `What do you want to know? ${askName()} answers from the report, its traces and the repo.`
        : 'Ask the agent to verify, explain or change something…',
      'aria-label': asking ? 'Question' : 'Comment',
    });
    const kinds = h('div.al-kinds', { role: 'radiogroup', 'aria-label': 'Kind' }, ...AL.KINDS.map((k) => h('button.al-kind' + (ui.kind === k ? '.is-on' : ''), {
      type: 'button', role: 'radio', 'aria-checked': String(ui.kind === k),
      onClick: (e) => { ui.kind = k; $$('.al-kind', kinds).forEach((b) => { b.classList.toggle('is-on', b === e.currentTarget); b.setAttribute('aria-checked', String(b === e.currentTarget)); }); },
    }, k)));
    const save = () => {
      if (needName()) return;
      const body = ta.value.trim();
      if (!body) { ta.focus(); return; }
      const ts = AL.nowIso();
      const row = { id: AL.newId(), reply_to: '', report: D.report.id, version: String(V), module: c.module, kind: asking ? 'question' : ui.kind,
        quote: c.quote || '', prefix: c.prefix || '', suffix: c.suffix || '', body, author: reviewer, role: 'human',
        status: 'open', created: ts, updated: ts };
      ui.compose = null;
      ui.focus = row.id;
      if (ui.filter === 'closed') ui.filter = 'open';
      composerBox.dataset.for = '';
      comments.put(row);
      if (asking) return askAgent(row.id);
      AL.toast(sync.mode === 'none' ? 'Comment kept in this browser — export the CSV when done' : 'Comment added — save when ready');
    };
    const cancel = () => { ui.compose = null; composerBox.dataset.for = ''; unwrapMarks('mark.al-hl.is-draft'); renderComposer(); };
    ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save(); if (e.key === 'Escape') { e.stopPropagation(); cancel(); } });
    composerBox.replaceChildren(
      h('div.al-composer-h', null, asking ? `Ask ${askName()} about ${c.quote ? 'this selection' : `“${mod ? mod.title : c.module}”`}`
        : c.quote ? 'New comment on selection' : `New comment on “${mod ? mod.title : c.module}”`),
      c.quote ? h('blockquote.al-quote', null, c.quote) : null,
      asking ? null : kinds, ta,
      h('div.al-composer-row', null, h('span.al-muted', null, 'Ctrl/⌘ + Enter'), btn(null, 'Cancel', cancel, '.is-small'),
        asking ? btn('spark', 'Ask', save, '.is-small.is-primary') : btn('comment', 'Comment', save, '.is-small.is-primary')));
  }

  /* =================================================== import/export */
  const fileBase = () => `${D.report.id.replace(/\//g, '__')}-v${V}-comments`;
  function exportCsv() { AL.download(`${fileBase()}.csv`, AL.rowsToCsv(reportRows())); }
  function copyCsv() { AL.copy(AL.rowsToCsv(reportRows())).then((ok) => AL.toast(ok ? 'CSV copied' : 'Copy failed', ok ? '' : 'error')); }
  async function importCsv() {
    const text = await AL.pickFile();
    if (text == null) return;
    const rows = AL.rowsFromCsv(text);
    const mine = rows.filter((r) => r.report === D.report.id && versionOf(r.version));
    if (!mine.length) return AL.toast(`No rows for ${D.report.id} in that file`, 'error');
    const before = new Map(comments.all().map((r) => [r.id, r.updated]));
    const fresh = mine.filter((r) => !before.has(r.id) || r.updated > before.get(r.id));
    comments.addLocal(fresh);
    AL.toast(`Imported ${fresh.length} new or updated row${fresh.length === 1 ? '' : 's'}${rows.length > mine.length ? ` (${rows.length - mine.length} for other reports skipped)` : ''}`);
  }
  function copyForAgent() {
    const list = listedThreads();
    if (!list.length) return AL.toast('No threads in this view', 'error');
    const latest = versionOf(D.latest);
    const brief = AL.agentBrief(list, {
      title: `${D.report.title} (v${V})`,
      storeName: D.store.name,
      reportInfo: () => ({ title: D.report.title, source: latest && latest.source_path }),
      isDirty: (id) => comments.isDirty(id),
    });
    AL.copy(brief).then((ok) => AL.toast(ok ? `Copied ${list.length} thread${list.length === 1 ? '' : 's'} as an agent brief` : 'Copy failed', ok ? '' : 'error'));
  }
  function clearLocal() {
    const n = comments.dirty().length;
    if (!n) return AL.toast('Nothing unsaved in this browser');
    AL.modal(`Discard ${n} unsaved change${n === 1 ? '' : 's'}?`, h('p', null, 'This removes comments and edits that exist only in this browser. Saved threads are not affected.'),
      [{ label: 'Discard', primary: true, run: () => comments.clearLocal() }]);
  }
  async function saveComments() {
    const rows = comments.dirty();
    if (!rows.length) return;
    try {
      const saved = await sync.save(rows, (r) => { const v = versionOf(r.version); return v && v.comments_path; });
      comments.markSynced(rows);
      comments.addRemote(saved.filter((r) => r.report === D.report.id), { prune: sync.mode === 'server' });
      AL.toast(`Saved ${rows.length} change${rows.length === 1 ? '' : 's'}`);
    } catch (e) {
      if (e.message !== 'cancelled') AL.toast(`Save failed: ${e.message}`, 'error');
    }
  }

  /* ============================================================ go */
  function ensureVisible(id) {
    const row = comments.get(id);
    if (!row) return;
    const want = row.status === 'open' ? 'open' : 'closed';
    if (ui.filter !== 'all' && ui.filter !== want) ui.filter = want;
    if (ui.scope === 'here' && row.version !== String(V)) ui.scope = 'all';
  }
  function focusThread(id, scrollDoc) {
    ui.focus = id;
    $$('mark.al-hl.is-active', article).forEach((m) => m.classList.remove('is-active'));
    const a = ui.anchors.get(id);
    if (a) a.marks.forEach((m) => m.classList.add('is-active'));
    renderPanel();
    const el = threadsBox.querySelector(`[data-cid="${CSS.escape(id)}"]`);
    if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    if (scrollDoc) go(id);
  }
  function go(id) {
    const a = ui.anchors.get(id);
    if (!a) return;
    const sec = moduleById(a.module);
    if (sec && sec.classList.contains('is-collapsed')) toggleCollapse(sec);
    const target = a.marks[0] || (sec && (sec.querySelector(':scope > h2') || sec));
    if (!target) return;
    if (window.innerWidth < 900 && ui.panel) closePanel();
    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    ui.focus = id;
    $$('mark.al-hl.is-active', article).forEach((m) => m.classList.remove('is-active'));
    a.marks.forEach((m) => m.classList.add('is-active'));
    if (a.orphan) AL.toast('The quoted text is not in this version; showing its module');
  }

  /* ======================================================= geometry */
  const geo = { tops: [], heights: [], articleTop: 0, articleH: 1 };
  const pageY = (el) => el.getBoundingClientRect().top + window.scrollY;
  function measure() {
    geo.articleTop = pageY(article);
    geo.articleH = article.offsetHeight || 1;
    geo.tops = modules.map(pageY);
    geo.heights = modules.map((m, i) => Math.max(1, (i + 1 < modules.length ? geo.tops[i + 1] : geo.tops[i] + m.offsetHeight) - geo.tops[i]));
  }
  const readLine = () => window.scrollY + window.innerHeight * 0.28;
  function progress() {
    const n = modules.length;
    if (!n) return { i: 0, f: 0, p: 0 };
    const y = readLine();
    let i = 0;
    for (let k = 0; k < n; k++) if (geo.tops[k] <= y) i = k;
    const f = Math.min(1, Math.max(0, (y - geo.tops[i]) / geo.heights[i]));
    const atEnd = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2;
    return { i, f, p: atEnd ? 1 : Math.min(1, (i + f) / n) };
  }
  function scrollToProgress(p) {
    const n = modules.length;
    p = Math.min(1, Math.max(0, p));
    const i = Math.min(n - 1, Math.floor(p * n));
    const f = p * n - i;
    window.scrollTo(0, geo.tops[i] + f * geo.heights[i] - window.innerHeight * 0.28 + 1);
  }
  function scrollToModule(i) {
    i = Math.max(0, Math.min(modules.length - 1, i));
    window.scrollTo({ top: geo.tops[i] - 72, behavior: 'smooth' });
  }

  /* ============================================================ rail */
  const rail = {};
  function buildRail() {
    const n = modules.length;
    if (n < 2) return;
    rail.el = h('nav.al-rail.al-noanchor', { 'aria-label': 'Report modules' });
    rail.el.style.height = `min(64vh, ${Math.max(140, n * 34)}px)`;
    rail.track = h('div.al-rail-track');
    rail.fill = h('div.al-rail-fill');
    rail.nodes = [];
    rail.labels = [];
    rail.ticks = [];
    rail.el.append(rail.track, rail.fill, h('div.al-rail-end'));
    modules.forEach((sec, i) => {
      const info = modInfo.get(sec.dataset.module) || {};
      const top = `${(i / n) * 100}%`;
      const node = h('button.al-rail-node', { type: 'button', style: `top:${top}`, title: info.title, 'aria-label': info.title, dataset: { state: info.state || '' }, onClick: () => scrollToModule(i) });
      const badge = h('span.al-badge', { hidden: true });
      const label = h('button.al-rail-label', { type: 'button', style: `top:${top}`, tabindex: -1, onClick: () => scrollToModule(i) }, h('span.al-rl-t', null, info.title), badge);
      label.badge = badge;
      rail.nodes.push(node);
      rail.labels.push(label);
      rail.el.append(node, label);
      (info.subs || []).forEach((s) => {
        const tick = h('div.al-rail-tick');
        tick.target = document.getElementById(s.id);
        tick.index = i;
        rail.ticks.push(tick);
        rail.el.append(tick);
      });
    });
    rail.knob = h('button.al-rail-knob', { type: 'button', 'aria-label': 'Drag to scrub through the report' });
    rail.bubble = h('div.al-rail-bubble');
    rail.el.append(rail.knob, rail.bubble);
    document.body.appendChild(rail.el);

    const toP = (e) => { const r = rail.track.getBoundingClientRect(); return (e.clientY - r.top) / r.height; };
    let dragging = false;
    const move = (e) => {
      if (!dragging) return;
      const p = Math.min(1, Math.max(0, toP(e)));
      scrollToProgress(p);
      const i = Math.min(n - 1, Math.floor(p * n));
      rail.bubble.textContent = `${i + 1}/${n} · ${(modInfo.get(modules[i].dataset.module) || {}).title || ''}`;
      rail.bubble.style.top = `${p * 100}%`;
    };
    const start = (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      dragging = true;
      rail.el.classList.add('is-dragging');
      rail.knob.setPointerCapture(e.pointerId);
      e.preventDefault();
      move(e);
    };
    rail.knob.addEventListener('pointerdown', start);
    rail.track.addEventListener('pointerdown', (e) => { start(e); });
    rail.knob.addEventListener('pointermove', move);
    const end = () => { dragging = false; rail.el.classList.remove('is-dragging'); };
    rail.knob.addEventListener('pointerup', end);
    rail.knob.addEventListener('pointercancel', end);
    rail.knob.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); scrollToModule(ui.active + 1); }
      if (e.key === 'ArrowUp') { e.preventDefault(); scrollToModule(ui.active - 1); }
    });
  }
  function layoutRail() {
    if (!rail.el) return;
    const n = modules.length;
    rail.ticks.forEach((t) => {
      if (!t.target) return;
      const frac = Math.min(0.97, Math.max(0.03, (pageY(t.target) - geo.tops[t.index]) / geo.heights[t.index]));
      t.style.top = `${((t.index + frac) / n) * 100}%`;
    });
    rail.el.classList.toggle('is-wide', article.getBoundingClientRect().left > 300);
  }
  function updateRail(pos) {
    if (!rail.el) return;
    rail.knob.style.top = `${pos.p * 100}%`;
    rail.fill.style.height = `${pos.p * 100}%`;
    rail.nodes.forEach((nd, k) => { nd.classList.toggle('is-active', k === pos.i); nd.classList.toggle('is-passed', k < pos.i); });
    rail.labels.forEach((l, k) => l.classList.toggle('is-active', k === pos.i));
  }

  /* ============================================================= pad */
  const pad = {};
  function buildPad() {
    pad.el = h('div.al-pad.al-noanchor');
    pad.nub = h('span.al-pad-nub', null, `v${V}`);
    pad.stick = h('button.al-pad-stick', { type: 'button', title: 'Navigator — click to open, drag up/down to scroll', 'aria-label': 'Navigator', 'aria-expanded': 'false' }, pad.nub);
    pad.body = h('div.al-pad-body', { hidden: true });
    pad.el.append(pad.body, pad.stick);
    document.body.appendChild(pad.el);

    // Versions
    const prev = versionOf(V - 1), next = versionOf(V + 1);
    const chainBox = h('div.al-pad-chain');
    chainBox.appendChild(AL.chain(D.versions, { current: V, gap: 24, onPick: (v) => { if (v.n !== V) location.href = versionHref(v); }, open: openByVersion() }));
    pad.chainBox = chainBox;
    const secVersions = h('div.al-pad-sec', null,
      h('div.al-pad-h', null, h('span', null, `Version ${V} of ${D.versions.length}`), h('span', null,
        iconBtn('left', prev ? `Previous: v${prev.n}` : 'No earlier version', () => prev && (location.href = versionHref(prev))),
        iconBtn('right', next ? `Next: v${next.n}` : 'No later version', () => next && (location.href = versionHref(next))))),
      chainBox);

    // Related reports
    const rel = h('div.al-pad-rel');
    if (D.report.parent && D.report.parent.href) rel.append(h('a.al-rel', { href: D.report.parent.href, title: 'Parent report' }, AL.iconEl('up'), h('span', null, D.report.parent.title)));
    D.report.children.forEach((c) => rel.append(h('a.al-rel', { href: c.href || '#', title: c.about || 'Sub-report' }, AL.iconEl('layers'), h('span', null, c.title))));
    const secRel = rel.childNodes.length ? h('div.al-pad-sec', null, h('div.al-pad-h', null, 'Related'), rel) : null;

    // Minimap
    pad.mini = h('div.al-mini', { role: 'slider', tabindex: 0, 'aria-label': 'Minimap: drag to move through the report', 'aria-valuemin': 0, 'aria-valuemax': 100 });
    pad.blocks = modules.map((sec) => {
      const info = modInfo.get(sec.dataset.module) || {};
      const b = h('div.al-mini-block', { dataset: { state: info.state || '' }, title: info.title }, h('b', null, info.title));
      pad.mini.append(b);
      return b;
    });
    pad.dots = h('div');
    pad.view = h('div.al-mini-view');
    pad.mini.append(pad.dots, pad.view);
    const secMini = h('div.al-pad-sec', null, h('div.al-pad-h', null, 'Map of this report'), pad.mini);

    pad.body.append(secVersions, secRel || '', secMini, h('div.al-pad-foot', null,
      btn('map', 'Project map', openMap, '.is-small'),
      h('a.al-btn.is-small', { href: D.store.dashboard, html: AL.icon('grid') + '<span class="al-lbl">Dashboard</span>' })));

    // Minimap scrubbing
    let scrubbing = false;
    const scrub = (e) => {
      const r = pad.mini.getBoundingClientRect();
      const y = (e.clientY - r.top) / r.height * geo.articleH + geo.articleTop - window.innerHeight / 2;
      window.scrollTo(0, y);
    };
    pad.mini.addEventListener('pointerdown', (e) => { scrubbing = true; pad.mini.setPointerCapture(e.pointerId); scrub(e); });
    pad.mini.addEventListener('pointermove', (e) => { if (scrubbing) scrub(e); });
    pad.mini.addEventListener('pointerup', () => { scrubbing = false; });
    pad.mini.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'PageDown') { e.preventDefault(); window.scrollBy(0, window.innerHeight * 0.8); }
      if (e.key === 'ArrowUp' || e.key === 'PageUp') { e.preventDefault(); window.scrollBy(0, -window.innerHeight * 0.8); }
    });

    // Joystick: tap toggles the pad; drag up/down scrolls with speed proportional to the pull.
    let origin = null, steering = false, vy = 0, raf = 0, last = 0;
    const loop = (ts) => {
      if (!steering) return;
      const dt = last ? Math.min(50, ts - last) : 16;
      last = ts;
      window.scrollBy(0, vy * dt / 1000);
      raf = requestAnimationFrame(loop);
    };
    pad.stick.addEventListener('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      origin = { x: e.clientX, y: e.clientY };
      pad.stick.setPointerCapture(e.pointerId);
    });
    pad.stick.addEventListener('pointermove', (e) => {
      if (!origin) return;
      const dx = e.clientX - origin.x, dy = e.clientY - origin.y;
      if (!steering && Math.hypot(dx, dy) > 6) { steering = true; last = 0; pad.el.classList.add('is-steering'); raf = requestAnimationFrame(loop); }
      if (!steering) return;
      const lim = 22, cy = Math.max(-lim, Math.min(lim, dy)), cx = Math.max(-lim / 3, Math.min(lim / 3, dx / 3));
      pad.nub.style.transform = `translate(${cx}px, ${cy}px)`;
      const k = Math.sign(dy) * Math.pow(Math.min(1, Math.abs(dy) / 90), 1.6);
      vy = k * 3200;
    });
    const release = () => {
      if (!origin) return;
      const wasSteering = steering;
      origin = null; steering = false; vy = 0;
      cancelAnimationFrame(raf);
      pad.el.classList.remove('is-steering');
      pad.nub.style.transform = '';
      if (!wasSteering) togglePad();
    };
    pad.stick.addEventListener('pointerup', release);
    pad.stick.addEventListener('pointercancel', release);
    pad.stick.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); togglePad(); }
      if (e.key === 'ArrowDown') { e.preventDefault(); scrollToModule(ui.active + 1); }
      if (e.key === 'ArrowUp') { e.preventDefault(); scrollToModule(ui.active - 1); }
    });
    if (AL.storage.get('agentledger:pad-open', window.innerWidth >= 1100)) togglePad(true);
  }
  function togglePad(force, transient) {
    const open = typeof force === 'boolean' ? force : pad.body.hidden;
    pad.body.hidden = !open;
    pad.stick.setAttribute('aria-expanded', String(open));
    if (!transient) AL.storage.set('agentledger:pad-open', open);
    if (open) { layoutPad(); updatePad(progress()); const cur = pad.chainBox.querySelector('.is-current'); if (cur && cur.scrollIntoView) pad.chainBox.scrollLeft = Math.max(0, cur.getBBox().x - 60); }
  }
  function openByVersion() {
    const out = {};
    for (const { root } of AL.threads(reportRows())) if (root.status === 'open') out[root.version] = (out[root.version] || 0) + 1;
    return out;
  }
  function layoutPad() {
    if (!pad.body || pad.body.hidden) return;
    const H = pad.mini.clientHeight || 176, scale = H / geo.articleH;
    pad.scale = scale;
    modules.forEach((sec, i) => {
      const b = pad.blocks[i];
      b.style.top = `${(geo.tops[i] - geo.articleTop) * scale}px`;
      b.style.height = `${Math.max(3, geo.heights[i] * scale - 2)}px`;
      b.firstChild.style.display = geo.heights[i] * scale > 15 ? '' : 'none';
    });
    pad.dots.replaceChildren();
    for (const [id, a] of ui.anchors) {
      const row = comments.get(id);
      if (!row || row.status === 'deleted' || a.orphan) continue;
      const block = blockFor(a);
      if (!block) continue;
      pad.dots.append(h('div.al-mini-dot' + (row.status === 'open' ? '' : '.is-resolved'), { style: `top:${(pageY(block) - geo.articleTop) * scale + 4}px` }));
    }
  }
  function updatePad(pos) {
    if (!pad.body || pad.body.hidden) return;
    const s = pad.scale || 0;
    const topbar = 52;
    pad.view.style.top = `${Math.max(0, (window.scrollY + topbar - geo.articleTop) * s)}px`;
    pad.view.style.height = `${Math.max(8, (window.innerHeight - topbar) * s)}px`;
    pad.blocks.forEach((b, k) => b.classList.toggle('is-active', k === pos.i));
    pad.mini.setAttribute('aria-valuenow', String(Math.round(pos.p * 100)));
  }

  /* ============================================================= map */
  function currentModuleId() { const m = modules[ui.active]; return m ? m.dataset.module : ''; }
  function versionHref(v) {
    const id = currentModuleId();
    return v.href + (id && v.modules && v.modules.includes(id) ? '#' + id : '');
  }
  function openMap() {
    const list = h('div', { role: 'list' });
    for (const e of D.map) {
      const isCur = !!e.current;
      list.append(h('div.al-map-row' + (isCur ? '.is-current' : ''), { role: 'listitem', dataset: { depth: String(e.depth) }, style: `--depth:${e.depth}` },
        h('div.al-map-title', null,
          e.href ? h('a', { href: e.href }, e.title) : h('span', null, e.title),
          e.about ? h('small', null, e.about) : null),
        h('div.al-map-chain', null, e.versions.length ? AL.chain(e.versions, {
          current: isCur ? V : null, r: 10, gap: 22,
          open: isCur ? openByVersion() : null,
          onPick: (v) => { location.href = isCur ? versionHref(v) : v.href; },
        }) : h('span.al-muted', null, 'no versions')),
        h('div.al-map-meta', null,
          e.open ? h('span.al-pill.is-open', null, `${e.open} open`) : null,
          e.updated ? h('span', { title: AL.fmtTime(e.updated) }, AL.relTime(e.updated)) : null)));
    }
    const box = h('div', null,
      h('p.al-map-sub', null, `Every report in ${D.project.title}. Each circle is a version — click to open it; the ringed one is what you are reading.`),
      list);
    const close = AL.modal(`${D.project.title} — map`, box, []);
    const modal = close.el;
    if (modal) {
      modal.classList.add('al-map');
      modal.querySelector('h3').after(h('div', { style: 'margin:-2px 0 6px' }, h('a', { href: D.store.dashboard }, 'Open dashboard →')));
    }
    return close;
  }

  /* ============================================================ help */
  function showHelp() {
    const keys = [['j / k', 'Next / previous module'], ['[ / ]', 'Previous / next version'], ['c', 'Comment on the selection (or the current module)'], ['a', 'Ask the agent about the selection'],
      ['p', 'Show / hide review comments'], ['m', 'Project map'], ['n', 'Show / hide the navigator pad'], ['d', 'Dashboard'], ['Esc', 'Close panels'], ['?', 'This help']];
    AL.modal('Keyboard shortcuts', h('div', null,
      h('div.al-help-grid', null, ...keys.flatMap(([k, d]) => [h('span', null, ...k.split(' / ').flatMap((x, i) => [i ? ' / ' : '', h('kbd.al-kbd', null, x)])), h('span', null, d)])),
      h('p', { style: 'margin-top:16px' }, 'Select any text to comment on it. Comments stay in this browser until you save them to the store or GitHub, or export the CSV for ',
        h('code', null, 'agentledger comments import'), '.')), []);
  }

  function setupKeys() {
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        if ($('.al-modal-backdrop')) return;
        if (!selBtn.hidden) return hideSel();
        if (ui.compose) { ui.compose = null; composerBox.dataset.for = ''; unwrapMarks('mark.al-hl.is-draft'); return renderComposer(); }
        if (ui.panel) return closePanel();
        if (!pad.body.hidden) return togglePad(false);
        return;
      }
      if (typing(e) || e.metaKey || e.ctrlKey || e.altKey || $('.al-modal-backdrop')) return;
      const k = e.key;
      if (k === 'j') scrollToModule(ui.active + 1);
      else if (k === 'k') scrollToModule(ui.active - 1);
      else if (k === '[') { const v = versionOf(V - 1); if (v) location.href = versionHref(v); }
      else if (k === ']') { const v = versionOf(V + 1); if (v) location.href = versionHref(v); }
      else if (k === 'c') { const cur = currentSelection(); startCompose(cur ? cur.anchor : { module: currentModuleId() }); hideSel(); }
      else if (k === 'a') { const cur = currentSelection(); startCompose(cur ? cur.anchor : { module: currentModuleId() }, 'ask'); hideSel(); }
      else if (k === 'p') togglePanel();
      else if (k === 'm') openMap();
      else if (k === 'n') togglePad();
      else if (k === 'd') location.href = D.store.dashboard;
      else if (k === '?') showHelp();
      else return;
      e.preventDefault();
    });
  }

  /* ========================================================== render */
  let renderQueued = false;
  function render() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      paintHighlights();
      renderPanel();
      // Scroll the panel to a thread only once highlights are painted: list order depends on them.
      if (ui.reveal && ui.panel) {
        const el = threadsBox.querySelector(`.al-thread[data-cid="${CSS.escape(ui.reveal)}"]`);
        if (el) el.scrollIntoView({ block: 'nearest' });
        ui.reveal = null;
      }
      const perModule = new Map();
      for (const { root } of AL.threads(reportRows())) {
        if (root.status !== 'open') continue;
        const a = ui.anchors.get(root.id);
        const m = a ? a.module : root.module;
        perModule.set(m, (perModule.get(m) || 0) + 1);
      }
      ui.modCount.forEach((el, id) => { const n = perModule.get(id) || 0; el.hidden = !n; el.textContent = `${n} open`; });
      if (rail.labels) rail.labels.forEach((l, i) => { const n = perModule.get(modules[i].dataset.module) || 0; l.badge.hidden = !n; l.badge.textContent = String(n); });
      if (pad.chainBox) pad.chainBox.replaceChildren(AL.chain(D.versions, { current: V, gap: 24, onPick: (v) => { if (v.n !== V) location.href = versionHref(v); }, open: openByVersion() }));
      relayout();
    });
  }

  let layoutQueued = false;
  function relayout() {
    if (layoutQueued) return;
    layoutQueued = true;
    requestAnimationFrame(() => {
      layoutQueued = false;
      measure();
      paintGutter();
      layoutRail();
      layoutPad();
      onScroll();
    });
  }

  let scrollQueued = false;
  function onScroll() {
    if (scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      const pos = progress();
      ui.active = pos.i;
      updateRail(pos);
      updatePad(pos);
    });
  }

  async function refreshFromFiles() {
    const results = await Promise.all(D.versions.map((v) => sync.fetchCsv(v.comments_href).then((rows) => (rows || []).map((r) => Object.assign(r, { report: r.report || D.report.id, version: r.version || String(v.n) })))));
    const rows = results.flat();
    if (rows.length) comments.addRemote(rows);
  }

  function handleHash() {
    const m = /^#c=(.+)$/.exec(location.hash);
    if (!m) return;
    const id = decodeURIComponent(m[1]);
    if (!comments.get(id)) return;
    openPanel();
    ensureVisible(id);
    requestAnimationFrame(() => focusThread(id, true));
  }

  /* ============================================================ boot */
  buildTopbar();
  decorateContent();
  buildPanel();
  buildRail();
  buildPad();
  setupSelection();
  setupKeys();
  comments.addRemote(D.comments, { silent: true });
  comments.onChange(render);
  measure();
  render();
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', relayout);
  window.addEventListener('hashchange', handleHash);
  if ('ResizeObserver' in window) new ResizeObserver(relayout).observe(article);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(relayout);
  window.addEventListener('load', relayout);
  setTimeout(handleHash, 60);
  sync.detect().then(() => renderPanel());
  refreshFromFiles();
  window.AgentLedger = { data: D, comments, sync, openPanel, openMap };
})();
