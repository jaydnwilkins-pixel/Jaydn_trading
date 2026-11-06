// Live (real-money) trading switch, limits and metrics — DESIGN.md §22.
// Shared by Mission Control (the switch) and Orders (switch + metrics +
// editable limits). Turning OFF works from any paired device; turning ON
// only from a browser on the desk's own PC, with the typed phrase — the
// server enforces both, this file only shows what is possible.

let LIVE = null;

function liveWhen(t) {
  if (!t) return "—";
  return new Date(t * 1000).toLocaleString([], {day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit"});
}
function livePx(n) { return n == null ? "—" : fmt(n, Math.abs(n) < 10 ? 3 : 2); }

async function liveLoad() {
  LIVE = await api('/api/live-status');
  return LIVE;
}

function liveCcy() {
  const a = (LIVE && LIVE.accounts || [])[0];
  return a ? a.base_currency : '';
}

function liveStateLine(st) {
  if (st.armed) return '<b class="bad">⚠ ARMED — real-money orders can be placed</b>';
  if (!st.enabled_in_env) return '<b>OFF</b> <span class="muted">· not enabled on this PC</span>';
  if (!st.accounts.length) return '<b>OFF</b> <span class="muted">· no live account set up</span>';
  return '<b class="ok">OFF</b> <span class="muted">· disarmed</span>';
}

// The switch: a big on/off control plus why ON may be unavailable.
function liveSwitchHtml(st, {compact = false} = {}) {
  const on = st.armed;
  const btn = on
    ? `<button class="reject" onclick="liveDisarm()">Turn OFF live trading</button>`
    : `<button ${st.can_arm_here ? '' : 'disabled'} onclick="liveArm()"
         title="${esc(st.arm_blocked || '')}">Turn ON live trading</button>`;
  const why = !on && st.arm_blocked
    ? `<div class="muted small" style="margin-top:.3rem">Can't turn on here: ${esc(st.arm_blocked)}</div>` : '';
  const last = !on && st.disarm_reason
    ? `<div class="muted small">last turned off ${liveWhen(st.disarmed_at)} — ${esc(st.disarm_reason)}</div>` : '';
  const armedAt = on ? `<div class="muted small">on since ${liveWhen(st.armed_at)} by ${esc(st.armed_by || '?')}</div>` : '';
  return `<div class="card live-switch" style="border:2px solid ${on ? 'var(--bad)' : 'var(--line)'};
      display:flex;gap:1rem;align-items:center;flex-wrap:wrap;justify-content:space-between">
    <div><div>Live trading (Saxo, real money): ${liveStateLine(st)}</div>
      ${armedAt}${last}${why}
      ${compact ? '' : `<div class="muted small">Even when ON, every live trade still needs your approval with LIVE typed, and limit orders only.</div>`}
    </div>
    <div>${btn}</div></div>`;
}

async function liveArm() {
  const st = LIVE || {};
  const lim = st.limits || {};
  const typed = prompt(
    `⚠ TURN ON REAL-MONEY TRADING on Saxo.\n\n` +
    `Limits: ${lim.max_order_base} per order · ${lim.max_orders_per_day} orders/day · ` +
    `${lim.max_daily_notional_base}/day (${liveCcy()}).\n` +
    `Each trade still needs your approval, typing LIVE.\n\n` +
    `Type exactly:  ${st.arm_phrase}`);
  if (typed === null) return;
  try {
    LIVE = await api('/api/live-arm', {phrase: typed});
    toast('live trading is ON — armed');
  } catch (e) { toast(e.message, false); }
  liveRefresh();
}

async function liveDisarm() {
  if (!confirm('Turn OFF live trading? New live orders stop at once; orders already at Saxo keep their stop-loss / take-profit.')) return;
  try {
    LIVE = await api('/api/live-disarm', {reason: 'turned off from the dashboard'});
    toast('live trading is OFF');
  } catch (e) { toast(e.message, false); }
  liveRefresh();
}

// pages define liveRefresh() to re-render after a change
function liveRefresh() { if (window.onLiveChange) window.onLiveChange(); }

// ── Orders page: metrics + editable limits ──────────────────────────────
const LIMIT_LABELS = {
  max_order_base: ['Per order', 'largest single buy'],
  max_orders_per_day: ['Orders per day', 'live buys placed per Singapore day'],
  max_daily_notional_base: ['Per day total', 'sum of today\'s live buys'],
};

function liveMetricsHtml(st) {
  const m = st.metrics || {}, lim = st.limits || {}, ccy = liveCcy();
  const accts = Object.entries(m.accounts || {});
  const usage = accts.length ? accts.map(([id, a]) => {
    const pctN = lim.max_daily_notional_base ? Math.min(100, 100 * a.notional_today / lim.max_daily_notional_base) : 0;
    return `<div class="statstrip">
      <div class="stat"><span>account</span>${esc(id)} (${esc(a.currency)})</div>
      <div class="stat"><span>buys today</span>${a.orders_today} of ${esc(lim.max_orders_per_day)} · ${a.orders_left} left</div>
      <div class="stat"><span>spent today</span>${fmt(a.notional_today)} of ${fmt(lim.max_daily_notional_base)} · ${fmt(a.notional_left)} left</div>
      <div class="stat"><span>exits today</span>${a.exits_today} <span class="muted" style="text-transform:none;letter-spacing:0">(not capped)</span></div>
      <div class="stat"><span>all time</span>${a.entries_all_time} buys · ${fmt(a.notional_all_time)}</div>
    </div>
    <div style="height:6px;background:var(--line);border-radius:3px;max-width:520px;margin:-.4rem 0 .8rem">
      <div style="height:6px;width:${pctN}%;background:${pctN >= 90 ? 'var(--bad)' : 'var(--warn)'};border-radius:3px"></div></div>`;
  }).join('') : '<p class="muted small">No live account yet, so nothing to track. Usage appears here after <code>bin/claire live setup</code>.</p>';

  const limits = `<table class="ot" style="max-width:640px"><tr><th>Limit</th><th>Now</th><th>New value</th><th></th></tr>
    ${Object.entries(LIMIT_LABELS).map(([k, [label, hint]]) => `<tr>
      <td>${label}<div class="muted small">${hint}</div></td>
      <td>${k === 'max_orders_per_day' ? esc(lim[k]) : fmt(lim[k]) + ' ' + esc(ccy)}</td>
      <td><input id="lim-${k}" type="number" min="${k === 'max_orders_per_day' ? 1 : 0.01}"
        step="${k === 'max_orders_per_day' ? 1 : 'any'}" value="${esc(lim[k])}" style="width:8rem"></td>
      <td></td></tr>`).join('')}
    </table>
    <button onclick="liveSaveLimits()" style="margin-top:.4rem">Save limits</button>
    <span class="muted small">Changes apply at once to the next live buy. Exits (selling what the desk bought) are never capped.</span>`;

  const orders = (m.orders || []).length ? `<table class="ot"><tr><th>When</th><th>Side</th><th>Instrument</th>
      <th>Shares</th><th>Limit</th><th>Status</th></tr>
      ${m.orders.map(o => `<tr><td class="small">${liveWhen(o.placed_at || o.created_at)}</td>
        <td>${esc(o.side)}</td><td>${esc(o.instrument_id)}</td><td>${fmt(o.qty, 0)}</td>
        <td>${livePx(o.limit_price)}</td><td>${esc(o.status)}</td></tr>`).join('')}</table>`
    : '<p class="muted small">No live orders yet.</p>';

  const events = (m.events || []).length ? `<table class="ot"><tr><th>When</th><th>What</th><th>By</th><th>Detail</th></tr>
      ${m.events.map(e => { let p = {}; try { p = JSON.parse(e.payload || '{}'); } catch (_) {}
        const detail = e.event === 'limits'
          ? Object.keys(p.after || {}).filter(k => (p.before || {})[k] !== p.after[k])
              .map(k => `${(LIMIT_LABELS[k] || [k])[0]}: ${p.before[k]} → ${p.after[k]}`).join(' · ')
          : (p.reason || '');
        return `<tr><td class="small">${liveWhen(e.ts)}</td><td>${esc(e.event)}</td>
          <td class="small">${esc(e.actor)}</td><td class="small">${esc(detail)}</td></tr>`; }).join('')}</table>`
    : '<p class="muted small">The switch has never been turned on or off, and the limits have never been changed.</p>';

  return `${liveSwitchHtml(st)}
    <h3 style="margin-top:.8rem">Today's allowance</h3>${usage}
    <h3>Limits</h3>${limits}
    <h3 style="margin-top:1rem">Live orders</h3>${orders}
    <h3 style="margin-top:1rem">Switch &amp; limit history</h3>${events}`;
}

async function liveSaveLimits() {
  const lim = (LIVE && LIVE.limits) || {}, changed = {};
  for (const k of Object.keys(LIMIT_LABELS)) {
    const v = Number(document.getElementById('lim-' + k).value);
    if (!(v > 0)) { toast(`${LIMIT_LABELS[k][0]} must be a positive number`, false); return; }
    if (v !== Number(lim[k])) changed[k] = v;
  }
  if (!Object.keys(changed).length) { toast('no changes'); return; }
  try {
    LIVE = await api('/api/live-limits', {limits: changed});
    toast('live limits saved ✓');
  } catch (e) { toast(e.message, false); }
  liveRefresh();
}
