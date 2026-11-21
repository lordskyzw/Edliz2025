/* EDLIZ Assistant - no framework, no build step. */

const $ = (sel) => document.querySelector(sel);
const messages = $('#messages');
const form = $('#ask-form');
const input = $('#question');
const sendBtn = $('#send');

let busy = false;

/* ----------------------------- helpers ----------------------------- */

const esc = (s) => s.replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

/** Deliberately small Markdown subset: headings, lists, bold, code, links. */
function md(src) {
  let text = esc(src);
  text = text.replace(/^### (.*)$/gm, '<h3>$1</h3>');
  text = text.replace(/^## (.*)$/gm, '<h2>$1</h2>');
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  const lines = text.split('\n');
  const out = [];
  let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const line of lines) {
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

/** Turn [p.42] and [W1] into clickable chips. */
function citations(html) {
  return html
    .replace(/\[p\.(\d+)\]/g, '<span class="cite" data-page="$1">p.$1</span>')
    .replace(/\[W(\d+)\]/g, '<a class="cite web" href="#" data-web="$1">W$1</a>');
}

function scrollDown(force = false) {
  const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 160;
  if (force || nearBottom) messages.scrollTop = messages.scrollHeight;
}

/* ----------------------------- chat ----------------------------- */

function addUser(text) {
  const el = document.createElement('div');
  el.className = 'msg user';
  el.innerHTML = `<div class="who">You</div><div class="bubble">${esc(text)}</div>`;
  messages.appendChild(el);
  scrollDown(true);
}

function addAssistant() {
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML = `
    <div class="who">EDLIZ Assistant</div>
    <div class="steps"></div>
    <div class="bubble hidden"></div>`;
  messages.appendChild(el);
  return {
    root: el,
    steps: el.querySelector('.steps'),
    bubble: el.querySelector('.bubble'),
  };
}

function stepNode(label) {
  const el = document.createElement('div');
  el.className = 'step';
  el.innerHTML = `<span class="spin"></span><span>${esc(label)}</span>`;
  return el;
}

function stepLabel(name, args) {
  if (name === 'search_edliz') return `Searching EDLIZ — “${args.query || ''}”`;
  if (name === 'lookup_medicine') return `Looking up ${args.name || 'medicine'}`;
  if (name === 'web_search') return `Searching the web — “${args.query || ''}”`;
  if (name === 'fetch_page') return 'Reading a web page';
  return name;
}

async function ask(question) {
  if (busy || !question.trim()) return;
  busy = true;
  sendBtn.disabled = true;
  const welcome = $('.welcome');
  if (welcome) welcome.remove();

  addUser(question);
  const node = addAssistant();
  let answer = '';
  let webResults = [];
  let pendingStep = null;
  let lastSteps = [];

  try {
    const res = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    const handle = (raw) => {
      const evLine = raw.match(/^event:\s*(.*)$/m);
      const dataLine = raw.match(/^data:\s*(.*)$/m);
      if (!dataLine) return;
      const kind = evLine ? evLine[1].trim() : 'message';
      let data;
      try { data = JSON.parse(dataLine[1]); } catch { return; }

      if (kind === 'step') {
        if (pendingStep) { pendingStep.classList.add('done'); pendingStep = null; }
        if (data.kind === 'retrieve') {
          const el = stepNode(data.count
            ? `Read ${data.count} passages from ${data.pages.length} pages`
            : 'No matching passages');
          el.classList.add('done');
          node.steps.appendChild(el);
        } else if (data.kind === 'tool') {
          pendingStep = stepNode(stepLabel(data.name, data.args || {}));
          node.steps.appendChild(pendingStep);
        } else if (data.kind === 'tool_error') {
          const el = stepNode('Tool call failed');
          el.classList.add('done');
          node.steps.appendChild(el);
        }
        lastSteps.push(data);
        scrollDown();
      } else if (kind === 'token') {
        if (pendingStep) { pendingStep.classList.add('done'); pendingStep = null; }
        answer += data.text || '';
        node.bubble.classList.remove('hidden');
        node.bubble.innerHTML = citations(md(answer));
        scrollDown();
      } else if (kind === 'done') {
        webResults = data.web || [];
        renderSources(data.hits || [], webResults);
        const meta = document.createElement('div');
        meta.className = 'step done';
        meta.innerHTML = `<span class="spin"></span><span>${data.pages.length
          ? 'Pages ' + data.pages.join(', ') + ' · ' : ''}${(data.ms / 1000).toFixed(1)}s</span>`;
        node.steps.appendChild(meta);
      } else if (kind === 'error') {
        node.bubble.classList.remove('hidden');
        node.bubble.innerHTML = `<p><strong>Something went wrong.</strong> ${esc(data.message || '')}</p>`;
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        handle(buffer.slice(0, idx));
        buffer = buffer.slice(idx + 2);
      }
    }
    if (pendingStep) pendingStep.classList.add('done');
    if (!answer) {
      node.bubble.classList.remove('hidden');
      node.bubble.innerHTML = '<p>No answer came back. Check the status indicator and try again.</p>';
    }
  } catch (err) {
    node.bubble.classList.remove('hidden');
    node.bubble.innerHTML = `<p><strong>Request failed.</strong> ${esc(String(err.message || err))}</p>`;
  } finally {
    busy = false;
    sendBtn.disabled = false;
    scrollDown(true);
  }
}

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
  input.style.height = Math.min(input.scrollHeight, 180) + 'px';
});

document.addEventListener('click', (e) => {
  const s = e.target.closest('.chip-suggest');
  if (s) { ask(s.textContent.trim()); return; }

  const cite = e.target.closest('.cite');
  if (cite) {
    e.preventDefault();
    if (cite.dataset.page) openPage(Number(cite.dataset.page));
    else if (cite.dataset.web) {
      const i = Number(cite.dataset.web) - 1;
      if (webResultsCache[i]) window.open(webResultsCache[i].url, '_blank', 'noopener');
    }
  }
});

/* ----------------------------- sources panel ----------------------------- */

let webResultsCache = [];

function renderSources(hits, web) {
  webResultsCache = web;
  const list = $('#sources-list');
  const hint = $('#sources-hint');
  list.innerHTML = '';
  hint.textContent = hits.length ? 'Click a page to read the source text.' : 'No passages matched.';

  hits.forEach((h) => {
    const el = document.createElement('div');
    el.className = 'source';
    el.innerHTML = `
      <div class="pnum">Page ${h.page}</div>
      <div class="shead">${esc(h.heading || '')}</div>
      <div class="snippet">${esc(h.text || '')}</div>`;
    el.addEventListener('click', () => openPage(h.page));
    list.appendChild(el);
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
    a.innerHTML = `<strong>W${i + 1}</strong> ${esc(w.title)}<div class="snippet">${esc(w.snippet || '')}</div>`;
    webList.appendChild(a);
  });
}

/* ----------------------------- drawer ----------------------------- */

async function openPage(n) {
  const drawer = $('#drawer');
  $('#drawer-title').textContent = `Page ${n}`;
  $('#drawer-body').innerHTML = '<p class="hint">Loading…</p>';
  drawer.classList.remove('hidden');
  try {
    const r = await fetch(`/api/page/${n}`);
    if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
    const d = await r.json();
    $('#drawer-title').textContent = `Page ${d.page}${d.heading ? ' · ' + d.heading : ''}`;
    $('#drawer-body').innerHTML = `<div class="pagetext">${esc(d.text)}</div>`;
  } catch (err) {
    $('#drawer-body').innerHTML = `<p class="hint">Could not load the page: ${esc(String(err.message || err))}</p>`;
  }
}

$('#drawer-close').addEventListener('click', () => $('#drawer').classList.add('hidden'));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') $('#drawer').classList.add('hidden');
});

/* ----------------------------- medicines list ----------------------------- */

const listBody = $('#list-body');
const listMeta = $('#list-meta');
const listQ = $('#list-q');
const lettersEl = $('#list-letters');
let listLoaded = false;
let listTimer = null;

for (const ch of '#ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('')) {
  const b = document.createElement('button');
  b.textContent = ch;
  b.addEventListener('click', () => {
    lettersEl.querySelectorAll('button').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    listQ.value = '';
    loadList({ letter: ch === '#' ? '' : ch });
  });
  lettersEl.appendChild(b);
}

listQ.addEventListener('input', (e) => {
  clearTimeout(listTimer);
  const q = e.target.value.trim();
  lettersEl.querySelectorAll('button').forEach((x) => x.classList.remove('active'));
  listTimer = setTimeout(() => loadList(q ? { q } : {}), 220);
});

async function loadList({ letter = '', q = '' } = {}) {
  listMeta.textContent = 'Loading…';
  const params = new URLSearchParams({ limit: '400' });
  if (q) params.set('q', q);
  else if (letter) params.set('letter', letter);

  let data;
  try {
    const r = await fetch(`/api/medicines?${params}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    data = await r.json();
  } catch (err) {
    listBody.innerHTML = '<tr><td colspan="4" class="empty">Could not load the list.</td></tr>';
    listMeta.textContent = 'Unavailable';
    return;
  }

  const items = data.items || [];
  const label = q ? `“${q}”`
    : letter ? `names starting with ${letter.toUpperCase()}`
    : 'the full index';
  listMeta.textContent = items.length
    ? `${items.length} ${items.length === 1 ? 'entry' : 'entries'} — ${label}`
    : 'No entries';

  if (!items.length) {
    listBody.innerHTML = `<tr><td colspan="4" class="empty">Nothing in the index matches ${esc(label)}.</td></tr>`;
    return;
  }

  listBody.innerHTML = '';
  const frag = document.createDocumentFragment();
  items.forEach((m) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="name">${esc(m.name)}</td>
      <td class="form">${esc(m.form || '—')}</td>
      <td class="section">${esc(m.section || '—')}</td>
      <td class="page">p.${m.page}</td>`;
    tr.addEventListener('click', () => openMedicine(m.name));
    frag.appendChild(tr);
  });
  listBody.appendChild(frag);
}

async function openMedicine(name) {
  const drawer = $('#drawer');
  $('#drawer-title').textContent = name;
  $('#drawer-body').innerHTML = '<p class="hint">Loading…</p>';
  drawer.classList.remove('hidden');
  try {
    const r = await fetch(`/api/medicine?name=${encodeURIComponent(name)}`);
    if (!r.ok) throw new Error('not found');
    const d = await r.json();
    const rows = d.rows.map((row) => `
      <tr><td>${esc(row.name)}</td>
          <td>${esc(row.raw || '')}</td>
          <td>p.${row.page}</td></tr>`).join('');
    const excerpts = d.excerpts.map((ex) => `
      <h3 style="font-size:13px;margin:16px 0 6px">Page ${ex.page}</h3>
      <div class="pagetext">${esc(ex.text)}</div>`).join('');
    $('#drawer-body').innerHTML = `
      <p class="hint">${esc(d.section || '')}</p>
      <table class="rowtable">${rows}</table>
      ${excerpts}`;
  } catch (err) {
    $('#drawer-body').innerHTML = '<p class="hint">Could not load this medicine.</p>';
  }
}

/* ----------------------------- view switch ----------------------------- */

document.querySelectorAll('.vtab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.vtab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
    tab.classList.add('active');
    $(`#view-${tab.dataset.view}`).classList.add('active');
    history.replaceState(null, '', `#${tab.dataset.view}`);
    if (tab.dataset.view === 'list' && !listLoaded) {
      listLoaded = true;
      loadList({});
    }
    if (tab.dataset.view === 'ask') setTimeout(() => scrollDown(true), 0);
  });
});

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.tabpane').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    $(`#tab-${tab.dataset.tab}`).classList.add('active');
  });
});

/* ----------------------------- boot ----------------------------- */

(async function boot() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    const dot = $('#status-dot');
    const text = $('#status-text');
    const badge = $('#edition-badge');

    if (s.caimex && s.caimex.reachable) {
      dot.classList.add('ok');
      text.textContent = `CAIMEX ${s.caimex.ms}ms`;
    } else {
      dot.classList.add('bad');
      text.textContent = 'CAIMEX unreachable';
    }

    if (s.ready) {
      badge.textContent = s.corpus.edition;
      badge.title = `${s.corpus.pages} pages · ${s.corpus.chunks} chunks · ${s.models.chat}`;
      if (s.medicines) $('#list-count').textContent = s.medicines;
    } else {
      badge.textContent = 'no corpus';
    }

    if (s.edition_drift) {
      const d = $('#drift');
      d.textContent = `The ingested corpus is “${s.corpus.edition}” but the app is configured for “${s.configured_edition}”. Re-ingest to match.`;
      d.classList.remove('hidden');
    }
  } catch {
    $('#status-text').textContent = 'offline';
  }

  const wanted = location.hash.replace('#', '');
  const tab = document.querySelector(`.vtab[data-view="${wanted}"]`);
  if (tab) tab.click();
})();
