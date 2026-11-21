/* EDLIZ Assistant — no framework, no build step.
 *
 * Two ideas shape this file:
 *   1. There is one place to type. Typing "ceftri" and typing "ceftriaxone for
 *      gonorrhoea" hit the same input; the first answers with medicines, the
 *      second with medicines *and* passages from the book.
 *   2. Everything is reachable from the keyboard, because this is a reference
 *      tool used while someone is standing up.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  view: 'ask',
  query: '',
  letter: '',
  medicines: null,      // cached browse list
  page: null,           // current drawer page
  lastFocus: null,
  busy: false,
};

/** fetch + JSON, with a readable failure instead of a parse error. */
async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText || 'error'}`.trim());
  return r.json();
}

/* ------------------------------- helpers ------------------------------- */

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const STOP = new Set(("a an the is are was were be been of for to in on at by with "
  + "and or not no what which who how when why does do can should could would may "
  + "tell about give me my patient child children adult dose doses").split(" "));

/** Significant words only: highlighting "the" tells the reader nothing. */
const terms = (q) => (q.match(/[A-Za-z][A-Za-z-]{2,}/g) || [])
  .filter((t) => t.length >= 4 && !STOP.has(t.toLowerCase()))
  .filter((t, i, a) => a.findIndex((x) => x.toLowerCase() === t.toLowerCase()) === i)
  .slice(0, 8);

/** Escape, then wrap matches — matches must be marked after escaping. */
function hl(text, q) {
  let out = esc(text);
  for (const t of terms(q)) {
    const safe = esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    out = out.replace(new RegExp(`\\b(${safe})`, 'gi'), '<mark>$1</mark>');
  }
  return out;
}

/** Small Markdown subset: headings, lists, bold, code, links. */
function md(src) {
  let text = esc(src);
  text = text.replace(/^### (.*)$/gm, '<h3>$1</h3>');
  text = text.replace(/^## (.*)$/gm, '<h2>$1</h2>');
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');

  const out = [];
  let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const line of text.split('\n')) {
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      const want = ul ? 'ul' : 'ol';
      if (list !== want) { close(); out.push(`<${want}>`); list = want; }
      out.push(`<li>${(ul || ol)[1]}</li>`);
      continue;
    }
    close();
    if (!line.trim()) continue;
    if (/^<h[23]>/.test(line)) { out.push(line); continue; }
    out.push(`<p>${line}</p>`);
  }
  close();
  return out.join('');
}

/** [p.42] and [W1] become the primary affordances of an answer. */
function citations(html) {
  return html
    .replace(/\[p\.(\d+)\]/g, '<button class="cite" data-page="$1">p.$1</button>')
    .replace(/\[W(\d+)\]/g, '<button class="cite web" data-web="$1">W$1</button>');
}

const messagesEl = $('#messages');

function atBottom(force = false) {
  const near = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 180;
  if (force || near) messagesEl.scrollTop = messagesEl.scrollHeight;
}

/* -------------------------------- views -------------------------------- */

function setView(view, { focus = false } = {}) {
  state.view = view;
  $$('.vtab').forEach((t) => {
    const on = t.dataset.view === view;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  });
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
  document.body.dataset.view = view;
  history.replaceState(null, '', `#${view}`);

  if (view === 'list') {
    if (!state.medicines) loadBrowse();
    if (focus) $('#q').focus();
  } else if (focus) {
    $('#question').focus();
  }
}

$$('.vtab').forEach((tab) => {
  tab.addEventListener('click', () => setView(tab.dataset.view, { focus: false }));
});

/* --------------------------------- ask --------------------------------- */

let webCache = [];

function addUser(text) {
  const el = document.createElement('div');
  el.className = 'msg user';
  el.innerHTML = `<div class="who">You</div><div class="bubble">${esc(text)}</div>`;
  messagesEl.appendChild(el);
  atBottom(true);
}

function addAssistant() {
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML = `<div class="who">EDLIZ Assistant</div>
                  <div class="steps" role="status" aria-live="polite"></div>
                  <div class="bubble hidden"></div>`;
  messagesEl.appendChild(el);
  return { steps: el.querySelector('.steps'), bubble: el.querySelector('.bubble') };
}

function stepNode(label) {
  const el = document.createElement('div');
  el.className = 'step';
  el.innerHTML = `<span class="spin" aria-hidden="true"></span><span>${esc(label)}</span>`;
  return el;
}

function stepLabel(name, args) {
  if (name === 'search_edliz') return `Searching EDLIZ — “${args.query || ''}”`;
  if (name === 'lookup_medicine') return `Looking up ${args.name || 'medicine'}`;
  if (name === 'web_search') return `Searching the web — “${args.query || ''}”`;
  if (name === 'pubmed') return `Searching PubMed — “${args.query || ''}”`;
  if (name === 'us_drug_label') return `Checking the US label for ${args.name || ''}`;
  if (name === 'fetch_page') return 'Reading a web page';
  return name;
}

async function ask(question) {
  const q = String(question || '').trim();
  if (state.busy || !q) return;
  state.busy = true;

  const send = $('#send');
  send.disabled = true;
  $('#recent')?.classList.add('hidden');
  $('#suggestions')?.classList.add('hidden');
  $('.welcome h2')?.remove();
  $('.welcome p')?.remove();

  if (state.view !== 'ask') setView('ask');
  addUser(q);
  const node = addAssistant();
  let answer = '';
  let pending = null;

  try {
    const res = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q }),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    const handle = (raw) => {
      const ev = raw.match(/^event:\s*(.*)$/m);
      const data = raw.match(/^data:\s*(.*)$/m);
      if (!data) return;
      const kind = ev ? ev[1].trim() : 'message';
      let d;
      try { d = JSON.parse(data[1]); } catch { return; }

      if (kind === 'step') {
        if (pending) { pending.classList.add('done'); pending = null; }
        if (d.kind === 'retrieve') {
          const el = stepNode(d.count
            ? `Read ${d.count} passages from ${d.pages.length} page${d.pages.length === 1 ? '' : 's'}`
            : 'No matching passages');
          el.classList.add('done');
          node.steps.appendChild(el);
        } else if (d.kind === 'tool') {
          pending = stepNode(stepLabel(d.name, d.args || {}));
          node.steps.appendChild(pending);
        } else if (d.kind === 'tool_error') {
          const el = stepNode('A tool call failed');
          el.classList.add('done');
          node.steps.appendChild(el);
        }
        atBottom();
      } else if (kind === 'token') {
        if (pending) { pending.classList.add('done'); pending = null; }
        answer += d.text || '';
        node.bubble.classList.remove('hidden');
        node.bubble.innerHTML = citations(md(answer));
        atBottom();
      } else if (kind === 'done') {
        webCache = d.web || [];
        renderSources(d.hits || [], webCache, q);
        const shown = d.pages.slice(0, 6).join(', ') + (d.pages.length > 6 ? '…' : '');
        const meta = stepNode(
          `${d.pages.length ? `Pages ${shown} · ` : ''}${(d.ms / 1000).toFixed(1)}s`);
        meta.classList.add('done');
        node.steps.appendChild(meta);
      } else if (kind === 'error') {
        node.bubble.classList.remove('hidden');
        node.bubble.innerHTML =
          `<p><strong>Something went wrong.</strong> ${esc(d.message || '')}</p>`;
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let i;
      while ((i = buffer.indexOf('\n\n')) >= 0) {
        handle(buffer.slice(0, i));
        buffer = buffer.slice(i + 2);
      }
    }
    if (pending) pending.classList.add('done');
    if (!answer) {
      node.bubble.classList.remove('hidden');
      node.bubble.innerHTML = '<p>No answer came back. Check the status indicator and try again.</p>';
    }
  } catch (err) {
    node.bubble.classList.remove('hidden');
    node.bubble.innerHTML =
      `<p><strong>Request failed.</strong> ${esc(err.message || err)}</p>`;
  } finally {
    state.busy = false;
    send.disabled = false;
    atBottom(true);
  }
}

const form = $('#ask-form');
const input = $('#question');

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const q = input.value.trim();
  input.value = '';
  input.style.height = 'auto';
  ask(q);
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 176)}px`;
});

/* ------------------------------ sources ------------------------------ */

function renderSources(hits, web, query) {
  const list = $('#sources-list');
  const hint = $('#sources-hint');
  const count = $('#sources-count');
  list.innerHTML = '';
  count.textContent = hits.length ? String(hits.length) : '';
  hint.classList.toggle('hidden', hits.length > 0);

  hits.forEach((h) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'source';
    b.innerHTML = `<div class="s-page">Page ${h.page}</div>
                   ${h.heading ? `<div class="s-head">${esc(h.heading)}</div>` : ''}
                   <div class="s-text">${hl(h.text || '', query)}</div>`;
    b.addEventListener('click', () => openPage(h.page, query));
    list.appendChild(b);
  });

  const block = $('#web-block');
  const webList = $('#web-list');
  webList.innerHTML = '';
  if (!web || !web.length) { block.classList.add('hidden'); return; }
  block.classList.remove('hidden');
  web.forEach((w, i) => {
    const a = document.createElement('a');
    a.href = w.url;
    a.target = '_blank';
    a.rel = 'noopener';
    const tag = ({ pubmed: 'PubMed', openfda: 'US FDA', wikipedia: 'Wikipedia' })[w.source] || 'Web';
    a.innerHTML = `<span class="tag">W${i + 1} · ${tag}</span> ${esc(w.title)}
                   <span class="snippet">${esc(w.snippet || '')}</span>`;
    webList.appendChild(a);
  });
}

/* -------------------------------- drawer -------------------------------- */

const drawer = $('#drawer');
const scrim = $('#scrim');

function showDrawer() {
  drawer.hidden = false;
  scrim.hidden = false;
  // next frame so the transition has a start state
  requestAnimationFrame(() => {
    drawer.dataset.open = 'true';
    scrim.dataset.open = 'true';
  });
}

function hideDrawer() {
  drawer.dataset.open = 'false';
  scrim.dataset.open = 'false';
  const done = () => {
    drawer.hidden = true;
    scrim.hidden = true;
    drawer.removeEventListener('transitionend', done);
  };
  drawer.addEventListener('transitionend', done);
  setTimeout(done, 320); // fallback if transitionend never fires
  state.page = null;
  if (state.lastFocus) { state.lastFocus.focus(); state.lastFocus = null; }
}

async function openPage(number, query = '') {
  const n = Number(number);
  if (!Number.isFinite(n) || n < 1) return;
  state.lastFocus = document.activeElement;
  state.page = n;

  $('#drawer-kicker').textContent = 'Page';
  $('#drawer-title').textContent = `Page ${n}`;
  $('#drawer-body').innerHTML = '<p class="empty">Loading…</p>';
  $('#prev-page').disabled = n <= 1;
  $('#next-page').disabled = false;

  showDrawer();
  drawer.focus();

  try {
    const r = await fetch(`/api/page/${n}`);
    if (!r.ok) throw new Error(`Page ${n} is not in this corpus.`);
    const d = await r.json();
    $('#drawer-title').textContent = `Page ${d.page}`;
    const kicker = $('#drawer-kicker');
    kicker.textContent = d.heading || '';
    kicker.classList.toggle('hidden', !d.heading);
    $('#drawer-body').innerHTML = `<div class="pagetext">${hl(d.text, query)}</div>`;
    const marked = $('#drawer-body mark');
    if (marked) marked.scrollIntoView({ block: 'center' });
  } catch (err) {
    $('#drawer-body').innerHTML = `<p class="empty">${esc(err.message || err)}</p>`;
  }
}

$('#drawer-close').addEventListener('click', hideDrawer);
scrim.addEventListener('click', hideDrawer);
$('#prev-page').addEventListener('click', () => { if (state.page > 1) openPage(state.page - 1); });
$('#next-page').addEventListener('click', () => { if (state.page) openPage(state.page + 1); });

/* ------------------------------- the list ------------------------------- */

const findResults = $('#find-results');
const lettersEl = $('#letters');
const searchInput = $('#q');

for (const ch of '#ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('')) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = ch;
  b.setAttribute('aria-pressed', 'false');
  b.addEventListener('click', () => {
    const already = b.getAttribute('aria-pressed') === 'true';
    $$('#letters button').forEach((x) => x.setAttribute('aria-pressed', 'false'));
    searchInput.value = '';
    state.query = '';
    if (already) { state.letter = ''; }
    else { b.setAttribute('aria-pressed', 'true'); state.letter = ch === '#' ? '' : ch; }
    loadBrowse(state.letter);
  });
  lettersEl.appendChild(b);
}

function medicineRow(m, query) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'mrow';
  b.innerHTML = `
    <span class="m-name">${hl(m.name, query)}</span>
    <span class="m-form">${esc(m.form || '—')}</span>
    <span class="m-page">p.${m.page}</span>
    ${m.section ? `<span class="m-section">${esc(m.section)}</span>` : ''}`;
  b.addEventListener('click', () => openMedicine(m.name));
  return b;
}

function passageCard(h, query) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'passage';
  b.innerHTML = `
    <span class="p-head">
      <span class="p-page">Page ${h.page}</span>
      <span class="p-section">${esc(h.heading || '')}</span>
    </span>
    <span class="p-text">${hl(h.text || '', query)}</span>`;
  b.addEventListener('click', () => openPage(h.page, query));
  return b;
}

function groupHead(title, count, action) {
  const head = document.createElement('div');
  head.className = 'group-head';
  head.innerHTML = `<h2>${esc(title)} <em>${count}</em></h2>`;
  if (action) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'group-action';
    btn.textContent = action.label;
    btn.addEventListener('click', action.onClick);
    head.appendChild(btn);
  }
  return head;
}

function renderList(items, query) {
  const wrap = document.createElement('div');
  wrap.className = 'mlist';
  items.forEach((m) => wrap.appendChild(medicineRow(m, query)));
  return wrap;
}

async function loadBrowse(letter = '') {
  findResults.innerHTML = '<p class="empty">Loading the index…</p>';
  try {
    const d = await getJSON(`/api/medicines?limit=400${letter ? `&letter=${letter}` : ''}`);
    state.medicines = d.items;
    renderBrowse(d.items, letter);
  } catch {
    findResults.innerHTML = '<p class="empty">Could not load the index.</p>';
  }
}

function renderBrowse(items, letter = '') {
  findResults.innerHTML = '';
  if (!items.length) {
    findResults.innerHTML = '<p class="empty">No medicines under that letter.</p>';
    return;
  }
  const label = letter ? `Beginning with ${letter.toUpperCase()}` : 'All medicines';
  findResults.appendChild(groupHead(label, items.length));
  findResults.appendChild(renderList(items, ''));
}

let searchTimer = null;
let searchAbort = null;

function onSearchInput() {
  const q = searchInput.value.trim();
  state.query = q;
  state.letter = '';
  $$('#letters button').forEach((x) => x.setAttribute('aria-pressed', 'false'));
  lettersEl.classList.toggle('hidden', q.length > 0);

  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runFind(q), 180);
}

async function runFind(q) {
  if (!q) { renderBrowse(state.medicines || [], ''); return; }

  findResults.innerHTML = '<p class="empty">Searching…</p>';
  if (searchAbort) searchAbort.abort();
  searchAbort = new AbortController();

  try {
    const [meds, pass] = await Promise.all([
      getJSON(`/api/medicines?limit=400&q=${encodeURIComponent(q)}`, { signal: searchAbort.signal }),
      getJSON(`/api/search?k=6&q=${encodeURIComponent(q)}`, { signal: searchAbort.signal }),
    ]);
    renderFind(q, meds.items || [], pass.hits || []);
  } catch (err) {
    if (err.name === 'AbortError') return;
    findResults.innerHTML =
      `<p class="empty">Search failed (${esc(err.name || 'Error')}: ${esc(err.message || err)}).</p>`;
  }
}

function renderFind(q, meds, passages) {
  findResults.innerHTML = '';

  if (!meds.length && !passages.length) {
    findResults.innerHTML =
      `<p class="empty">Nothing in this edition matches “${esc(q)}”.<br>
       <button class="group-action" id="ask-empty">Ask the assistant instead →</button></p>`;
    $('#ask-empty')?.addEventListener('click', () => {
      setView('ask', { focus: true });
      $('#question').value = q;
    });
    return;
  }

  if (meds.length) {
    const shown = meds.slice(0, 8);
    findResults.appendChild(groupHead(
      'Medicines', meds.length,
      meds.length > shown.length
        ? { label: `Show all ${meds.length}`, onClick: () => {
            findResults.replaceChildren();
            findResults.appendChild(groupHead('Medicines', meds.length));
            findResults.appendChild(renderList(meds, q));
          } }
        : null));
    findResults.appendChild(renderList(shown, q));
  }

  if (passages.length) {
    findResults.appendChild(groupHead(
      'Passages in the guideline', passages.length,
      { label: 'Ask this instead →', onClick: () => {
          setView('ask', { focus: true });
          $('#question').value = q;
        } }));

    const wrap = document.createElement('div');
    wrap.className = 'passages';
    passages.forEach((h) => wrap.appendChild(passageCard(h, q)));
    findResults.appendChild(wrap);
  }
}

searchInput.addEventListener('input', onSearchInput);

async function openMedicine(name) {
  state.lastFocus = document.activeElement;
  $('#drawer-kicker').textContent = 'Medicine';
  $('#drawer-title').textContent = name;
  $('#drawer-body').innerHTML = '<p class="empty">Loading…</p>';
  $('#prev-page').disabled = true;
  $('#next-page').disabled = true;
  showDrawer();
  drawer.focus();

  try {
    const r = await fetch(`/api/medicine?name=${encodeURIComponent(name)}`);
    if (!r.ok) throw new Error('Not in the digitised index.');
    const d = await r.json();
    const rows = d.rows.map((row) => `
      <tr><td>${esc(row.raw || row.name)}</td><td>p.${row.page}</td></tr>`).join('');
    $('#drawer-kicker').textContent = d.form || 'Medicine';
    const excerpts = d.excerpts.map((ex) => `
      <h3 class="kicker" style="margin:16px 0 6px">Page ${ex.page}</h3>
      <div class="pagetext">${esc(ex.text)}</div>`).join('');
    $('#drawer-body').innerHTML = `
      <table class="rowtable">${rows}</table>${excerpts}`;
  } catch (err) {
    $('#drawer-body').innerHTML = `<p class="empty">${esc(err.message || err)}</p>`;
  }
}

/* ------------------------------ keyboard ------------------------------ */

document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);

  if (e.key === 'Escape') {
    if (!drawer.hidden) { hideDrawer(); return; }
    if (typing && document.activeElement === searchInput && searchInput.value) {
      searchInput.value = '';
      onSearchInput();
      return;
    }
    if (typing) { document.activeElement.blur(); return; }
  }

  // "/" focuses search from anywhere that is not a text field
  if (e.key === '/' && !typing && drawer.hidden) {
    e.preventDefault();
    setView('list', { focus: true });
    return;
  }

  // move through results with the arrow keys, as a list should
  if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && state.view === 'list') {
    const items = $$('#find-results .mrow, #find-results .passage, #find-results .source');
    if (!items.length) return;
    e.preventDefault();
    const at = items.indexOf(document.activeElement);
    let nextIndex;
    if (at === -1) nextIndex = e.key === 'ArrowDown' ? 0 : items.length - 1;
    else nextIndex = Math.min(items.length - 1, Math.max(0, at + (e.key === 'ArrowDown' ? 1 : -1)));
    items[nextIndex].focus();
    items[nextIndex].scrollIntoView({ block: 'nearest' });
  }

  if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !drawer.hidden && !typing) {
    if (!state.page) return;
    if (e.key === 'ArrowLeft' && state.page > 1) openPage(state.page - 1);
    if (e.key === 'ArrowRight') openPage(state.page + 1);
  }
});

/* citation + chip delegation */
document.addEventListener('click', (e) => {
  const s = e.target.closest('.chip-suggest');
  if (s) { ask(s.textContent.trim()); return; }

  const r = e.target.closest('.recent-item');
  if (r) { ask(r.textContent.trim()); return; }

  const cite = e.target.closest('.cite');
  if (cite) {
    e.preventDefault();
    if (cite.dataset.page) openPage(Number(cite.dataset.page), state.query);
    else if (cite.dataset.web) {
      const i = Number(cite.dataset.web) - 1;
      if (webCache[i]) window.open(webCache[i].url, '_blank', 'noopener');
    }
  }
});

/* --------------------------------- boot --------------------------------- */

async function loadRecent() {
  try {
    const r = await fetch('/api/history?limit=4');
    const d = await r.json();
    if (!d.items.length) return;
    const list = $('#recent-list');
    d.items.forEach((it) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'recent-item';
      b.textContent = it.question;
      list.appendChild(b);
    });
    $('#recent').classList.remove('hidden');
  } catch { /* history is a nicety */ }
}

(async function boot() {
  const params = new URLSearchParams(location.search);

  try {
    const s = await (await fetch('/api/status')).json();
    const dot = $('#status-dot');
    const text = $('#status-text');
    const badge = $('#edition-badge');

    if (s.caimex?.reachable) {
      dot.classList.add('ok');
      text.textContent = 'ready';
      text.title = `CAIMEX gateway answered in ${s.caimex.ms} ms`;
    } else {
      dot.classList.add('bad');
      text.textContent = 'offline';
    }

    if (s.ready) {
      badge.textContent = s.corpus.edition;
      badge.title = `${s.corpus.pages} pages · ${s.corpus.chunks} passages · ${s.models.chat}`;
      if (s.medicines) $('#list-count').textContent = s.medicines;
    } else {
      badge.textContent = 'no corpus';
    }

    if (s.edition_drift) {
      const d = $('#drift');
      d.textContent =
        `The loaded corpus is “${s.corpus.edition}” but the app is configured for “${s.configured_edition}”. Re-ingest to match.`;
      d.classList.remove('hidden');
    }
  } catch {
    $('#status-text').textContent = 'offline';
  }

  loadRecent();

  const q = params.get('q');
  const page = params.get('page');

  if (q) {
    setView('list');
    searchInput.value = q;
    onSearchInput();
  } else if (location.hash === '#list') {
    setView('list');
  }

  if (page) openPage(Number(page), q || '');
})();
