function nav(active) {
  const items = [["/", "Mission Control"], ["/approvals", "Approvals"],
    ["/markets", "Markets"],
    ["/claire", "Claire"], ["/trading", "Trading"],
    ["/portfolio", "Portfolio"], ["/agents", "Agents & Schedule"],
    ["/providers", "Providers & MCP"]];
  document.write('<nav>' + items.map(([href, label]) =>
    `<a href="${href}" class="${href === active ? 'active' : ''}">${label}</a>`
  ).join('') +
  '<span style="flex:1"></span><a href="#" onclick="pair();return false" ' +
  'title="paste the dashboard key from etc/claire.env">🔑</a>' +
  '</nav><div id="msg"></div>');
}
function dashKey() { return localStorage.getItem('dashKey') || ''; }
function pair() {
  const k = prompt('Dashboard key (CLAIRE_DASHBOARD_KEY in etc/claire.env):',
    dashKey());
  if (k !== null) { localStorage.setItem('dashKey', k.trim()); toast('key saved'); }
}
async function api(path, opts) {
  const headers = {'X-Dash-Key': dashKey()};
  let init;
  if (opts) {
    headers['Content-Type'] = 'application/json';
    init = {method: 'POST', headers, body: JSON.stringify(opts)};
  } else init = {headers};
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || r.status);
  return data;
}
function toast(text, ok = true) {
  const el = document.getElementById('msg');
  el.textContent = text; el.style.display = 'block';
  el.style.borderColor = ok ? 'var(--good)' : 'var(--bad)';
  setTimeout(() => el.style.display = 'none', 4000);
}
function fmt(n, dp = 2) {
  return n == null ? '—' : Number(n).toLocaleString(undefined,
    {maximumFractionDigits: dp});
}
function fmtBig(n) {
  if (n == null) return '—';
  const a = Math.abs(n);
  if (a >= 1e12) return fmt(n / 1e12) + 'T';
  if (a >= 1e9) return fmt(n / 1e9) + 'B';
  if (a >= 1e6) return fmt(n / 1e6) + 'M';
  if (a >= 1e3) return fmt(n / 1e3) + 'K';
  return fmt(n);
}
function ts(t) { return t ? new Date(t * 1000).toLocaleString() : '—'; }
function ago(t) {
  if (!t) return '—';
  const s = Date.now() / 1000 - t;
  if (s < 90) return Math.round(s) + 's ago';
  if (s < 5400) return Math.round(s / 60) + 'm ago';
  if (s < 172800) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
/* HTML-escape for BOTH text and attribute context. The old textContent →
   innerHTML trick left quotes alone, so title="${esc(x)}" could still be
   broken out of. Everything server-sent (provider names, models, tickers,
   work-item ids) goes through this before it touches innerHTML. */
const esc = s => String(s ?? '').replace(/[&<>"'`]/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  '`': '&#96;'})[c]);
/* a value as a JS string literal inside an inline handler attribute:
   onclick="f(${jsArg(id)})". JSON.stringify makes it a safe JS literal and
   esc keeps it inside the attribute; the browser decodes the entities back
   before the JS runs. Plain esc is NOT enough there — &#39; decodes to '
   before the handler is parsed. */
const jsArg = s => esc(JSON.stringify(String(s ?? '')));
function sparkline(series, w = 70, h = 22) {
  if (!series || series.length < 2) return '';
  const lo = Math.min(...series), hi = Math.max(...series);
  const up = series[series.length - 1] >= series[0];
  const pts = series.map((v, i) =>
    `${(i / (series.length - 1) * w).toFixed(1)},` +
    `${(h - 2 - (h - 4) * (v - lo) / (hi - lo || 1)).toFixed(1)}`).join(' ');
  return `<svg class="spark" width="${w}" height="${h}"><polyline
    points="${pts}" fill="none" stroke-width="1.4"
    stroke="${up ? 'var(--good)' : 'var(--bad)'}"/></svg>`;
}
/* minimal markdown: headings, bold, italics, code, bullets — no library */
function mdLite(t) {
  return esc(t)
    .replace(/^#{1,3} (.+)$/gm, '<h3>$1</h3>')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/(^|\s)_(.+?)_(?=\s|$)/g, '$1<i>$2</i>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, '<ul>$1</ul>')
    .split(/\n{2,}/).map(p => p.match(/^\s*<(h3|ul)/) ? p
      : `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
}
function feeFor(model, notional) {
  if (!model) return 0;
  if (model.type === 'pct')
    return Math.max(notional * Number(model.pct || 0), Number(model.min || 0));
  return Number(model.per_trade || 0);
}
