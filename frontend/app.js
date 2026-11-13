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

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = '';
    try { const j = await res.json(); detail = j.detail || j.message || ''; } catch {}
    const e = new Error(`${res.status} ${res.statusText}${detail ? ': ' + (typeof detail === 'string' ? detail : JSON.stringify(detail)) : ''}`);
    e.status = res.status;
    throw e;
  }
  return res.json();
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
  running: false, pollTimer: null,
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
    <button class="primary" id="run-btn" ${state.running ? 'disabled' : ''}>${state.running ? 'Scan running…' : 'Run scan'}</button>`;
  const sel = $('#scan-select');
  if (sel) sel.onchange = () => { state.scanId = sel.value; if (location.hash.startsWith('#/t/')) location.hash = '#/'; else render(); };
  $('#run-btn').onclick = startScan;
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
  if (selectNewest || !state.scans.some((x) => String(x.id) === String(state.scanId))) state.scanId = state.scans[0]?.id ?? null;
  renderTopbar();
}

/* ---------- router ---------- */
function render() {
  const hash = location.hash || '#/';
  document.querySelectorAll('[data-nav]').forEach((a) => a.classList.toggle('active', (hash.startsWith('#/performance') ? 'performance' : 'home') === a.dataset.nav));
  const token = ++renderToken;
  destroyChart();
  let p;
  if (hash.startsWith('#/t/')) p = viewDetail(decodeURIComponent(hash.slice(4)).toUpperCase(), token);
  else if (hash.startsWith('#/performance')) p = viewPerformance(token);
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
      <p>Press <b>Run scan</b> in the top right to run your first scan. It can take a few minutes; progress shows at the top of the page.</p></div>`;
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

/* ---------- boot ---------- */
(async function boot() {
  await loadMeta(true);
  render();
  try { const st = await api('/api/scans/status'); if (st.running) { setBanner(st); pollScan(); } } catch {}
})();
