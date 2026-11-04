/* The Sell button (Portfolio and Saxo accounts pages) — DESIGN.md §11a.

   Nothing here places an order. "Send to approval" creates a MANUAL sell
   (POST /api/manual-trade) that then waits on the Approvals page like every
   other trade, where you confirm the shares and the account with the
   approval token. On a Saxo account the desk first cancels the position's
   resting stop-loss / take-profit (Saxo refuses a sell while they rest),
   then sells; any shares left are re-protected at the same levels.

   A page registers each sellable position with sellRegister(key, pos) while
   rendering, puts <div id="sellbox-KEY"></div> where the form should open,
   and calls sellRestore() after every re-render so an open form survives
   the page's auto-refresh. pos = {ticker, exchange, broker, account_id,
   label, held, price, currency, whole}. Every server string is escaped. */
const SELLPOS = {}, SELLS = {};

function sellRegister(key, pos) { SELLPOS[key] = pos; }

function sellButton(key) {
  return `<button class="ghost" onclick="sellOpen(${jsArg(key)})"
    title="sell shares the desk holds — goes to Approvals first">Sell…</button>`;
}

function sellOpen(key) {
  const p = SELLPOS[key];
  if (!p) return;
  if (!SELLS[key]) {
    SELLS[key] = {qty: p.held, px: p.price != null ? Math.round(p.price * 100)
      / 100 : '', note: '', msg: ''};
  }
  sellRender(key);
}

function sellClose(key) {
  delete SELLS[key];
  const el = document.getElementById('sellbox-' + key);
  if (el) el.innerHTML = '';
}

function sellRestore() {
  Object.keys(SELLS).forEach(k => { if (SELLPOS[k]) sellRender(k); });
}

function sellSave(key) {
  const s = SELLS[key];
  if (!s) return;
  const g = n => (document.getElementById(n + '-' + key) || {}).value;
  s.qty = g('sq'); s.px = g('sp'); s.note = g('sn');
}

function sellRender(key) {
  const el = document.getElementById('sellbox-' + key);
  const p = SELLPOS[key], s = SELLS[key];
  if (!el || !p || !s) return;
  const k = esc(key), a = jsArg(key);
  el.innerHTML = `<div class="card" style="margin:.4rem 0;padding:.6rem .8rem;
      border:1px solid var(--warn)">
    <p style="margin:0 0 .3rem"><b>Sell ${esc(p.ticker)}</b>
      <span class="muted">on ${esc(p.label || p.account_id)} · the desk holds
      <b>${fmt(p.held, 4)}</b> · last <b>${p.price == null ? '—'
      : fmt(p.price, 2) + ' ' + esc(p.currency || '')}</b></span></p>
    <div class="row" style="flex-wrap:wrap;gap:.5rem">
      <label>shares <input id="sq-${k}" type="number" min="0"
        step="${p.whole ? 1 : 'any'}" max="${esc(p.held)}" style="width:90px"
        value="${esc(s.qty)}" oninput="sellSave(${a})"></label>
      <label>limit price <input id="sp-${k}" type="number" min="0"
        step="0.01" style="width:100px" value="${esc(s.px)}"
        oninput="sellSave(${a})"></label>
      <input id="sn-${k}" placeholder="note (optional)" maxlength="200"
        style="flex:1;min-width:140px" value="${esc(s.note)}"
        oninput="sellSave(${a})">
    </div>
    <div class="row" style="margin-top:.4rem;gap:.5rem">
      <button type="button" onclick="sellCheck(${a})">✓ Check with Saxo</button>
      <button type="button" onclick="sellSend(${a})">Send to approval →</button>
      <button type="button" class="ghost" onclick="sellClose(${a})">✖</button>
    </div>
    <p id="sm-${k}" style="font-size:.82rem;margin:.3rem 0">${s.msg || ''}</p>
    <p class="muted" style="font-size:.72rem;margin:0">Nothing is sold yet:
      this puts a SELL on the Approvals page, where you confirm shares and
      account. A limit sell only fills at that price or better. At Saxo the
      desk first cancels this position's stop-loss / take-profit (Saxo will
      not take a sell while they rest), then sells; shares left over are
      re-protected at the same levels.</p></div>`;
}

function sellValues(key) {
  sellSave(key);
  const s = SELLS[key] || {}, p = SELLPOS[key] || {};
  return {qty: Number(s.qty), entry: Number(s.px), note: s.note || null, p};
}

function sellMsg(key, html) {
  if (SELLS[key]) SELLS[key].msg = html;
  const el = document.getElementById('sm-' + key);
  if (el) el.innerHTML = html;
}

async function sellCheck(key) {
  const v = sellValues(key);
  if (!(v.qty > 0) || !(v.entry > 0))
    return sellMsg(key, '<b class="bad">enter shares and a limit price</b>');
  sellMsg(key, '<span class="muted">asking Saxo…</span>');
  try {
    const r = await api('/api/trade-check', {ticker: v.p.ticker,
      exchange: v.p.exchange, direction: 'sell', qty: v.qty, entry: v.entry,
      broker: v.p.broker});
    sellMsg(key, r.ok === null ? `<span class="warn">${esc(r.note)}</span>`
      : r.ok ? `<b class="ok">✔ Saxo would accept:</b> sell ${esc(r.amount)}
          @ ${esc(r.price)}`
      : `<b class="bad">✖ Saxo would refuse:</b> ${esc(r.error)}`);
  } catch (e) { sellMsg(key, `<b class="bad">${esc(e.message)}</b>`); }
}

async function sellSend(key) {
  const v = sellValues(key);
  if (!(v.qty > 0) || !(v.entry > 0))
    return sellMsg(key, '<b class="bad">enter shares and a limit price</b>');
  if (v.qty > v.p.held + 1e-9)
    return sellMsg(key, `<b class="bad">the desk holds only ${
      fmt(v.p.held, 4)} — it can only sell what it bought</b>`);
  if (!confirm(`Send to approval: SELL ${v.qty} ${v.p.ticker} at limit ` +
               `${v.entry} on ${v.p.label || v.p.account_id}?\n\nNothing ` +
               'is sold until you approve it on the Approvals page.')) return;
  try {
    const r = await api('/api/manual-trade', {ticker: v.p.ticker,
      exchange: v.p.exchange, direction: 'sell', entry: v.entry,
      note: v.note, source: v.p.source || 'portfolio',
      suggested: {qty: v.qty, broker: v.p.broker}});
    sellMsg(key, `<b class="ok">✔ sent</b> — approve it on
      <a href="/approvals#c-${esc(r.work_item_id)}">Approvals →</a>
      <span class="muted">(${esc(r.work_item_id)})</span>`);
    toast('sell sent to approval ✓');
  } catch (e) { sellMsg(key, `<b class="bad">${esc(e.message)}</b>`); }
}
