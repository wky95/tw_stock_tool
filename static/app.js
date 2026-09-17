const $ = (selector) => document.querySelector(selector);
const form = $('#backtest-form');
const paramsRoot = $('#strategy-params');
let csvText = '';
let currentResult = null;
let factorDiscovery = null;
let factorSynthesis = null;

const strategies = {
  sma_cross: [
    { id: 'fast', label: '短均線', value: 20, min: 1 },
    { id: 'slow', label: '長均線', value: 60, min: 2 },
  ],
  breakout: [
    { id: 'entry', label: '突破週期', value: 20, min: 2 },
    { id: 'exit', label: '出場週期', value: 10, min: 2 },
  ],
  rsi: [
    { id: 'length', label: 'RSI 週期', value: 14, min: 2 },
    { id: 'lower', label: '買進線', value: 30, min: 0 },
    { id: 'upper', label: '賣出線', value: 70, min: 1 },
  ],
};

function localISO(date) {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date - offset).toISOString().slice(0, 10);
}
const today = new Date();
const threeYearsAgo = new Date(today); threeYearsAgo.setFullYear(today.getFullYear() - 3);
$('#end').value = localISO(today);
$('#start').value = localISO(threeYearsAgo);

function renderParams() {
  paramsRoot.innerHTML = strategies[$('#strategy').value].map(p =>
    `<label class="field">${p.label}<input data-param="${p.id}" type="number" min="${p.min}" value="${p.value}"></label>`
  ).join('');
}
renderParams();
$('#strategy').addEventListener('change', renderParams);

document.querySelectorAll('[name="source"]').forEach(input => input.addEventListener('change', () => {
  const isCsv = input.checked && input.value === 'csv';
  if (!input.checked) return;
  $('#twse-fields').hidden = isCsv;
  $('#csv-fields').hidden = !isCsv;
}));

$('#csv-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  csvText = await file.text();
  $('#file-label').textContent = file.name;
});

function number(id) { return Number($(id).value); }
function fmt(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toLocaleString('zh-TW', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  const source = document.querySelector('[name="source"]:checked').value;
  const error = $('#error');
  if (source === 'csv' && !csvText) {
    error.textContent = '請先選擇 CSV 檔案。'; error.hidden = false; return;
  }
  error.hidden = true;
  const button = $('#run-button');
  button.disabled = true; button.querySelector('span').textContent = source === 'csv' ? '計算中…' : '下載並計算中…';
  const params = {};
  document.querySelectorAll('[data-param]').forEach(input => params[input.dataset.param] = Number(input.value));
  const payload = {
    source, csv_text: source === 'csv' ? csvText : undefined,
    symbol: $('#symbol').value.trim(), market: $('#market').value, start: $('#start').value, end: $('#end').value,
    strategy: $('#strategy').value, params,
    config: {
      initial_cash: number('#initial-cash'), commission_discount: number('#discount'),
      tax_rate: number('#tax') / 100, slippage_bps: number('#slippage'),
    },
  };
  try {
    const response = await fetch('/api/v1/backtests', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error?.message || data.error || '回測失敗');
    currentResult = data.result;
    renderResult(currentResult);
  } catch (err) {
    error.textContent = err.message || '無法連線至回測服務。'; error.hidden = false;
  } finally {
    button.disabled = false; button.querySelector('span').textContent = '開始回測';
  }
});

function renderResult(result) {
  $('#empty-state').hidden = true; $('#results').hidden = false;
  const m = result.meta; const strategyName = $('#strategy').selectedOptions[0].textContent;
  $('#result-meta').textContent = `${m.source} · ${m.start} → ${m.end} · ${m.bar_count} 根日 K`;
  $('#result-title').textContent = `${m.symbol}｜${strategyName}`;
  const s = result.summary;
  const cards = [
    ['總報酬', s.total_return_pct, '%', true], ['年化報酬', s.annual_return_pct, '%', true],
    ['最大回撤', s.max_drawdown_pct, '%', true], ['Sharpe', s.sharpe, '', false],
    ['勝率', s.win_rate_pct, '%', false], ['完成交易', s.trade_count, ' 筆', false],
  ];
  $('#metrics').innerHTML = cards.map(([label, value, suffix, color]) => {
    const tone = color && value !== null ? (value >= 0 ? 'positive' : 'negative') : '';
    const rendered = value === null ? '—' : (label === '完成交易' ? fmt(value, 0) : fmt(value));
    return `<div class="metric"><span>${label}</span><strong class="${tone}">${rendered}${value === null ? '' : suffix}</strong>${label === '總報酬' ? `<small>買入持有 ${fmt(s.buy_hold_pct)}%</small>` : ''}</div>`;
  }).join('');
  $('#trade-count').textContent = `${result.trades.length} 筆`;
  $('#trade-body').innerHTML = result.trades.length ? result.trades.map(t => `<tr>
    <td>${t.entry_date}</td><td>${t.exit_date}</td><td>${fmt(t.shares, 0)}</td><td>${fmt(t.entry_price)}</td><td>${fmt(t.exit_price)}</td>
    <td class="${t.pnl >= 0 ? 'positive' : 'negative'}">${fmt(t.pnl, 0)}</td><td class="${t.return_pct >= 0 ? 'positive' : 'negative'}">${fmt(t.return_pct)}%</td>
    <td>${fmt(t.fees_tax, 0)}</td><td>${t.exit_reason}</td></tr>`).join('') : '<tr><td colspan="9" class="no-trades">這段期間沒有完成交易</td></tr>';
  requestAnimationFrame(() => { drawEquity(result); drawPrice(result); });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function canvasSetup(canvas) {
  const rect = canvas.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(300, rect.width * dpr); canvas.height = Number(canvas.getAttribute('height')) * dpr;
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  return { ctx, w: canvas.width / dpr, h: canvas.height / dpr };
}

function bounds(values, padding = .08) {
  let min = Math.min(...values), max = Math.max(...values); const span = max - min || Math.abs(max) || 1;
  return [min - span * padding, max + span * padding];
}

function axes(ctx, w, h, min, max, dates) {
  const box = { l: 62, r: w - 15, t: 12, b: h - 28 };
  ctx.font = '10px monospace'; ctx.textAlign = 'right'; ctx.fillStyle = '#7f8984'; ctx.strokeStyle = '#e2e1da'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = box.t + (box.b - box.t) * i / 4; const value = max - (max - min) * i / 4;
    ctx.beginPath(); ctx.moveTo(box.l, y); ctx.lineTo(box.r, y); ctx.stroke(); ctx.fillText(Math.round(value).toLocaleString(), box.l - 8, y + 3);
  }
  ctx.textAlign = 'center';
  [0, .25, .5, .75, 1].forEach(f => { const index = Math.min(dates.length - 1, Math.floor((dates.length - 1) * f)); ctx.fillText(dates[index].slice(0, 7), box.l + (box.r - box.l) * f, h - 8); });
  return box;
}

function line(ctx, points, color, width = 2, dash = []) {
  ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
  points.forEach(([x,y], i) => i ? ctx.lineTo(x,y) : ctx.moveTo(x,y)); ctx.stroke(); ctx.setLineDash([]);
}

function drawEquity(result) {
  const {ctx,w,h} = canvasSetup($('#equity-chart')); const data = result.equity_curve;
  const all = data.flatMap(d => [d.equity, d.benchmark]); const [min,max] = bounds(all);
  const box = axes(ctx,w,h,min,max,data.map(d=>d.date));
  const x = i => box.l + (box.r-box.l) * i / Math.max(1,data.length-1); const y = v => box.b - (v-min)/(max-min)*(box.b-box.t);
  const gradient = ctx.createLinearGradient(0,box.t,0,box.b); gradient.addColorStop(0,'rgba(19,92,69,.16)'); gradient.addColorStop(1,'rgba(19,92,69,0)');
  ctx.beginPath(); data.forEach((d,i)=>i?ctx.lineTo(x(i),y(d.equity)):ctx.moveTo(x(i),y(d.equity))); ctx.lineTo(box.r,box.b);ctx.lineTo(box.l,box.b);ctx.closePath();ctx.fillStyle=gradient;ctx.fill();
  line(ctx,data.map((d,i)=>[x(i),y(d.benchmark)]),'#9ca49f',1.5,[5,5]); line(ctx,data.map((d,i)=>[x(i),y(d.equity)]),'#135c45',2.3);
}

function drawPrice(result) {
  const {ctx,w,h} = canvasSetup($('#price-chart')); const allBars = result.bars;
  const step = Math.max(1, Math.ceil(allBars.length / Math.max(60, Math.floor((w-80)/5))));
  const indices = []; for(let i=0;i<allBars.length;i+=step) indices.push(i); if(indices.at(-1)!==allBars.length-1) indices.push(allBars.length-1);
  const bars = indices.map(i=>allBars[i]); const values = bars.flatMap(d=>[d.low,d.high]); const [min,max] = bounds(values,.05);
  const box = axes(ctx,w,h,min,max,bars.map(d=>d.date)); const slot=(box.r-box.l)/bars.length; const y=v=>box.b-(v-min)/(max-min)*(box.b-box.t);
  bars.forEach((b,i)=>{ const x=box.l+slot*(i+.5); const up=b.close>=b.open; ctx.strokeStyle=up?'#c94d3f':'#177157';ctx.fillStyle=up?'#c94d3f':'#177157';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,y(b.high));ctx.lineTo(x,y(b.low));ctx.stroke();const top=y(Math.max(b.open,b.close)), bottom=y(Math.min(b.open,b.close));ctx.fillRect(x-Math.max(1,slot*.28),top,Math.max(2,slot*.56),Math.max(1,bottom-top)); });
  const dateToIndex = new Map(bars.map((b,i)=>[b.date,i]));
  result.executions.forEach(e=>{ let nearest=bars.findIndex(b=>b.date>=e.date);if(nearest<0)nearest=bars.length-1;const x=box.l+slot*(nearest+.5), py=y(e.price);ctx.fillStyle=e.side==='BUY'?'#135c45':'#c94d3f';ctx.beginPath();if(e.side==='BUY'){ctx.moveTo(x,py-9);ctx.lineTo(x-5,py-1);ctx.lineTo(x+5,py-1);}else{ctx.moveTo(x,py+9);ctx.lineTo(x-5,py+1);ctx.lineTo(x+5,py+1);}ctx.closePath();ctx.fill();});
}

$('#export-button').addEventListener('click', () => {
  if (!currentResult) return;
  const columns = ['entry_date','exit_date','shares','entry_price','exit_price','pnl','return_pct','fees_tax','exit_reason'];
  const escape = v => `"${String(v ?? '').replaceAll('"','""')}"`;
  const csv = '\ufeff' + [columns.join(','), ...currentResult.trades.map(t=>columns.map(c=>escape(t[c])).join(','))].join('\n');
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download=`backtest_${currentResult.meta.symbol}.csv`;a.click();URL.revokeObjectURL(a.href);
});

let resizeTimer;
window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(currentResult){drawEquity(currentResult);drawPrice(currentResult);}if(factorSynthesis){drawFactorIc(factorSynthesis);}},120);});

// --- Intraday monitor -----------------------------------------------------
let monitorTimer = null;
let monitorBusy = false;
let activeAlerts = new Set();
const alertStorageKey = 'tw-stock-tool.alerts.v1';

document.querySelectorAll('.nav-button').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.nav-button').forEach(item => item.classList.toggle('active', item === button));
  document.querySelectorAll('.app-view').forEach(view => view.hidden = view.id !== button.dataset.view);
  if (button.dataset.view === 'monitor-view') {
    loadCapabilities();
    if (!monitorTimer) refreshQuotes();
    scheduleMonitor();
  } else {
    clearInterval(monitorTimer); monitorTimer = null;
    if (button.dataset.view === 'factor-view') loadFactorCatalog();
  }
}));

function loadRules() {
  try { return JSON.parse(localStorage.getItem(alertStorageKey) || '[]'); }
  catch { return []; }
}
function saveRules(rules) { localStorage.setItem(alertStorageKey, JSON.stringify(rules)); }
function ruleId() { return `rule-${Date.now()}-${Math.random().toString(16).slice(2)}`; }

function renderRules() {
  const rules = loadRules(); const root = $('#alert-rules');
  if (!rules.length) { root.innerHTML = '<div class="empty-alert">尚未設定警示條件</div>'; return; }
  root.innerHTML = rules.map(rule => `<div class="alert-row" data-rule="${rule.id}">
    <input class="alert-enabled" type="checkbox" aria-label="啟用" ${rule.enabled ? 'checked' : ''}>
    <input class="alert-symbol" value="${rule.instrument}" placeholder="2330:tse" aria-label="股票">
    <select class="alert-condition" aria-label="條件">
      <option value="above" ${rule.condition === 'above' ? 'selected' : ''}>價格 ≥</option>
      <option value="below" ${rule.condition === 'below' ? 'selected' : ''}>價格 ≤</option>
      <option value="change_pct_above" ${rule.condition === 'change_pct_above' ? 'selected' : ''}>漲跌幅 ≥ %</option>
      <option value="change_pct_below" ${rule.condition === 'change_pct_below' ? 'selected' : ''}>漲跌幅 ≤ %</option>
    </select>
    <input class="alert-threshold" type="number" step="0.01" value="${rule.threshold}" aria-label="門檻">
    <button class="remove-alert" title="刪除">×</button></div>`).join('');
}

function collectRules() {
  return [...document.querySelectorAll('.alert-row')].map(row => ({
    id: row.dataset.rule, instrument: row.querySelector('.alert-symbol').value.trim(),
    condition: row.querySelector('.alert-condition').value,
    threshold: Number(row.querySelector('.alert-threshold').value),
    enabled: row.querySelector('.alert-enabled').checked,
  }));
}

$('#alert-rules').addEventListener('change', () => saveRules(collectRules()));
$('#alert-rules').addEventListener('click', event => {
  if (!event.target.matches('.remove-alert')) return;
  event.target.closest('.alert-row').remove(); saveRules(collectRules()); renderRules();
});
$('#add-alert').addEventListener('click', () => {
  const first = $('#watchlist').value.split(',')[0].trim() || '2330:tse';
  const rules = loadRules(); rules.push({id: ruleId(), instrument: first, condition: 'above', threshold: 0, enabled: true}); saveRules(rules); renderRules();
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
});
renderRules();

function watchlist() { return $('#watchlist').value.split(',').map(value => value.trim()).filter(Boolean); }
function price(value) { return value == null ? '—' : fmt(value); }
function renderQuotes(quotes) {
  $('#quote-grid').innerHTML = quotes.map(q => {
    const available = q.price != null; const direction = (q.change || 0) >= 0 ? 'positive' : 'negative';
    const bestBid = q.bids?.[0], bestAsk = q.asks?.[0];
    return `<article class="quote-card ${available ? '' : 'unavailable'}">
      <div class="quote-head"><div class="quote-symbol"><strong>${q.symbol}</strong><span>${q.name}</span></div><span class="market-tag">${q.market}</span></div>
      <div class="quote-price"><strong>${price(q.price)}</strong><span class="${direction}">${q.change == null ? '—' : `${q.change >= 0 ? '+' : ''}${fmt(q.change)} · ${q.change_pct >= 0 ? '+' : ''}${fmt(q.change_pct)}%`}</span></div>
      <div class="quote-stats"><div><small>開</small><b>${price(q.open)}</b></div><div><small>高</small><b>${price(q.high)}</b></div><div><small>低</small><b>${price(q.low)}</b></div><div><small>量（張）</small><b>${q.volume == null ? '—' : fmt(q.volume,0)}</b></div></div>
      <div class="quote-depth"><div class="depth-side"><span>買一</span><b>${bestBid ? price(bestBid.price) : '—'}</b></div><div class="depth-side"><span>賣一</span><b>${bestAsk ? price(bestAsk.price) : '—'}</b></div></div>
      <div class="quote-time">${q.timestamp ? q.timestamp.replace('T',' ').replace('+08:00','') : q.status}</div>
    </article>`;
  }).join('');
}

function notifyAlerts(events) {
  const next = new Set(events.map(event => event.rule_id));
  events.forEach(event => {
    if (activeAlerts.has(event.rule_id)) return;
    if ('Notification' in window && Notification.permission === 'granted') new Notification('島嶼量化價格警示', {body: event.message});
  });
  activeAlerts = next;
}

async function refreshQuotes() {
  if (monitorBusy || !watchlist().length) return;
  monitorBusy = true; $('#refresh-quotes').disabled = true; $('#monitor-error').hidden = true;
  try {
    saveRules(collectRules());
    const response = await fetch('/api/v1/monitor/snapshot', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({instruments:watchlist(),alerts:loadRules()})});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error?.message || '行情載入失敗');
    const result = data.result; renderQuotes(result.quotes); notifyAlerts(result.alerts);
    $('#quote-source').textContent = `${result.provider} · 最快 ${result.minimum_interval_seconds} 秒更新`;
    $('#updated-at').textContent = `更新 ${result.fetched_at.replace('T',' ').slice(0,19)}`;
    $('#market-state').textContent = result.market_session === 'open' ? '交易時段' : '非交易時段';
    $('#market-light').classList.toggle('open', result.market_session === 'open');
  } catch (err) { $('#monitor-error').textContent = err.message; $('#monitor-error').hidden = false; }
  finally { monitorBusy = false; $('#refresh-quotes').disabled = false; }
}

function scheduleMonitor() {
  clearInterval(monitorTimer); monitorTimer = null;
  const seconds = Number($('#refresh-interval').value);
  if (seconds) monitorTimer = setInterval(refreshQuotes, seconds * 1000);
}
$('#refresh-quotes').addEventListener('click', refreshQuotes);
$('#refresh-interval').addEventListener('change', scheduleMonitor);
$('#watchlist').addEventListener('change', () => { localStorage.setItem('tw-stock-tool.watchlist.v1', $('#watchlist').value); refreshQuotes(); });
$('#watchlist').value = localStorage.getItem('tw-stock-tool.watchlist.v1') || $('#watchlist').value;

let capabilitiesLoaded = false;
async function loadCapabilities() {
  if (capabilitiesLoaded) return;
  try {
    const response = await fetch('/api/v1/capabilities'); const data = await response.json(); if (!data.ok) return;
    const c = data.result; const providers = Object.values(c.historical_providers).join(' / ');
    $('#capabilities').innerHTML = `<div><dt>歷史資料</dt><dd>${providers}</dd></div><div><dt>盤中行情</dt><dd>${c.quote_provider}</dd></div><div><dt>券商下單</dt><dd>${c.live_order_execution ? c.broker : '未啟用（安全）'}</dd></div>`;
    capabilitiesLoaded = true;
  } catch { /* Status remains visible as unavailable. */ }
}

// --- Factor discovery and synthesis --------------------------------------
const factorStartDate = new Date(today); factorStartDate.setFullYear(today.getFullYear() - 1);
$('#factor-start').value = localISO(factorStartDate);
$('#factor-end').value = localISO(today);

async function loadFactorCatalog() {
  if ($('#factor-count').textContent !== '—') return;
  try {
    const response = await fetch('/api/v1/factors/catalog'); const data = await response.json();
    if (data.ok) $('#factor-count').textContent = data.result.length;
  } catch { $('#factor-count').textContent = '?'; }
}

function factorRequest() {
  return {
    instruments: $('#factor-universe').value.split(',').map(value => value.trim()).filter(Boolean),
    start: $('#factor-start').value,
    end: $('#factor-end').value,
    horizon: Number($('#factor-horizon').value),
    train_ratio: Number($('#factor-train-ratio').value),
    top_k: Number($('#factor-top-k').value),
    correlation_threshold: 0.85,
  };
}

function metricCard(label, value, suffix = '', tone = false, digits = 3) {
  const className = tone && value != null ? (value >= 0 ? 'positive' : 'negative') : '';
  return `<div class="metric"><span>${label}</span><strong class="${className}">${value == null ? '—' : `${fmt(value, digits)}${suffix}`}</strong></div>`;
}

function factorStatus(status) {
  return {stable:'穩定', weak:'樣本外偏弱', sign_flip:'樣本外反向', insufficient:'資料不足'}[status] || status;
}

$('#discover-factors').addEventListener('click', async () => {
  const button = $('#discover-factors'), error = $('#factor-error');
  error.hidden = true; $('#factor-loading').hidden = false; button.disabled = true; button.querySelector('span').textContent = '挖掘中…';
  try {
    const response = await fetch('/api/v1/factors/discover', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(factorRequest())});
    const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.error?.message || '因子挖掘失敗');
    factorDiscovery = data.result; renderFactorDiscovery(factorDiscovery);
  } catch (err) { error.textContent = err.message; error.hidden = false; }
  finally { $('#factor-loading').hidden = true; button.disabled = false; button.querySelector('span').textContent = '開始挖掘'; }
});

function renderFactorDiscovery(result) {
  $('#factor-results').hidden = false; $('#synthesis-results').hidden = true;
  const settings = result.settings, first = result.candidates[0];
  $('#factor-summary').innerHTML = [
    metricCard('候選因子', result.candidates.length, ' 個', false, 0),
    metricCard('去重後保留', result.selected_keys.length, ' 個', false, 0),
    metricCard('共同交易日', result.meta.common_dates, ' 日', false, 0),
    metricCard('訓練／測試切分', null),
    metricCard('最佳訓練 IC', first.train.mean_ic, '', true, 4),
  ].join('');
  $('#factor-summary').children[3].querySelector('strong').textContent = first.split_date;
  const selected = new Set(result.selected_keys);
  $('#factor-body').innerHTML = result.candidates.map(item => `<tr>
    <td><input class="factor-check" type="checkbox" value="${item.key}" ${selected.has(item.key) ? 'checked' : ''}></td>
    <td><strong>${item.label}</strong><br><small>${item.key}</small></td><td>${item.category}</td>
    <td class="${item.train.mean_ic >= 0 ? 'positive':'negative'}">${fmt(item.train.mean_ic,4)}</td>
    <td class="${item.test.mean_ic >= 0 ? 'positive':'negative'}">${fmt(item.test.mean_ic,4)}</td>
    <td>${fmt(item.train.ic_ir,2)}</td><td>${fmt(item.train.q_value,3)}</td><td>${fmt(item.test.mean_spread_pct,2)}%</td>
    <td><span class="status-pill ${item.status}">${factorStatus(item.status)}</span></td></tr>`).join('');
  updateSelectedFactorCount();
  $('#factor-results').scrollIntoView({behavior:'smooth',block:'start'});
}

function selectedFactorKeys() { return [...document.querySelectorAll('.factor-check:checked')].map(input => input.value); }
function updateSelectedFactorCount() { $('#selected-factor-count').textContent = `${selectedFactorKeys().length} 個已選`; }
$('#factor-body').addEventListener('change', updateSelectedFactorCount);

$('#synthesize-factors').addEventListener('click', async () => {
  const error = $('#factor-error'), button = $('#synthesize-factors'); error.hidden = true;
  const keys = selectedFactorKeys();
  if (keys.length < 2) { error.textContent = '請至少選擇兩個因子。'; error.hidden = false; return; }
  button.disabled = true; button.querySelector('span').textContent = '合成中…'; $('#factor-loading').hidden = false;
  try {
    const payload = {...factorRequest(), factor_keys:keys, method:$('#synthesis-method').value};
    const response = await fetch('/api/v1/factors/synthesize', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.error?.message || '因子合成失敗');
    factorSynthesis = data.result; renderFactorSynthesis(factorSynthesis);
  } catch (err) { error.textContent = err.message; error.hidden = false; }
  finally { button.disabled = false; button.querySelector('span').textContent = '合成並驗證'; $('#factor-loading').hidden = true; }
});

function renderFactorSynthesis(result) {
  const perf = result.performance; $('#synthesis-results').hidden = false;
  $('#synthesis-metrics').innerHTML = [
    metricCard('訓練 IC',perf.train.mean_ic,'',true,4), metricCard('樣本外 IC',perf.test.mean_ic,'',true,4),
    metricCard('樣本外 ICIR',perf.test.ic_ir,'',true,2), metricCard('樣本外多空差',perf.test.mean_spread_pct,'%',true,2),
    metricCard('平均排名換手',result.average_rank_turnover_pct,'%',false,1), metricCard('穩定性',null),
  ].join('');
  $('#synthesis-metrics').children[5].querySelector('strong').textContent = factorStatus(perf.status);
  const maxWeight = Math.max(...result.weights.map(item=>Math.abs(item.weight)),.001);
  $('#factor-weights').innerHTML = result.weights.sort((a,b)=>Math.abs(b.weight)-Math.abs(a.weight)).map(item => {
    const width = Math.abs(item.weight)/maxWeight*50;
    return `<div class="weight-row"><span>${item.label}</span><div class="weight-track"><i class="weight-zero"></i><i class="weight-fill ${item.weight>=0?'positive':'negative'}" style="width:${width}%"></i></div><b class="${item.weight>=0?'positive':'negative'}">${item.weight>=0?'+':''}${fmt(item.weight*100,1)}%</b></div>`;
  }).join('');
  $('#ranking-title').textContent = `最新排名 · ${result.latest_date}`;
  $('#factor-ranking').innerHTML = result.latest_ranking.map(item=>`<tr><td>${item.rank}</td><td>${item.symbol}</td><td class="${item.score>=0?'positive':'negative'}">${fmt(item.score,4)}</td></tr>`).join('');
  requestAnimationFrame(()=>drawFactorIc(result));
  $('#synthesis-results').scrollIntoView({behavior:'smooth',block:'start'});
}

function drawFactorIc(result) {
  const canvas = $('#factor-ic-chart'); if (canvas.closest('[hidden]')) return;
  const {ctx,w,h}=canvasSetup(canvas), rows=result.performance.daily_ic;
  const values=rows.map(row=>row.ic); let [min,max]=bounds([...values,0],.1); min=Math.min(min,-.05); max=Math.max(max,.05);
  const box=axes(ctx,w,h,min,max,rows.map(row=>row.date)); const x=i=>box.l+(box.r-box.l)*i/Math.max(1,rows.length-1); const y=v=>box.b-(v-min)/(max-min)*(box.b-box.t);
  ctx.strokeStyle='#adb5b0';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(box.l,y(0));ctx.lineTo(box.r,y(0));ctx.stroke();
  line(ctx,rows.map((row,i)=>[x(i),y(row.ic)]),'#135c45',1.7);
  const splitIndex=rows.findIndex(row=>row.date>=result.performance.split_date);
  if(splitIndex>=0){ctx.strokeStyle='#9ca49f';ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(x(splitIndex),box.t);ctx.lineTo(x(splitIndex),box.b);ctx.stroke();ctx.setLineDash([]);}
}
