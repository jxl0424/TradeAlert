// TradeAlert frontend. No build step; hash routing; all API strings escaped before insertion.
const $ = (s, r = document) => r.querySelector(s);
const app = $('#app');

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const isNum = (n) => typeof n === 'number' && Number.isFinite(n);
const safeUrl = (u) => (typeof u === 'string' && /^https?:\/\//i.test(u) ? u : null);
const link = (url, text) => {
  const u = safeUrl(url);
  return u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : esc(text);
};

const fmt = {
  num: (n, d = 2) => (isNum(n) ? n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—'),
  price: (n) => (isNum(n) ? '$' + n.toFixed(n < 1 ? 4 : 2) : '—'),
  pct: (n, d = 1) => (isNum(n) ? (n > 0 ? '+' : '') + (n * 100).toFixed(d) + '%' : '—'),
  pctPlain: (n, d = 1) => (isNum(n) ? (n * 100).toFixed(d) + '%' : '—'),
  big: (n) => {
    if (!isNum(n)) return '—';
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (a >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (a >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toFixed(0);
  },
  usd: (n) => (isNum(n) ? '$' + fmt.big(n) : '—'),
  mult: (n) => (isNum(n) ? n.toFixed(1) + 'x' : '—'),
  int: (n) => (isNum(n) ? Math.round(n).toString() : '—'),
  dt: (s) => {
    if (!s) return '—';
    const d = new Date(s);
    return isNaN(d) ? esc(s) : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  },
  date: (s) => {
    if (!s) return '—';
    const d = new Date(String(s).length <= 10 ? s + 'T00:00:00' : s);
    return isNaN(d) ? esc(s) : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  },
};
const signCls = (n) => (!isNum(n) || n === 0 ? '' : n > 0 ? 'up' : 'down');
const pctCell = (n, d) => `<span class="${signCls(n)}">${fmt.pct(n, d)}</span>`;

/* ---------- static (GitHub Pages) mode ----------
   site-config.js (written by `run.py export-site`) sets window.TRADEALERT_STATIC. In live mode the
   server serves an empty one, so STATIC is null and everything below behaves exactly as before.
   All URLs are relative so the page works from a sub-path such as /TradeAlert/. */
const STATIC = (typeof window.TRADEALERT_STATIC === 'object' && window.TRADEALERT_STATIC) || null;
// Must match safe_name() in backend/tradealert/export_site.py.
const safeName = (t) => {
  const bytes = new TextEncoder().encode(String(t).toUpperCase());
  let out = '';
  bytes.forEach((b, i) => {
    const ch = String.fromCharCode(b);
    out += b < 128 && /[A-Z0-9.\-]/.test(ch) && !(i === 0 && ch === '.') ? ch : '_' + b.toString(16).padStart(2, '0');
  });
  return out || '_';
};
const ghRepo = STATIC && /^[\w.-]+\/[\w.-]+$/.test(STATIC.repo || '') ? STATIC.repo : '';
const ghUrl = (workflow) => (ghRepo ? `https://github.com/${ghRepo}/actions${workflow ? '/workflows/' + workflow : ''}` : '');
const ghLink = (label, workflow, cls = '') => {
  const u = ghUrl(workflow);
  return u ? `<a class="btn ${cls}" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : '';
};

async function staticGet(path) {
  const u = new URL(path, 'http://x');
  const seg = u.pathname.replace(/^\/api\//, '').split('/').map(decodeURIComponent);
  const q = u.searchParams;
  const file = async (rel) => {
    const r = await fetch(rel);
    if (!r.ok) { const e = new Error(`${r.status} ${r.statusText}`); e.status = r.status; throw e; }
    return r.json();
  };
  if (seg[0] === 'candidates' && seg.length === 1) {
    const d = await file(q.get('scan_id') ? `api/candidates/${encodeURIComponent(q.get('scan_id'))}.json` : 'api/candidates/latest.json');
    const min = q.get('min_score') === null || q.get('min_score') === '' ? null : Number(q.get('min_score'));
    const v = q.get('verdict');
    if (v || min !== null) d.candidates = (d.candidates || []).filter((c) => (!v || c.verdict === v) && (min === null || (isNum(c.score) && c.score >= min)));
    return d;
  }
  if (seg[0] === 'candidates' && seg.length === 2) {
    let sid = q.get('scan_id');
    if (!sid) { const l = await file('api/candidates/latest.json'); sid = l.scan && l.scan.id; }
    if (sid === null || sid === undefined) { const e = new Error('404 no completed scans yet'); e.status = 404; throw e; }
    return file(`api/candidates/${encodeURIComponent(sid)}/${encodeURIComponent(safeName(seg[1]))}.json`);
  }
  return file('api/' + seg.map(encodeURIComponent).join('/') + '.json');
}

async function api(path, opts) {
  if (STATIC) {
    if (opts && opts.method && opts.method.toUpperCase() !== 'GET') {
      const e = new Error('This page is a read-only snapshot. Start scans and experiments on GitHub.');
      e.status = 405;
      throw e;
    }
    return staticGet(path);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = '', raw;
    try { const j = await res.json(); raw = j.detail ?? j.message; detail = j.detail || j.message || ''; } catch {}
    const e = new Error(`${res.status} ${res.statusText}${detail ? ': ' + (typeof detail === 'string' ? detail : JSON.stringify(detail)) : ''}`);
    e.status = res.status;
    e.detail = raw;
    throw e;
  }
  const txt = await res.text();
  return txt ? JSON.parse(txt) : null;
}

const verdictBadge = (v, big) => {
  const known = ['STRONG', 'WATCH', 'WEAK', 'AVOID'].includes(v);
  return `<span class="badge v-${known ? v : 'OTHER'}${big ? ' big' : ''}">${esc(v || 'UNKNOWN')}</span>`;
};
const SEV_ORDER = { high: 0, medium: 1, low: 2 };
const sevOf = (s) => (SEV_ORDER[s] === undefined ? 'low' : s);
const sortFlags = (f) => [...(f || [])].sort((a, b) => SEV_ORDER[sevOf(a.severity)] - SEV_ORDER[sevOf(b.severity)]);

/* ---------- state ---------- */
const state = {
  scans: [], scanId: null, health: null,
  filters: { verdict: '', minScore: '', q: '', hideAvoid: false },
  sort: { key: 'score', dir: -1 },
  perfSort: { key: 'scan_date', dir: -1 },
  running: false, pollTimer: null, expTimer: null, pk: null,
};
let renderToken = 0;

/* ---------- header / scan runner ---------- */
function renderTopbar() {
  const h = state.health;
  const last = h ? fmt.dt(h.last_scan_at) : '—';
  const opts = state.scans.map((s) => `<option value="${esc(s.id)}" ${String(s.id) === String(state.scanId) ? 'selected' : ''}>${esc(s.scan_date)} (${esc(s.candidates_count ?? '—')})</option>`).join('');
  $('#topbar-right').innerHTML = `
    <span class="muted">Last scan: <b>${last}</b>${h && h.ai_enabled === false ? ' · AI off' : ''}</span>
    ${state.scans.length ? `<label class="muted">Scan date <select id="scan-select" aria-label="Scan date">${opts}</select></label>` : ''}
    ${STATIC ? (ghLink('Start on GitHub', 'scan.yml', 'primary') || '<span class="muted">Read-only snapshot</span>')
      : `<button class="primary" id="run-btn" ${state.running ? 'disabled' : ''}>${state.running ? 'Scan running…' : 'Run scan'}</button>`}`;
  const sel = $('#scan-select');
  if (sel) sel.onchange = () => { state.scanId = sel.value; if (location.hash.startsWith('#/t/')) location.hash = '#/'; else render(); };
  if (!STATIC) $('#run-btn').onclick = startScan;
}

function setBanner(st) {
  const el = $('#run-banner');
  if (!st) { el.hidden = true; el.innerHTML = ''; return; }
  const pct = isNum(st.progress) ? Math.round(st.progress * 100) : 0;
  el.hidden = false;
  el.innerHTML = `<b>Scanning</b> · ${esc(st.stage || 'starting')} · ${esc(st.message || '')} <span class="muted">(${pct}%)</span>
    <div class="progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><div style="width:${pct}%"></div></div>`;
}

async function startScan() {
  try {
    await api('/api/scans/run', { method: 'POST' });
  } catch (e) {
    if (e.status !== 409) { setBannerError('Could not start scan: ' + e.message); return; }
  }
  pollScan();
}
function setBannerError(msg) {
  const el = $('#run-banner');
  el.hidden = false;
  el.innerHTML = `<span class="down">${esc(msg)}</span>`;
}
function pollScan() {
  clearTimeout(state.pollTimer);
  state.running = true;
  renderTopbar();
  const tick = async () => {
    try {
      const st = await api('/api/scans/status');
      if (st.running) {
        setBanner(st);
        state.pollTimer = setTimeout(tick, 1500);
      } else {
        state.running = false;
        setBanner(null);
        await loadMeta(true);
        render();
      }
    } catch (e) {
      state.running = false; renderTopbar();
      setBannerError('Lost contact with scan status: ' + e.message);
    }
  };
  tick();
}

async function loadMeta(selectNewest) {
  const [h, s] = await Promise.allSettled([api('/api/health'), api('/api/scans')]);
  state.health = h.status === 'fulfilled' ? h.value : null;
  state.scans = s.status === 'fulfilled' && Array.isArray(s.value) ? s.value : [];
  if (STATIC && Array.isArray(STATIC.scan_ids)) state.scans = state.scans.filter((x) => STATIC.scan_ids.includes(x.id)); // only scans with exported candidate files
  if (selectNewest || !state.scans.some((x) => String(x.id) === String(state.scanId))) state.scanId = state.scans[0]?.id ?? null;
  renderTopbar();
}

/* ---------- router ---------- */
function render() {
  const hash = location.hash || '#/';
  document.querySelectorAll('[data-nav]').forEach((a) => a.classList.toggle('active', (hash.startsWith('#/performance') ? 'performance' : hash.startsWith('#/experiments') ? 'experiments' : 'home') === a.dataset.nav));
  const token = ++renderToken;
  destroyChart();
  stopExpTimer();
  let p;
  if (hash.startsWith('#/t/')) p = viewDetail(decodeURIComponent(hash.slice(4)).toUpperCase(), token);
  else if (hash.startsWith('#/performance')) p = viewPerformance(token);
  else if (hash.startsWith('#/experiments/')) p = viewExperiment(decodeURIComponent(hash.slice(14)), token);
  else if (hash.startsWith('#/experiments')) p = viewExperiments(token);
  else p = viewList(token);
  p.catch((e) => { if (token === renderToken) app.innerHTML = `<div class="error">Failed to load: ${esc(e.message)}</div>`; });
}
window.addEventListener('hashchange', () => { render(); window.scrollTo(0, 0); });
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => render());

/* ---------- list view ---------- */
const COLS = [
  { key: 'ticker', label: 'Ticker', get: (c) => c.ticker, text: true },
  { key: 'verdict', label: 'Verdict', get: (c) => ({ STRONG: 4, WATCH: 3, WEAK: 2, AVOID: 1 }[c.verdict] ?? 0) },
  { key: 'score', label: 'Score', num: true, get: (c) => c.score },
  { key: 'close', label: 'Close', num: true, get: (c) => c.close },
  { key: 'pct_change_1d', label: '1d', num: true, get: (c) => c.pct_change_1d },
  { key: 'pct_change_5d', label: '5d', num: true, get: (c) => c.pct_change_5d },
  { key: 'rel_volume', label: 'Rel vol', num: true, get: (c) => c.rel_volume },
  { key: 'dollar_volume', label: '$ vol', num: true, get: (c) => c.dollar_volume },
  { key: 'float_shares', label: 'Float', num: true, get: (c) => c.float_shares },
  { key: 'short_pct_float', label: 'Short %', num: true, get: (c) => c.short_pct_float },
  { key: 'risk', label: 'Risk flags', get: (c) => sortFlags(c.risk_flags).length ? 3 - SEV_ORDER[sevOf(sortFlags(c.risk_flags)[0].severity)] : 0 },
  { key: 'reason', label: 'Top reason', get: (c) => (c.top_reasons || [])[0] || '', text: true },
];

function sortRows(rows, cols, sort) {
  const col = cols.find((c) => c.key === sort.key) || cols[0];
  return [...rows].sort((a, b) => {
    const x = col.get(a), y = col.get(b);
    const xn = x === null || x === undefined || x === '', yn = y === null || y === undefined || y === '';
    if (xn && yn) return 0;
    if (xn) return 1; // nulls always last
    if (yn) return -1;
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * sort.dir;
  });
}
function headerCells(cols, sort) {
  return cols.map((c) => `<th class="sortable ${c.num ? 'num' : ''}" data-sort="${c.key}" aria-sort="${sort.key === c.key ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none'}">${esc(c.label)}${sort.key === c.key ? (sort.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('');
}

async function viewList(token) {
  app.innerHTML = '<p class="muted">Loading…</p>';
  if (!state.health && !state.scans.length) await loadMeta(true);
  if (token !== renderToken) return;
  if (!state.scans.length || state.scanId === null) {
    app.innerHTML = `<div class="panel empty"><h2>No scans yet</h2>
      <p>TradeAlert scans the US penny-stock universe, scores each candidate for spike potential and flags the risks.</p>
      ${STATIC ? `<p>No scan has been published yet. Run the <b>Daily scan</b> workflow on GitHub (Actions tab), then this page updates itself.</p>${ghLink('Start on GitHub', 'scan.yml', 'primary big')}`
        : '<p>Press <b>Run scan</b> in the top right to run your first scan. It can take a few minutes; progress shows at the top of the page.</p>'}</div>`;
    return;
  }
  const data = await api(`/api/candidates?scan_id=${encodeURIComponent(state.scanId)}`);
  if (token !== renderToken) return;
  const all = data.candidates || [];
  const scan = data.scan || {};
  app.innerHTML = `
    <div class="toolbar">
      <label>Verdict <select id="f-verdict"><option value="">All</option>${['STRONG', 'WATCH', 'WEAK', 'AVOID'].map((v) => `<option ${state.filters.verdict === v ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
      <label>Min score <input id="f-min" type="number" min="0" max="100" style="width:64px" value="${esc(state.filters.minScore)}"></label>
      <label>Search <input id="f-q" type="search" placeholder="Ticker or name" value="${esc(state.filters.q)}"></label>
      <label><input id="f-hide" type="checkbox" ${state.filters.hideAvoid ? 'checked' : ''}> Hide AVOID</label>
      <span class="count" id="count"></span>
    </div>
    <div class="table-wrap"><table class="clickable"><thead><tr>${headerCells(COLS, state.sort)}</tr></thead><tbody id="rows"></tbody></table></div>
    <p class="note" style="margin-top:8px">Scan ${esc(scan.scan_date || '')} · universe ${esc(scan.universe_size ?? '—')} · status ${esc(scan.status || '—')}</p>`;

  const draw = () => {
    const f = state.filters;
    const min = f.minScore === '' ? null : Number(f.minScore);
    const q = f.q.trim().toLowerCase();
    let rows = all.filter((c) =>
      (!f.verdict || c.verdict === f.verdict) &&
      (min === null || (isNum(c.score) && c.score >= min)) &&
      (!f.hideAvoid || c.verdict !== 'AVOID') &&
      (!q || (c.ticker || '').toLowerCase().includes(q) || (c.name || '').toLowerCase().includes(q)));
    rows = sortRows(rows, COLS, state.sort);
    $('#count').textContent = `${rows.length} of ${all.length} candidates`;
    $('#rows').innerHTML = rows.length ? rows.map(rowHtml).join('') : `<tr><td colspan="${COLS.length}" class="empty">${all.length ? 'No candidates match these filters.' : 'This scan produced no candidates.'}</td></tr>`;
  };
  draw();
  $('#f-verdict').onchange = (e) => { state.filters.verdict = e.target.value; draw(); };
  $('#f-min').oninput = (e) => { state.filters.minScore = e.target.value; draw(); };
  $('#f-q').oninput = (e) => { state.filters.q = e.target.value; draw(); };
  $('#f-hide').onchange = (e) => { state.filters.hideAvoid = e.target.checked; draw(); };
  $('thead').onclick = (e) => {
    const th = e.target.closest('th[data-sort]'); if (!th) return;
    const k = th.dataset.sort;
    state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (COLS.find((c) => c.key === k).text ? 1 : -1) };
    $('thead tr').innerHTML = headerCells(COLS, state.sort); draw();
  };
  $('#rows').onclick = (e) => { const tr = e.target.closest('tr[data-t]'); if (tr) location.hash = '#/t/' + encodeURIComponent(tr.dataset.t); };
  $('#rows').onkeydown = (e) => { if (e.key === 'Enter') { const tr = e.target.closest('tr[data-t]'); if (tr) location.hash = '#/t/' + encodeURIComponent(tr.dataset.t); } };
}

function rowHtml(c) {
  const flags = sortFlags(c.risk_flags);
  const sc = isNum(c.score) ? Math.max(0, Math.min(100, c.score)) : null;
  return `<tr data-t="${esc(c.ticker)}" tabindex="0">
    <td><div class="tk">${esc(c.ticker)}</div><div class="tk-name" title="${esc(c.name)}">${esc(c.name) || '—'}</div></td>
    <td>${verdictBadge(c.verdict)}</td>
    <td class="num"><div class="scorecell"><b>${fmt.int(c.score)}</b><div class="scorebar"><i style="width:${sc ?? 0}%"></i></div></div></td>
    <td class="num">${fmt.price(c.close)}</td>
    <td class="num">${pctCell(c.pct_change_1d)}</td>
    <td class="num">${pctCell(c.pct_change_5d)}</td>
    <td class="num">${fmt.mult(c.rel_volume)}</td>
    <td class="num">${fmt.usd(c.dollar_volume)}</td>
    <td class="num">${fmt.big(c.float_shares)}</td>
    <td class="num">${fmt.pctPlain(c.short_pct_float)}</td>
    <td><div class="chips">${flags.map((f) => `<span class="chip ${sevOf(f.severity)}" title="${esc(f.text)}">${sevOf(f.severity) === 'high' ? '! ' : ''}${esc(f.code)}</span>`).join('') || '<span class="muted">—</span>'}</div></td>
    <td class="reason">${esc((c.top_reasons || [])[0]) || '—'}</td></tr>`;
}

/* ---------- detail view ---------- */
let chartInst = null, chartRO = null;
function destroyChart() {
  if (chartRO) { chartRO.disconnect(); chartRO = null; }
  if (chartInst) { try { chartInst.remove(); } catch {} chartInst = null; }
}
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

function drawChart(bars) {
  const el = $('#chart');
  if (!el) return;
  if (!bars.length) { el.innerHTML = '<p class="muted">No price bars available.</p>'; return; }
  if (!window.LightweightCharts) { el.innerHTML = '<p class="muted">Chart library failed to load (needs network access to unpkg.com).</p>'; return; }
  const LW = window.LightweightCharts;
  const up = cssVar('--up'), down = cssVar('--down');
  chartInst = LW.createChart(el, {
    width: el.clientWidth, height: 380,
    layout: { background: { color: 'transparent' }, textColor: cssVar('--chart-text') },
    grid: { vertLines: { color: cssVar('--chart-grid') }, horzLines: { color: cssVar('--chart-grid') } },
    rightPriceScale: { borderColor: cssVar('--border') },
    timeScale: { borderColor: cssVar('--border') },
    crosshair: { mode: 0 },
  });
  const candles = chartInst.addCandlestickSeries({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false, priceFormat: { type: 'price', precision: 4, minMove: 0.0001 } });
  const clean = bars.filter((b) => b && b.time && [b.open, b.high, b.low, b.close].every(isNum));
  candles.setData(clean.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));
  candles.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: 0.25 } });
  const vol = chartInst.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol' });
  vol.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  vol.setData(clean.filter((b) => isNum(b.volume)).map((b) => ({ time: b.time, value: b.volume, color: (b.close >= b.open ? up : down) + '88' })));
  chartInst.timeScale().fitContent();
  chartRO = new ResizeObserver(() => chartInst && chartInst.applyOptions({ width: el.clientWidth }));
  chartRO.observe(el);
}

const kvVal = (v) => (v === null || v === undefined ? '—' : typeof v === 'number' ? (Number.isInteger(v) ? v.toLocaleString('en-US') : String(+v.toFixed(6))) : typeof v === 'object' ? esc(JSON.stringify(v)) : esc(v));

async function viewDetail(ticker, token) {
  app.innerHTML = '<p class="muted">Loading…</p>';
  if (!state.scans.length && !state.health) await loadMeta(true);
  const q = state.scanId !== null ? `?scan_id=${encodeURIComponent(state.scanId)}` : '';
  let d;
  try { d = await api(`/api/candidates/${encodeURIComponent(ticker)}${q}`); }
  catch (e) {
    if (token !== renderToken) return;
    app.innerHTML = `<a class="back" href="#/">← Back to scan</a><div class="error">${e.status === 404 ? `${esc(ticker)} is not in this scan.` : 'Failed to load: ' + esc(e.message)}</div>`;
    return;
  }
  if (token !== renderToken) return;
  const flags = sortFlags(d.risk_flags);
  const comps = d.components || [];
  const ai = d.ai_summary;
  const aiBlock = ai ? `<div class="ai">
      <div><h3 class="bull">Bull case</h3><p>${esc(ai.bull_case) || '—'}</p></div>
      <div><h3 class="bear">Bear case</h3><p>${esc(ai.bear_case) || '—'}</p></div>
      <div><h3>Catalyst read</h3><p>${esc(ai.catalyst_read) || '—'}</p></div>
      <div><h3 class="bear">Key risk</h3><p>${esc(ai.key_risk) || '—'}</p></div>
    </div><div class="ai-meta">${esc(ai.model) || 'model unknown'} · ${fmt.dt(ai.generated_at)} · AI-generated, verify independently</div>`
    : '<p class="ai-off">AI summary disabled — set ANTHROPIC_API_KEY</p>';
  const news = d.news || [], filings = d.filings || [], hist = d.history || [];
  const signals = Object.entries(d.signals || {});

  app.innerHTML = `
    <a class="back" href="#/">← Back to scan</a>
    <div class="headline">
      <div><h1>${esc(d.ticker)}</h1><div class="sub">${esc(d.name) || '—'} · ${esc(d.exchange) || '—'} · ${esc(d.sector) || '—'}</div></div>
      ${verdictBadge(d.verdict, true)}
      <div class="bigscore">${fmt.int(d.score)}<small> / 100</small></div>
      <div class="stats">
        <div class="stat"><b>${fmt.price(d.close)}</b><span>Close</span></div>
        <div class="stat"><b>${pctCell(d.pct_change_1d)}</b><span>1d</span></div>
        <div class="stat"><b>${pctCell(d.pct_change_5d)}</b><span>5d</span></div>
        <div class="stat"><b>${fmt.mult(d.rel_volume)}</b><span>Rel vol</span></div>
        <div class="stat"><b>${fmt.usd(d.dollar_volume)}</b><span>$ vol</span></div>
        <div class="stat"><b>${fmt.usd(d.market_cap)}</b><span>Mkt cap</span></div>
        <div class="stat"><b>${fmt.big(d.float_shares)}</b><span>Float</span></div>
        <div class="stat"><b>${fmt.pctPlain(d.short_pct_float)}</b><span>Short %</span></div>
      </div>
    </div>
    ${flags.length ? `<div class="flags" role="list" aria-label="Risk flags">${flags.map((f) => `<div class="flag ${sevOf(f.severity)}" role="listitem"><span class="sev">${esc(sevOf(f.severity))}</span><span>${esc(f.text)}<code>${esc(f.code)}</code></span></div>`).join('')}</div>` : ''}
    ${(d.top_reasons || []).length ? `<div class="panel"><h2>Why it was flagged</h2><ul class="list">${d.top_reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul></div>` : ''}
    <div class="grid2">
      <div>
        <div class="panel"><h2>Price · daily</h2><div id="chart"></div></div>
        <div class="panel"><h2>AI summary</h2>${aiBlock}</div>
        <div class="panel"><h2>News</h2>${news.length ? `<ul class="list">${news.map((n) => `<li>${link(n.url, n.headline || '(no headline)')}<div class="li-meta">${esc(n.source) || '—'} · ${fmt.dt(n.published_at)}</div></li>`).join('')}</ul>` : '<p class="muted">No recent news.</p>'}</div>
        <div class="panel"><h2>SEC filings</h2>${filings.length ? `<ul class="list">${filings.map((f) => {
          const cat = ['catalyst', 'dilution', 'risk', 'routine'].includes(f.category) ? f.category : 'routine';
          return `<li><span class="cat cat-${cat}">${cat === 'dilution' ? 'DILUTION' : esc(cat)}</span><b>${link(f.url, f.form || 'Filing')}</b> ${esc(f.description)}<div class="li-meta">${fmt.date(f.filed_at)}</div></li>`; }).join('')}</ul>` : '<p class="muted">No recent filings.</p>'}</div>
      </div>
      <div>
        <div class="panel"><h2>Score breakdown</h2>${comps.length ? comps.map((c) => {
          const pct = isNum(c.points) && isNum(c.max_points) && c.max_points > 0 ? Math.max(0, Math.min(100, (c.points / c.max_points) * 100)) : 0;
          return `<div class="comp"><span class="nm">${esc(c.name)}</span><div class="bar"><i style="width:${pct}%"></i></div><span class="pts">${fmt.num(c.points, 1)}/${fmt.num(c.max_points, 0)}</span><span class="why">${esc(c.reason)}</span></div>`; }).join('') : '<p class="muted">No breakdown available.</p>'}</div>
        <div class="panel"><h2>History</h2>${hist.length ? `<table><thead><tr><th>Scan date</th><th>Verdict</th><th class="num">Score</th></tr></thead><tbody>${hist.map((h) => `<tr><td>${esc(h.scan_date)}</td><td>${verdictBadge(h.verdict)}</td><td class="num">${fmt.int(h.score)}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">First appearance.</p>'}</div>
        <div class="panel"><h2>Raw signals</h2>${signals.length ? `<table class="kv"><tbody>${signals.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${kvVal(v)}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">No signals recorded.</p>'}</div>
      </div>
    </div>
`;
  drawChart(d.bars || []);
}

/* ---------- performance view ---------- */
const PCOLS = [
  { key: 'scan_date', label: 'Date', text: true, get: (p) => p.scan_date },
  { key: 'ticker', label: 'Ticker', text: true, get: (p) => p.ticker },
  { key: 'verdict', label: 'Verdict', text: true, get: (p) => p.verdict },
  { key: 'score', label: 'Score', num: true, get: (p) => p.score },
  { key: 'entry_close', label: 'Entry', num: true, get: (p) => p.entry_close },
  { key: 'ret_1d', label: '1d', num: true, get: (p) => p.ret_1d },
  { key: 'ret_3d', label: '3d', num: true, get: (p) => p.ret_3d },
  { key: 'ret_5d', label: '5d', num: true, get: (p) => p.ret_5d },
  { key: 'ret_10d', label: '10d', num: true, get: (p) => p.ret_10d },
  { key: 'max_gain_5d', label: 'Max gain 5d', num: true, get: (p) => p.max_gain_5d },
  { key: 'max_drawdown_5d', label: 'Max DD 5d', num: true, get: (p) => p.max_drawdown_5d },
];

async function viewPerformance(token) {
  app.innerHTML = '<p class="muted">Loading…</p>';
  const d = await api('/api/performance');
  if (token !== renderToken) return;
  const by = d.by_verdict || [], picks = d.picks || [];
  const thr = isNum(d.hit_threshold_pct) ? d.hit_threshold_pct : null;
  const order = ['STRONG', 'WATCH', 'WEAK', 'AVOID'];
  by.sort((a, b) => order.indexOf(a.verdict) - order.indexOf(b.verdict));
  const withData = by.some((v) => v.count > 0);
  app.innerHTML = `
    <h2 style="margin:0 0 4px">Track record</h2>
    <p class="note">How past picks actually performed after the scan close. Returns are measured from each pick's entry close.
    <b>Hit rate</b> = share of picks whose maximum gain within 5 trading days reached at least ${thr === null ? '—' : esc(thr) + '%'} above entry.
    Max drawdown is the worst dip below entry within the same 5 days.</p>
    ${!picks.length && !withData ? `<div class="panel empty"><h2>Not enough data yet</h2><p>Performance is filled in as days pass after each scan (1, 3, 5 and 10 trading days). Run scans for a few days and check back.</p></div>` : `
    <div class="cards">${by.map((v) => `<div class="card">${verdictBadge(v.verdict, true)} <span class="muted">${fmt.int(v.count)} picks</span>
      <dl><dt>Hit rate</dt><dd><b>${fmt.pctPlain(v.hit_rate, 0)}</b></dd>
      <dt>Avg 5d return</dt><dd>${pctCell(v.avg_close_return_5d)}</dd>
      <dt>Median 5d return</dt><dd>${pctCell(v.median_close_return_5d)}</dd>
      <dt>Avg max gain</dt><dd>${pctCell(v.avg_max_gain_5d)}</dd>
      <dt>Avg max drawdown</dt><dd>${pctCell(v.avg_max_drawdown_5d)}</dd></dl></div>`).join('')}</div>
    <div class="table-wrap"><table><thead><tr>${headerCells(PCOLS, state.perfSort)}</tr></thead><tbody id="prows"></tbody></table></div>`}`;
  if (!$('#prows')) return;
  const draw = () => {
    $('#prows').innerHTML = sortRows(picks, PCOLS, state.perfSort).map((p) => `<tr>
      <td>${esc(p.scan_date)}</td><td><a href="#/t/${encodeURIComponent(p.ticker)}"><b>${esc(p.ticker)}</b></a></td><td>${verdictBadge(p.verdict)}</td>
      <td class="num">${fmt.int(p.score)}</td><td class="num">${fmt.price(p.entry_close)}</td>
      ${['ret_1d', 'ret_3d', 'ret_5d', 'ret_10d', 'max_gain_5d', 'max_drawdown_5d'].map((k) => `<td class="num">${pctCell(p[k])}</td>`).join('')}</tr>`).join('') || `<tr><td colspan="${PCOLS.length}" class="empty">No picks recorded yet.</td></tr>`;
  };
  draw();
  $('thead').onclick = (e) => {
    const th = e.target.closest('th[data-sort]'); if (!th) return;
    const k = th.dataset.sort;
    state.perfSort = { key: k, dir: state.perfSort.key === k ? -state.perfSort.dir : (PCOLS.find((c) => c.key === k).text ? 1 : -1) };
    $('thead tr').innerHTML = headerCells(PCOLS, state.perfSort); draw();
  };
}

/* ---------- experiments ---------- */
const KIND_LABEL = { backtest: 'History run', forward: 'Forward test' };
const kindLabel = (k) => KIND_LABEL[k] || 'Experiment';
const STATUS_LABEL = { running: 'Running', waiting: 'Waiting', completed: 'Completed', failed: 'Failed' };
const statusBadge = (s) => `<span class="badge st-${STATUS_LABEL[s] ? s : 'other'}">${esc(STATUS_LABEL[s] || s || 'Unknown')}</span>`;
const expName = (e) => e.name || `${kindLabel(e.kind)} #${e.id}`;
const MIN_EVALUATED = 20;
const ENTRY_LABEL = { next_open: "the next trading day's open", close: 'the close of the signal day' };
const stopExpTimer = () => { clearTimeout(state.expTimer); state.expTimer = null; };
const expHref = (id) => '#/experiments/' + encodeURIComponent(id);

function detailText(e) {
  const d = e && e.detail;
  if (Array.isArray(d)) return d.map((x) => (x && x.msg ? `${Array.isArray(x.loc) ? x.loc.filter((l) => l !== 'body' && l !== 'params').join('.') + ': ' : ''}${x.msg}` : String(x))).join('; ');
  if (d && typeof d === 'object') return JSON.stringify(d);
  return d ? String(d) : (e && e.message) || 'Unknown error';
}

/* Plain-words verdict: only meaningful for completed experiments. */
function expVerdict(h, status) {
  if (status !== 'completed') return null;
  if (!h || !isNum(h.evaluated) || h.evaluated < MIN_EVALUATED) {
    return { cls: 'few', icon: '?', label: 'Too few picks to judge', why: `Only ${isNum(h && h.evaluated) ? h.evaluated : 0} scored picks; at least ${MIN_EVALUATED} are needed.` };
  }
  const sgn = (a, b, tol) => (isNum(a) && isNum(b) ? (a - b > tol ? 1 : a - b < -tol ? -1 : 0) : null);
  const ss = [sgn(h.hit_rate, h.baseline_hit_rate, 0.02), sgn(h.avg_return, h.baseline_avg_return, 0.005)].filter((x) => x !== null);
  if (!ss.length) return { cls: 'few', icon: '?', label: 'No baseline to compare', why: 'The baseline numbers are missing.' };
  const pos = ss.includes(1), neg = ss.includes(-1);
  if (pos && neg) return { cls: 'mixed', icon: '≈', label: 'Mixed against baseline', why: 'One measure is better than baseline and the other is worse.' };
  if (pos) return { cls: 'beat', icon: '▲', label: 'Beat baseline', why: 'Picks did better than all penny stocks on the same days.' };
  if (neg) return { cls: 'worse', icon: '▼', label: 'Worse than baseline', why: 'Picks did worse than all penny stocks on the same days.' };
  return { cls: 'none', icon: '≈', label: 'No edge over baseline', why: 'Picks did about as well as all penny stocks on the same days.' };
}
const verdictChip = (v) => (v ? `<span class="vd vd-${v.cls}" title="${esc(v.why)}"><span aria-hidden="true">${v.icon}</span> ${esc(v.label)}</span>` : '');

function describeParams(kind, p) {
  p = p || {};
  const hit = `A pick counts as a hit if its price rose at least ${isNum(p.hit_threshold_pct) ? esc(p.hit_threshold_pct) : '—'}% within ${isNum(p.horizon_days) ? esc(p.horizon_days) : '—'} trading days.`;
  const entry = ENTRY_LABEL[p.entry] ? `Entry at ${ENTRY_LABEL[p.entry]}.` : '';
  if (kind === 'backtest') {
    return `Replayed the scanner from <b>${fmt.date(p.start_date)}</b> to <b>${fmt.date(p.end_date)}</b>, taking the top ${isNum(p.top_n) ? esc(p.top_n) : '—'} picks per day that scored at least ${isNum(p.min_score) ? esc(p.min_score) : '—'}. ${entry} ${hit} SEC filings ${p.include_filings === false ? 'were not used' : 'were used'} in scoring.`;
  }
  const src = p.run_fresh_scan ? 'a fresh scan run at the start' : (p.scan_id !== null && p.scan_id !== undefined ? `scan #${esc(p.scan_id)}` : 'the latest scan');
  const vs = Array.isArray(p.verdicts) && p.verdicts.length ? p.verdicts.map(esc).join(' and ') : 'all';
  return `Froze the predictions from ${src}: ${vs} verdicts${isNum(p.min_score) ? `, score at least ${esc(p.min_score)}` : ''}. ${entry} Each is scored after ${isNum(p.horizon_days) ? esc(p.horizon_days) : '—'} trading days. ${hit}`;
}

const progressBar = (prog) => {
  if (!isNum(prog)) return '<div class="progress indet" role="progressbar" aria-label="Working"><div></div></div>';
  const pct = Math.max(0, Math.min(100, Math.round(prog * 100)));
  return `<div class="progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><div style="width:${pct}%"></div></div>`;
};

/* ---- list ---- */
function headlineHtml(h) {
  if (!h) return '<p class="muted">No results recorded.</p>';
  const row = (label, a, b, signed) => `<div class="hl-row"><span class="hl-l">${label}</span>
    <span class="hl-v"><b>${signed ? pctCell(a) : fmt.pctPlain(a, 0)}</b><small>picks</small></span>
    <span class="hl-v base"><b>${signed ? pctCell(b) : fmt.pctPlain(b, 0)}</b><small>baseline</small></span></div>`;
  return `<div class="hl">${row('Hit rate', h.hit_rate, h.baseline_hit_rate, false)}${row('Avg return', h.avg_return, h.baseline_avg_return, true)}
    <div class="hl-n muted">${fmt.int(h.evaluated)} of ${fmt.int(h.picks)} picks scored</div></div>`;
}

function expCard(e) {
  const v = expVerdict(e.headline, e.status);
  let body = '';
  if (e.status === 'running') body = `<div class="exp-run">${progressBar(e.progress)}<div class="muted">${esc(e.message) || 'Working…'}${isNum(e.progress) ? ` (${Math.round(e.progress * 100)}%)` : ''}</div></div>`;
  else if (e.status === 'waiting') body = `<p class="exp-wait"><b>Results due ${fmt.date(e.due_date)}</b><span class="muted">${e.headline && isNum(e.headline.picks) ? ` · ${fmt.int(e.headline.picks)} picks frozen` : ''}</span></p>`;
  else if (e.status === 'failed') body = `<p class="down exp-fail">${esc(e.message) || 'This experiment failed.'}</p>`;
  else body = headlineHtml(e.headline);
  return `<article class="exp-card" data-id="${esc(e.id)}">
    <div class="exp-top"><a class="exp-name" href="${esc(expHref(e.id))}">${esc(expName(e))}</a>${statusBadge(e.status)}</div>
    <div class="exp-meta muted"><span class="kind-tag">${esc(kindLabel(e.kind))}</span> · Created ${fmt.dt(e.created_at)}</div>
    ${v ? `<div class="exp-verdict">${verdictChip(v)}</div>` : ''}
    ${body}</article>`;
}

function newExpButton() {
  return STATIC ? (ghLink('Start on GitHub', 'experiment.yml', 'primary big') || '<span class="muted">Start experiments from the repository Actions tab</span>')
    : '<button class="primary big" data-new-exp>New experiment</button>';
}

function emptyExperiments() {
  return `<div class="panel empty"><h2>No experiments yet</h2>
    <p>Experiments test whether TradeAlert's picks are any good, by comparing them with all penny stocks on the same days.</p>
    <div class="kind-explain">
      <div><b>History run</b><p>Replays the scanner over a past date range and measures what its picks did afterwards. Quick, but uses hindsight data.</p></div>
      <div><b>Forward test</b><p>Freezes today's predictions, waits (5 trading days by default), then marks each one right or wrong. Slower, but no hindsight.</p></div>
    </div>
    <p>${newExpButton()}</p></div>`;
}

async function viewExperiments(token) {
  app.innerHTML = `<div class="page-head"><div><h2 style="margin:0">Experiments</h2>
      <p class="note" style="margin:2px 0 0">Is the tool's advice any good? Each experiment compares its picks with all penny stocks on the same days.</p></div>
      ${newExpButton()}</div>
    <div id="exp-list"><p class="muted">Loading…</p></div>`;
  app.querySelectorAll('[data-new-exp]').forEach((b) => { b.onclick = () => openNewExperiment(); });
  let last = null;
  const load = async (first) => {
    let list;
    try { list = await api('/api/experiments'); }
    catch (e) {
      if (token !== renderToken) return;
      if (first) { $('#exp-list').innerHTML = `<div class="error">Failed to load experiments: ${esc(e.message)}</div>`; return; }
      state.expTimer = setTimeout(() => load(false), 5000); return;
    }
    if (token !== renderToken) return;
    if (!Array.isArray(list)) list = [];
    const sig = JSON.stringify(list);
    if (sig !== last) {
      last = sig;
      $('#exp-list').innerHTML = list.length ? `<div class="exp-grid">${list.map(expCard).join('')}</div>` : emptyExperiments();
      app.querySelectorAll('#exp-list [data-new-exp]').forEach((b) => { b.onclick = () => openNewExperiment(); });
      app.querySelectorAll('.exp-card').forEach((c) => { c.onclick = (ev) => { if (!ev.target.closest('a,button')) location.hash = expHref(c.dataset.id); }; });
    }
    stopExpTimer();
    if (list.some((e) => e.status === 'running')) state.expTimer = setTimeout(() => load(false), 2000);
  };
  await load(true);
}

/* ---- new experiment dialog ---- */
const FALLBACK_DEFAULTS = {
  backtest: { horizon_days: 5, hit_threshold_pct: 20, top_n: 10, min_score: 45, entry: 'next_open', include_filings: true },
  forward: { run_fresh_scan: false, verdicts: ['STRONG', 'WATCH'], horizon_days: 5, hit_threshold_pct: 20, entry: 'next_open' },
};
async function loadExpDefaults() {
  let resp = null, ok = false;
  try { resp = await api('/api/experiments/defaults'); ok = true; } catch {}
  const out = { backtest: { defaults: {}, ranges: {} }, forward: { defaults: {}, ranges: {} }, fromServer: ok };
  for (const k of ['backtest', 'forward']) {
    const x = resp && typeof resp === 'object' ? resp[k] : null;
    let d = {}, r = {};
    if (x && typeof x === 'object') {
      d = x.defaults && typeof x.defaults === 'object' ? x.defaults : x;
      r = x.ranges && typeof x.ranges === 'object' ? x.ranges : {};
    }
    if (resp && resp.ranges && typeof resp.ranges === 'object' && resp.ranges[k] && typeof resp.ranges[k] === 'object') r = { ...resp.ranges[k], ...r };
    const clean = {};
    for (const [key, val] of Object.entries(d)) if (val !== null && val !== undefined && typeof val !== 'object' || key === 'verdicts') clean[key] = val;
    out[k].defaults = { ...FALLBACK_DEFAULTS[k], ...clean };
    if (!Array.isArray(out[k].defaults.verdicts) && k === 'forward') out[k].defaults.verdicts = FALLBACK_DEFAULTS.forward.verdicts;
    out[k].ranges = r;
  }
  return out;
}
const rangeOf = (ranges, key) => {
  const r = ranges && ranges[key];
  if (Array.isArray(r)) return { min: isNum(r[0]) ? r[0] : null, max: isNum(r[1]) ? r[1] : null };
  if (r && typeof r === 'object') return { min: isNum(r.min) ? r.min : null, max: isNum(r.max) ? r.max : null };
  return { min: null, max: null };
};
const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

function closeAnyDialog() { document.querySelectorAll('dialog.dlg').forEach((d) => { try { d.close(); } catch {} d.remove(); }); }

function confirmDialog(message, okLabel) {
  closeAnyDialog();
  return new Promise((resolve) => {
    const dlg = document.createElement('dialog');
    dlg.className = 'dlg small';
    dlg.setAttribute('aria-labelledby', 'cf-msg');
    dlg.innerHTML = `<div class="dlg-body"><p id="cf-msg" style="margin:0">${esc(message)}</p></div>
      <div class="dlg-foot"><button type="button" data-no>Cancel</button><button type="button" class="danger" data-yes>${esc(okLabel || 'Delete')}</button></div>`;
    document.body.appendChild(dlg);
    let res = false;
    dlg.querySelector('[data-yes]').onclick = () => { res = true; dlg.close(); };
    dlg.querySelector('[data-no]').onclick = () => dlg.close();
    dlg.addEventListener('close', () => { dlg.remove(); resolve(res); });
    dlg.showModal();
  });
}

async function openNewExperiment() {
  closeAnyDialog();
  const defs = await loadExpDefaults();
  const B = defs.backtest.defaults, F = defs.forward.defaults;
  const BR = defs.backtest.ranges, FR = defs.forward.ranges;
  const today = new Date();
  const endD = new Date(today); endD.setDate(endD.getDate() - 14);
  const startD = new Date(endD); startD.setDate(startD.getDate() - 90);
  const bStart = typeof B.start_date === 'string' ? B.start_date : ymd(startD);
  const bEnd = typeof B.end_date === 'string' ? B.end_date : ymd(endD);
  const val = (v) => (v === null || v === undefined ? '' : esc(v));
  const rangeAttrs = (rs, key, fb) => { const r = rangeOf(rs, key); const mn = r.min ?? fb.min, mx = r.max ?? fb.max; return `${mn !== undefined && mn !== null ? ` min="${esc(mn)}"` : ''}${mx !== undefined && mx !== null ? ` max="${esc(mx)}"` : ''}`; };
  const field = (id, label, input, help) => `<div class="field"><label for="${id}">${label}</label>${input}<div class="help" id="help-${id}">${help}</div><div class="ferr" id="err-${id}"></div></div>`;
  const numIn = (id, v, attrs, step) => `<input type="number" id="${id}" name="${id}" value="${val(v)}" step="${step || 'any'}"${attrs || ''} inputmode="decimal" aria-describedby="help-${id} err-${id}">`;
  const entrySel = (id, v) => `<select id="${id}" name="${id}" aria-describedby="help-${id}"><option value="next_open" ${v !== 'close' ? 'selected' : ''}>Next trading day's open (realistic)</option><option value="close" ${v === 'close' ? 'selected' : ''}>Close of the signal day</option></select>`;
  const entryHelp = "When we pretend to buy. The next day's open is more realistic, because you can't buy at a close you only see after the fact.";
  const scanOpts = state.scans.map((s) => `<option value="${esc(s.id)}">${esc(s.scan_date)} (${esc(s.candidates_count ?? '—')} candidates)</option>`).join('');
  const fv = Array.isArray(F.verdicts) ? F.verdicts : [];

  const dlg = document.createElement('dialog');
  dlg.className = 'dlg';
  dlg.setAttribute('aria-labelledby', 'dlg-title');
  dlg.innerHTML = `<form id="exp-form" novalidate>
    <div class="dlg-head"><h2 id="dlg-title">New experiment</h2><button type="button" class="x" data-close aria-label="Close">×</button></div>
    <div class="dlg-body">
      <fieldset class="kinds"><legend>What kind of experiment?</legend>
        <label class="kind-opt"><input type="radio" name="kind" value="backtest" checked><span><b>History run</b><small>Replays the scanner over a past date range and measures what its picks did afterwards.</small></span></label>
        <label class="kind-opt"><input type="radio" name="kind" value="forward"><span><b>Forward test</b><small>Freezes today's predictions, waits a few trading days, then marks each one right or wrong.</small></span></label>
      </fieldset>
      ${defs.fromServer ? '' : '<p class="note">Could not load the server defaults, so built-in ones are shown.</p>'}
      <div class="field"><label for="exp-name">Name <span class="muted">(optional)</span></label><input type="text" id="exp-name" name="name" maxlength="80" placeholder="e.g. Spring penny-stock check"></div>
      <div data-kind="backtest">
        <div class="two">
          ${field('bt_start_date', 'Start date', `<input type="date" id="bt_start_date" name="bt_start_date" value="${esc(bStart)}" aria-describedby="help-bt_start_date err-bt_start_date">`, 'First day the scanner is replayed on.')}
          ${field('bt_end_date', 'End date', `<input type="date" id="bt_end_date" name="bt_end_date" value="${esc(bEnd)}" aria-describedby="help-bt_end_date err-bt_end_date">`, 'Last day. Leave room after it so picks can be followed.')}
        </div>
        <div class="two">
          ${field('bt_horizon_days', 'Days to follow each pick', numIn('bt_horizon_days', B.horizon_days, rangeAttrs(BR, 'horizon_days', { min: 1 }), 1), 'Trading days after entry that we watch the price.')}
          ${field('bt_hit_threshold_pct', 'Hit threshold (%)', numIn('bt_hit_threshold_pct', B.hit_threshold_pct, rangeAttrs(BR, 'hit_threshold_pct', { min: 0 })), 'Hit = price rose at least this much within the days above.')}
        </div>
        <div class="two">
          ${field('bt_top_n', 'Picks per day', numIn('bt_top_n', B.top_n, rangeAttrs(BR, 'top_n', { min: 1 }), 1), 'Take the highest-scoring candidates each day, up to this many.')}
          ${field('bt_min_score', 'Minimum score', numIn('bt_min_score', B.min_score, rangeAttrs(BR, 'min_score', { min: 0, max: 100 })), 'Ignore candidates scoring below this (0 to 100).')}
        </div>
        ${field('bt_entry', 'Entry price', entrySel('bt_entry', B.entry), entryHelp)}
        <div class="field"><label class="check"><input type="checkbox" id="bt_include_filings" name="bt_include_filings" ${B.include_filings === false ? '' : 'checked'}> Use SEC filings when scoring</label><div class="help">More realistic, but the run takes longer.</div></div>
      </div>
      <div data-kind="forward" hidden>
        <div class="field"><label for="fw_scan_id">Which predictions to freeze</label>
          <select id="fw_scan_id" name="fw_scan_id" aria-describedby="help-fw_scan_id"><option value="">Latest scan</option>${scanOpts}</select>
          <label class="check sub"><input type="checkbox" id="fw_run_fresh_scan" name="fw_run_fresh_scan" ${F.run_fresh_scan ? 'checked' : ''}> Run a fresh scan now instead (takes a few minutes)</label>
          <div class="help" id="help-fw_scan_id">The picks are locked in today, so they cannot be tweaked after seeing the outcome.</div></div>
        <fieldset class="field vchecks"><legend>Include these verdicts</legend>
          ${['STRONG', 'WATCH', 'WEAK', 'AVOID'].map((v) => `<label class="check"><input type="checkbox" name="fw_verdicts" value="${v}" ${fv.includes(v) ? 'checked' : ''}> ${verdictBadge(v)}</label>`).join('')}
          <div class="help">Only candidates with these verdicts are tracked.</div><div class="ferr" id="err-fw_verdicts"></div></fieldset>
        <div class="two">
          ${field('fw_horizon_days', 'Days to wait', numIn('fw_horizon_days', F.horizon_days, rangeAttrs(FR, 'horizon_days', { min: 1 }), 1), 'Trading days before each pick is marked right or wrong.')}
          ${field('fw_hit_threshold_pct', 'Hit threshold (%)', numIn('fw_hit_threshold_pct', F.hit_threshold_pct, rangeAttrs(FR, 'hit_threshold_pct', { min: 0 })), 'Hit = price rose at least this much within the days above.')}
        </div>
        ${field('fw_min_score', 'Minimum score <span class="muted">(optional)</span>', numIn('fw_min_score', F.min_score, rangeAttrs(FR, 'min_score', { min: 0, max: 100 })), 'Ignore candidates scoring below this (0 to 100). Leave empty for no limit.')}
        ${field('fw_entry', 'Entry price', entrySel('fw_entry', F.entry), entryHelp)}
        <p class="note callout">Results become final after the due date, which is about <b id="fw-due-n">${esc(F.horizon_days)}</b> trading days from now. The app must be run again after then to score them; nothing is scored while it is closed.</p>
      </div>
      <div id="form-err" class="error" role="alert" hidden></div>
    </div>
    <div class="dlg-foot"><button type="button" data-close>Cancel</button><button type="submit" class="primary" id="exp-submit">Start experiment</button></div>
  </form>`;
  document.body.appendChild(dlg);
  dlg.addEventListener('close', () => dlg.remove());
  dlg.querySelectorAll('[data-close]').forEach((b) => { b.onclick = () => dlg.close(); });
  const form = dlg.querySelector('#exp-form');
  const kindOf = () => form.querySelector('input[name=kind]:checked').value;
  const sync = () => {
    const k = kindOf();
    form.querySelectorAll('[data-kind]').forEach((d) => { d.hidden = d.dataset.kind !== k; });
    form.querySelectorAll('.kind-opt').forEach((l) => l.classList.toggle('sel', l.querySelector('input').checked));
    $('#exp-submit', form).textContent = k === 'forward' ? 'Freeze predictions' : 'Start experiment';
    const fe = $('#form-err', form); fe.hidden = true;
  };
  form.querySelectorAll('input[name=kind]').forEach((r) => { r.onchange = sync; });
  const fresh = $('#fw_run_fresh_scan', form), scanSel = $('#fw_scan_id', form);
  const syncFresh = () => { scanSel.disabled = fresh.checked; };
  fresh.onchange = syncFresh; syncFresh();
  $('#fw_horizon_days', form).oninput = (e) => { $('#fw-due-n', form).textContent = e.target.value || '—'; };
  sync();

  const collect = () => {
    const kind = kindOf(), pre = kind === 'backtest' ? 'bt' : 'fw';
    const R = kind === 'backtest' ? BR : FR;
    const errs = {}, p = {};
    const g = (n) => form.querySelector(`[name="${pre}_${n}"]`);
    const num = (n, label, o = {}) => {
      const raw = g(n).value.trim();
      if (raw === '') { if (o.optional) return null; errs[`${pre}_${n}`] = `${label} is required.`; return null; }
      const v = Number(raw);
      if (!Number.isFinite(v)) { errs[`${pre}_${n}`] = `${label} must be a number.`; return null; }
      if (o.int && !Number.isInteger(v)) { errs[`${pre}_${n}`] = `${label} must be a whole number.`; return null; }
      const r = rangeOf(R, n);
      const mn = r.min ?? o.min, mx = r.max ?? o.max;
      if (o.gt0 && v <= 0) { errs[`${pre}_${n}`] = `${label} must be greater than 0.`; return null; }
      if (mn !== undefined && mn !== null && v < mn) { errs[`${pre}_${n}`] = `${label} must be at least ${mn}.`; return null; }
      if (mx !== undefined && mx !== null && v > mx) { errs[`${pre}_${n}`] = `${label} must be at most ${mx}.`; return null; }
      return v;
    };
    if (kind === 'backtest') {
      const s = g('start_date').value, e = g('end_date').value;
      if (!s) errs.bt_start_date = 'Start date is required.';
      if (!e) errs.bt_end_date = 'End date is required.';
      if (s && e && s > e) errs.bt_end_date = 'End date must be on or after the start date.';
      p.start_date = s; p.end_date = e;
      p.horizon_days = num('horizon_days', 'Days to follow', { int: true, min: 1 });
      p.hit_threshold_pct = num('hit_threshold_pct', 'Hit threshold', { gt0: true });
      p.top_n = num('top_n', 'Picks per day', { int: true, min: 1 });
      p.min_score = num('min_score', 'Minimum score', { min: 0, max: 100 });
      p.entry = g('entry').value;
      p.include_filings = g('include_filings').checked;
    } else {
      p.run_fresh_scan = g('run_fresh_scan').checked;
      if (!p.run_fresh_scan && g('scan_id').value) p.scan_id = isNaN(Number(g('scan_id').value)) ? g('scan_id').value : Number(g('scan_id').value);
      p.verdicts = [...form.querySelectorAll('input[name=fw_verdicts]:checked')].map((c) => c.value);
      if (!p.verdicts.length) errs.fw_verdicts = 'Pick at least one verdict.';
      p.horizon_days = num('horizon_days', 'Days to wait', { int: true, min: 1 });
      p.hit_threshold_pct = num('hit_threshold_pct', 'Hit threshold', { gt0: true });
      const ms = num('min_score', 'Minimum score', { min: 0, max: 100, optional: true });
      if (ms !== null) p.min_score = ms;
      p.entry = g('entry').value;
    }
    return { kind, p, errs, pre };
  };

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const { kind, p, errs } = collect();
    form.querySelectorAll('.ferr').forEach((x) => { x.textContent = ''; });
    form.querySelectorAll('[aria-invalid]').forEach((x) => x.removeAttribute('aria-invalid'));
    const fe = $('#form-err', form); fe.hidden = true;
    const keys = Object.keys(errs);
    if (keys.length) {
      keys.forEach((k) => { const el = $('#err-' + k, form); if (el) el.textContent = errs[k]; const inp = form.querySelector(`[name="${k}"]`); if (inp) inp.setAttribute('aria-invalid', 'true'); });
      const first = form.querySelector(`[name="${keys[0]}"]`) || form.querySelector(`input[name="${keys[0]}"]`);
      if (first && first.focus) first.focus();
      fe.textContent = 'Please fix the highlighted fields.'; fe.hidden = false;
      return;
    }
    const body = { kind, params: p };
    const nm = $('#exp-name', form).value.trim();
    if (nm) body.name = nm;
    const btn = $('#exp-submit', form);
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = 'Starting…';
    try {
      const r = await api('/api/experiments', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      dlg.close();
      if (r && r.id !== undefined && r.id !== null) location.hash = expHref(r.id); else render();
    } catch (e) {
      fe.textContent = e.status === 409
        ? `Another experiment or scan is already running. Wait for it to finish, then try again.${e.detail ? ' (' + detailText(e) + ')' : ''}`
        : e.status === 400 ? `The server rejected these settings: ${detailText(e)}` : `Could not start the experiment: ${e.message}`;
      fe.hidden = false; fe.scrollIntoView({ block: 'nearest' });
      btn.disabled = false; btn.textContent = label;
    }
  };
  dlg.showModal();
}

/* ---- detail ---- */
const CMP_ROWS = [
  ['hit_rate', 'Hit rate', 'Share of picks that reached the hit threshold', false],
  ['avg_return', 'Average return', 'Mean return at the end of the follow period', true],
  ['median_return', 'Median return', 'The typical pick, ignoring huge winners', true],
  ['win_rate', 'Win rate', 'Share of picks that ended above entry', false],
  ['avg_max_gain', 'Avg max gain', 'Best point reached, on average', true],
  ['avg_max_drawdown', 'Avg max drawdown', 'Worst dip below entry, on average', true],
];
const cellVal = (v, signed) => (signed ? pctCell(v) : fmt.pctPlain(v));
const diffCell = (a, b) => {
  if (!isNum(a) || !isNum(b)) return '<span class="muted">—</span>';
  const x = (a - b) * 100;
  return `<span class="${signCls(Math.abs(x) < 0.05 ? 0 : x)}">${x > 0.05 ? '+' : ''}${x.toFixed(1)} pp</span>`;
};

function groupTable(rows, headLabel, labelFn, baseline) {
  const cols = ['evaluated', 'hit_rate', 'avg_return', 'median_return', 'win_rate', 'avg_max_gain', 'avg_max_drawdown'];
  const tr = (g, label, cls) => `<tr class="${cls || ''}"><td>${label}</td><td class="num">${fmt.int(g.count)}</td><td class="num">${fmt.int(g.evaluated)}</td>
    <td class="num">${fmt.pctPlain(g.hit_rate)}</td><td class="num">${pctCell(g.avg_return)}</td><td class="num">${pctCell(g.median_return)}</td>
    <td class="num">${fmt.pctPlain(g.win_rate)}</td><td class="num">${pctCell(g.avg_max_gain)}</td><td class="num">${pctCell(g.avg_max_drawdown)}</td></tr>`;
  void cols;
  return `<div class="table-wrap"><table><thead><tr><th>${esc(headLabel)}</th><th class="num">Picks</th><th class="num">Scored</th><th class="num">Hit rate</th><th class="num">Avg return</th><th class="num">Median</th><th class="num">Win rate</th><th class="num">Avg max gain</th><th class="num">Avg max DD</th></tr></thead>
    <tbody>${rows.map((g) => tr(g, labelFn(g))).join('')}${baseline ? tr(baseline, '<i>All penny stocks (baseline)</i>', 'base-row') : ''}</tbody></table></div>`;
}

function drawEquityChart(curve) {
  const el = $('#eq-chart');
  if (!el) return;
  const series = (k) => {
    const m = new Map();
    for (const r of curve) if (r && r.date && isNum(r[k])) m.set(r.date, r[k]);
    return [...m.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1)).map(([time, value]) => ({ time, value }));
  };
  const sp = series('cumulative_picks'), sb = series('cumulative_baseline');
  if (sp.length < 2 && sb.length < 2) { $('#eq-panel').hidden = true; return; }
  if (!window.LightweightCharts) { el.innerHTML = '<p class="muted">Chart library failed to load (needs network access to unpkg.com).</p>'; return; }
  const LW = window.LightweightCharts;
  const cp = cssVar('--s-picks'), cb = cssVar('--s-base');
  const pf = { type: 'custom', minMove: 0.0001, formatter: (v) => (v > 0 ? '+' : '') + (v * 100).toFixed(1) + '%' };
  chartInst = LW.createChart(el, {
    width: el.clientWidth, height: 300,
    layout: { background: { color: 'transparent' }, textColor: cssVar('--chart-text') },
    grid: { vertLines: { color: cssVar('--chart-grid') }, horzLines: { color: cssVar('--chart-grid') } },
    rightPriceScale: { borderColor: cssVar('--border') },
    timeScale: { borderColor: cssVar('--border') },
    crosshair: { mode: 0 },
  });
  const lp = chartInst.addLineSeries({ color: cp, lineWidth: 3, priceFormat: pf, title: '' });
  const lb = chartInst.addLineSeries({ color: cb, lineWidth: 2, lineStyle: 2, priceFormat: pf, title: '' });
  lp.setData(sp); lb.setData(sb);
  chartInst.timeScale().fitContent();
  const vp = $('#lg-picks'), vb = $('#lg-base');
  chartInst.subscribeCrosshairMove((param) => {
    const a = param && param.seriesData && param.seriesData.get(lp), b = param && param.seriesData && param.seriesData.get(lb);
    vp.textContent = a && isNum(a.value) ? fmt.pct(a.value) : '';
    vb.textContent = b && isNum(b.value) ? fmt.pct(b.value) : '';
  });
  chartRO = new ResizeObserver(() => chartInst && chartInst.applyOptions({ width: el.clientWidth }));
  chartRO.observe(el);
}

const resultOf = (p) => (p.hit === true ? 'Hit' : p.hit === false ? 'Miss' : 'Pending');
const PICK_COLS = [
  { key: 'signal_date', label: 'Signal date', text: true, get: (p) => p.signal_date },
  { key: 'ticker', label: 'Ticker', text: true, get: (p) => p.ticker },
  { key: 'verdict', label: 'Verdict', get: (p) => ({ STRONG: 4, WATCH: 3, WEAK: 2, AVOID: 1 }[p.verdict] ?? 0) },
  { key: 'score', label: 'Score', num: true, get: (p) => p.score },
  { key: 'entry_date', label: 'Entry', text: true, get: (p) => p.entry_date },
  { key: 'entry_price', label: 'Entry $', num: true, get: (p) => p.entry_price },
  { key: 'exit_date', label: 'Exit', text: true, get: (p) => p.exit_date },
  { key: 'exit_price', label: 'Exit $', num: true, get: (p) => p.exit_price },
  { key: 'return', label: 'Return', num: true, get: (p) => p.return },
  { key: 'max_gain', label: 'Max gain', num: true, get: (p) => p.max_gain },
  { key: 'max_drawdown', label: 'Max DD', num: true, get: (p) => p.max_drawdown },
  { key: 'result', label: 'Result', get: (p) => ({ Hit: 2, Miss: 1, Pending: 0 }[resultOf(p)]) },
  { key: 'flags', label: 'Risk flags', get: (p) => (Array.isArray(p.risk_flags) ? p.risk_flags.length : 0) },
];
const PAGE_SIZE = 50;

function pickRow(p) {
  const r = resultOf(p), partial = p.status === 'partial';
  const flags = Array.isArray(p.risk_flags) ? p.risk_flags : [];
  return `<tr class="${partial ? 'partial' : ''}"><td>${fmt.date(p.signal_date)}</td>
    <td><a href="#/t/${encodeURIComponent(p.ticker || '')}"><b>${esc(p.ticker) || '—'}</b></a><div class="tk-name" title="${esc(p.name)}">${esc(p.name) || ''}</div></td>
    <td>${verdictBadge(p.verdict)}</td><td class="num">${fmt.int(p.score)}</td>
    <td>${fmt.date(p.entry_date)}</td><td class="num">${fmt.price(p.entry_price)}</td>
    <td>${fmt.date(p.exit_date)}</td><td class="num">${fmt.price(p.exit_price)}</td>
    <td class="num">${pctCell(p.return)}</td><td class="num">${pctCell(p.max_gain)}</td><td class="num">${pctCell(p.max_drawdown)}</td>
    <td><span class="res res-${r}">${r}</span>${partial ? ' <span class="chip low" title="Still within its follow period; the numbers may change">In progress</span>' : ''}</td>
    <td><div class="chips">${flags.slice(0, 2).map((f) => `<span class="chip low" title="${esc(f)}">${esc(String(f).length > 22 ? String(f).slice(0, 21) + '…' : f)}</span>`).join('')}${flags.length > 2 ? `<span class="chip low" title="${esc(flags.slice(2).join('; '))}">+${flags.length - 2}</span>` : ''}${flags.length ? '' : '<span class="muted">—</span>'}</div></td></tr>`;
}

function picksSection(d) {
  const picks = Array.isArray(d.picks) ? d.picks : [];
  if (!state.pk || state.pk.id !== d.id) state.pk = { id: d.id, sort: { key: 'signal_date', dir: -1 }, verdict: '', result: '', q: '', page: 0 };
  const pk = state.pk;
  const verdicts = [...new Set(picks.map((p) => p.verdict).filter(Boolean))];
  const total = isNum(d.picks_total) ? d.picks_total : picks.length;
  $('#picks-body').innerHTML = `
    <div class="toolbar">
      <label>Verdict <select id="pf-verdict"><option value="">All</option>${verdicts.map((v) => `<option ${pk.verdict === v ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></label>
      <label>Result <select id="pf-result">${['', 'Hit', 'Miss', 'Pending'].map((v) => `<option value="${v}" ${pk.result === v ? 'selected' : ''}>${v || 'All'}</option>`).join('')}</select></label>
      <label>Ticker <input id="pf-q" type="search" placeholder="Search" value="${esc(pk.q)}" style="width:110px"></label>
      <span class="count" id="pf-count"></span>
    </div>
    ${total > picks.length ? `<p class="note">Showing the first ${fmt.int(picks.length)} of ${fmt.int(total)} picks.</p>` : ''}
    <div class="table-wrap"><table id="ptable"><thead><tr>${headerCells(PICK_COLS, pk.sort)}</tr></thead><tbody id="prows"></tbody></table></div>
    <div class="pager"><button id="pg-prev">‹ Prev</button><span id="pg-info" class="muted"></span><button id="pg-next">Next ›</button></div>`;
  const draw = () => {
    const q = pk.q.trim().toLowerCase();
    let rows = picks.filter((p) => (!pk.verdict || p.verdict === pk.verdict) && (!pk.result || resultOf(p) === pk.result) && (!q || String(p.ticker || '').toLowerCase().includes(q) || String(p.name || '').toLowerCase().includes(q)));
    rows = sortRows(rows, PICK_COLS, pk.sort);
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    pk.page = Math.min(pk.page, pages - 1);
    const from = pk.page * PAGE_SIZE;
    $('#pf-count').textContent = `${fmt.int(rows.length)} of ${fmt.int(picks.length)} picks`;
    $('#prows').innerHTML = rows.length ? rows.slice(from, from + PAGE_SIZE).map(pickRow).join('') : `<tr><td colspan="${PICK_COLS.length}" class="empty">${picks.length ? 'No picks match these filters.' : 'No picks recorded.'}</td></tr>`;
    $('#pg-info').textContent = rows.length ? `${from + 1}–${Math.min(rows.length, from + PAGE_SIZE)} of ${fmt.int(rows.length)} · page ${pk.page + 1} of ${pages}` : '';
    $('#pg-prev').disabled = pk.page <= 0; $('#pg-next').disabled = pk.page >= pages - 1;
  };
  draw();
  $('#pf-verdict').onchange = (e) => { pk.verdict = e.target.value; pk.page = 0; draw(); };
  $('#pf-result').onchange = (e) => { pk.result = e.target.value; pk.page = 0; draw(); };
  $('#pf-q').oninput = (e) => { pk.q = e.target.value; pk.page = 0; draw(); };
  $('#pg-prev').onclick = () => { pk.page--; draw(); };
  $('#pg-next').onclick = () => { pk.page++; draw(); };
  $('#ptable thead').onclick = (e) => {
    const th = e.target.closest('th[data-sort]'); if (!th) return;
    const k = th.dataset.sort;
    pk.sort = { key: k, dir: pk.sort.key === k ? -pk.sort.dir : (PICK_COLS.find((c) => c.key === k).text ? 1 : -1) };
    pk.page = 0;
    $('#ptable thead tr').innerHTML = headerCells(PICK_COLS, pk.sort); draw();
  };
}

function drawExperiment(d, token) {
  stopExpTimer();
  destroyChart();
  const s = d.summary || {};
  const h = d.headline || null;
  const fallbackO = h ? { hit_rate: h.hit_rate, avg_return: h.avg_return, evaluated: h.evaluated, count: h.picks } : {};
  const fallbackB = h ? { hit_rate: h.baseline_hit_rate, avg_return: h.baseline_avg_return } : {};
  const o = s.overall || fallbackO, b = s.baseline || fallbackB;
  const hl = { evaluated: isNum(o.evaluated) ? o.evaluated : h && h.evaluated, hit_rate: o.hit_rate, baseline_hit_rate: b.hit_rate, avg_return: o.avg_return, baseline_avg_return: b.avg_return };
  const v = expVerdict(hl, d.status);
  const done = d.status === 'completed' || d.status === 'waiting';
  const byV = Array.isArray(s.by_verdict) ? [...s.by_verdict] : [];
  const order = ['STRONG', 'WATCH', 'WEAK', 'AVOID'];
  byV.sort((x, y) => (order.indexOf(x.verdict) + 1 || 9) - (order.indexOf(y.verdict) + 1 || 9));
  const byM = Array.isArray(s.by_month) ? [...s.by_month].sort((x, y) => String(x.month).localeCompare(String(y.month))) : [];
  const curve = Array.isArray(s.equity_curve) ? s.equity_curve : [];
  const notes = Array.isArray(d.notes) ? d.notes.filter(Boolean) : [];
  const picks = Array.isArray(d.picks) ? d.picks : [];
  const hasSummary = !!(s.overall || h);

  let status = '';
  if (d.status === 'running') status = `<div class="panel"><h2>In progress</h2>${progressBar(d.progress)}<p class="muted" style="margin:8px 0 0">${esc(d.message) || 'Working…'}${isNum(d.progress) ? ` (${Math.round(d.progress * 100)}%)` : ''} This page updates by itself.</p></div>`;
  else if (d.status === 'failed') status = `<div class="error"><b>This experiment failed.</b><div>${esc(d.error || d.message) || 'No error message was recorded.'}</div></div>`;
  else if (d.status === 'waiting') status = `<div class="panel callout"><b>Waiting for results.</b> The predictions are frozen. They will be scored after <b>${fmt.date(d.due_date)}</b>; ${STATIC ? 'the next daily scan on GitHub settles them.' : 'the app must be running then.'} Rows marked "In progress" show the price so far. ${d.message ? esc(d.message) : ''}</div>`;

  const noNumbers = d.status === 'waiting' && !(isNum(o.evaluated) && o.evaluated > 0);
  const cmp = hasSummary && !noNumbers && d.status !== 'running' && d.status !== 'failed' ? `
    <div class="panel cmp-panel"><h2>Picks vs baseline</h2>
      <div class="cmp-top">
        <div class="cmp-verdict">${v ? `<div class="vd vd-${v.cls} lg"><span aria-hidden="true">${v.icon}</span> ${esc(v.label)}</div><div class="muted">${esc(v.why)}</div>` : `<div class="vd vd-few lg">${d.status === 'waiting' ? 'Not scored yet' : 'No verdict'}</div><div class="muted">${d.status === 'waiting' ? 'Check back after the due date.' : ''}</div>`}</div>
        <div class="cmp-big"><span class="muted">Hit rate</span><div><b>${fmt.pctPlain(o.hit_rate, 1)}</b> <small>picks</small></div><div class="base"><b>${fmt.pctPlain(b.hit_rate, 1)}</b> <small>baseline</small></div></div>
        <div class="cmp-big"><span class="muted">Average return</span><div><b>${pctCell(o.avg_return)}</b> <small>picks</small></div><div class="base"><b>${pctCell(b.avg_return)}</b> <small>baseline</small></div></div>
      </div>
      <p class="note" style="margin-top:12px">Baseline = all penny stocks on the same days. ${fmt.int(o.evaluated)} of ${fmt.int(o.count)} picks scored. "pp" = percentage points.</p>
      <div class="table-wrap"><table class="cmp"><thead><tr><th>Measure</th><th class="num">Picks</th><th class="num">Baseline</th><th class="num">Difference</th></tr></thead><tbody>
        ${CMP_ROWS.map(([k, label, help, signed]) => `<tr><td><b>${label}</b><div class="li-meta">${help}</div></td><td class="num">${cellVal(o[k], signed)}</td><td class="num">${cellVal(b[k], signed)}</td><td class="num">${diffCell(o[k], b[k])}</td></tr>`).join('')}
      </tbody></table></div></div>` : '';

  app.innerHTML = `
    <a class="back" href="#/experiments">← All experiments</a>
    <div class="headline">
      <div><h1>${esc(expName(d))}</h1><div class="sub">${esc(kindLabel(d.kind))} · Created ${fmt.dt(d.created_at)}${d.finished_at ? ` · Finished ${fmt.dt(d.finished_at)}` : ''}${d.status === 'waiting' ? ` · Due ${fmt.date(d.due_date)}` : ''}</div></div>
      ${statusBadge(d.status)}${verdictChip(v)}
      <div class="exp-actions">
        ${STATIC ? '' : `${d.status === 'waiting' && d.kind === 'forward' ? '<button class="primary" id="x-check">Check results now</button>' : ''}
        <button class="danger-o" id="x-del" ${d.status === 'running' ? 'disabled title="Cannot delete while running"' : ''}>Delete</button>`}
        ${STATIC ? ghLink('Start on GitHub', d.status === 'waiting' && d.kind === 'forward' ? 'scan.yml' : 'experiment.yml', 'primary') : ''}
      </div>
    </div>
    <div id="x-err" class="error" role="alert" hidden></div>
    <p class="exp-params">${describeParams(d.kind, d.params)}</p>
    ${status}
    ${cmp}
    ${done && byV.length ? `<div class="panel"><h2>By verdict</h2><p class="note">Does STRONG do better than WATCH?</p>${groupTable(byV, 'Verdict', (g) => verdictBadge(g.verdict), s.baseline)}</div>` : ''}
    ${done && curve.length ? `<div class="panel" id="eq-panel"><h2>Cumulative return: picks vs baseline</h2>
      <div class="legend" id="eq-legend"><span><i class="sw sw-picks"></i>Picks <b id="lg-picks"></b></span><span><i class="sw sw-base"></i>Baseline (all penny stocks) <b id="lg-base"></b></span></div>
      <div id="eq-chart"></div></div>` : ''}
    ${done && byM.length ? `<div class="panel"><h2>By month</h2>${groupTable(byM, 'Month', (g) => esc(g.month) || '—', null)}</div>` : ''}
    ${notes.length ? `<div class="notes"><h3>Things to keep in mind</h3><ul>${notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul></div>` : ''}
    ${picks.length || isNum(d.picks_total) ? `<div class="panel"><h2>Picks (${fmt.int(isNum(d.picks_total) ? d.picks_total : picks.length)})</h2><div id="picks-body"></div></div>` : ''}`;

  if (curve.length) drawEquityChart(curve);
  if ($('#picks-body')) picksSection(d);
  const err = (m) => { const el = $('#x-err'); el.textContent = m; el.hidden = false; el.scrollIntoView({ block: 'nearest' }); };
  const chk = $('#x-check');
  if (chk) chk.onclick = async () => {
    chk.disabled = true; chk.textContent = 'Checking…';
    try { const nd = await api(`/api/experiments/${encodeURIComponent(d.id)}/evaluate`, { method: 'POST' }); if (token === renderToken) drawExperiment(nd, token); }
    catch (e) { if (token !== renderToken) return; chk.disabled = false; chk.textContent = 'Check results now'; err(`Could not check results: ${detailText(e)}`); }
  };
  if (!STATIC) $('#x-del').onclick = async () => {
    if (!(await confirmDialog(`Delete "${expName(d)}"? This removes the experiment and all of its picks.`, 'Delete'))) return;
    try { await api(`/api/experiments/${encodeURIComponent(d.id)}`, { method: 'DELETE' }); location.hash = '#/experiments'; }
    catch (e) { err(e.status === 409 ? 'This experiment is still running and cannot be deleted yet.' : `Could not delete: ${detailText(e)}`); }
  };
  if (d.status === 'running') {
    state.expTimer = setTimeout(async () => {
      if (token !== renderToken) return;
      try { const nd = await api(`/api/experiments/${encodeURIComponent(d.id)}`); if (token === renderToken) drawExperiment(nd, token); }
      catch { if (token === renderToken) state.expTimer = setTimeout(() => drawExperiment(d, token), 5000); }
    }, 2000);
  }
}

async function viewExperiment(id, token) {
  app.innerHTML = '<p class="muted">Loading…</p>';
  let d;
  try { d = await api(`/api/experiments/${encodeURIComponent(id)}`); }
  catch (e) {
    if (token !== renderToken) return;
    app.innerHTML = `<a class="back" href="#/experiments">← All experiments</a><div class="error">${e.status === 404 ? 'That experiment does not exist (it may have been deleted).' : 'Failed to load: ' + esc(e.message)}</div>`;
    return;
  }
  if (token !== renderToken) return;
  drawExperiment(d, token);
}

function renderStaticNote() {
  const el = $('#run-banner');
  el.hidden = false;
  el.classList.add('static-note');
  const when = STATIC.generated_at ? fmt.dt(STATIC.generated_at) : 'an unknown time';
  const actions = ghUrl('');
  el.innerHTML = `<b>Read-only snapshot</b> generated ${esc(when)}. Scans and experiments run on GitHub Actions and this page is rebuilt afterwards${actions ? ` · ${ghLink('Open Actions on GitHub', '')} · ${ghLink('Run scan', 'scan.yml')} · ${ghLink('New experiment', 'experiment.yml')}` : ''}.`;
}

/* ---------- boot ---------- */
(async function boot() {
  if (STATIC) renderStaticNote();
  await loadMeta(true);
  render();
  if (STATIC) return;
  try { const st = await api('/api/scans/status'); if (st.running) { setBanner(st); pollScan(); } } catch {}
})();
