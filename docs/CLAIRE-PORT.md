# Port to Claire — the 2026-11-10 / 11 Ronathan changes

**For:** a Claude Code session working in `/mnt/projects/claire_trading` (Claire, Richard's desk).
**Source:** Ronathan (`/home/jaydn/jaydn_trading`), Jaydn's fork of Claire. All changes below were built, tested (778 offline tests passing) and are running on Ronathan.
**Reference patch:** `claire-port/ronathan-2026-11-11.patch` (next to this file): `git diff` from Ronathan commit `547ce3b` (the state Claire matches) to the commits that hold these changes, new files included. Use it as the reference implementation; do **not** apply it blindly (see "Where Claire differs" first).

---

## 0. Read this first: where Claire differs from Ronathan

Claire and Ronathan's pre-change code are almost identical once names are swapped (`Ronathan→Claire`, `ronathan→claire`, `RONATHAN_→CLAIRE_`). Files that were byte-identical after renaming: `app/watchlist.py`, `app/approvals.py`, `app/graph/pipeline.py`, `app/watcher.py`, `app/tools/market.py`, `app/tools/saxo_chart.py`, `app/tools/tradingview.py`, `app/api/desk.py`, `web/orders.html`, `web/watchlist.html`. Those hunks should apply with only the renames.

Real differences you must handle:

| Area | Ronathan | Claire | What it means for the port |
|---|---|---|---|
| Ports | dashboards 7797, api 7798 | dashboards **7787**, api **7788** | Use Claire's ports everywhere (e.g. `DeskApi("http://127.0.0.1:7788", …)`). |
| Env file / prefix | `etc/ronathan.env`, `RONATHAN_*` | `etc/claire.env`, `CLAIRE_*` | e.g. `RONATHAN_TELEGRAM_ENV` → `CLAIRE_TELEGRAM_ENV`, `RONATHAN_NO_SAXO_CHARTS` → `CLAIRE_NO_SAXO_CHARTS`. |
| Owner | Jaydn | Richard | Docstrings / UI text only. |
| **Trailing floor** | Only a **watcher alert** (starts a sell review; never sells by itself) | A **real Saxo trailing stop**: `nodes_execution.trail_distance()` turns `trail_pct` into `TrailingStopIfTraded` at Saxo (distance = trail % × entry limit), `venue_stops` / `local_exchanges` on accounts, precheck sends `trail_distance` | **Wording must differ** (see §3/§4): on Claire, for Saxo accounts (not SGX), call it **"Trailing stop %"** and say it *sells automatically*; keep "Trailing alert" only where it is the watcher alert (built-in sims, SGX on saxo-paper). The `/floor` command (§1.4) must also move the **Saxo trailing-stop order**, not just the watcher rule — see §1.4. |
| **Built-in sim stops** (§12) | The sim holds a bracket's stop-loss / take-profit as resting orders; the custodian fills them | Claire's built-in sims (alpaca, moomoo, SGX on saxo-paper) are the same `PaperSimBroker` — port as is. Saxo (non-SGX) is untouched: Saxo holds those itself. |
| Live account on cards | Removed from screens (SIM only) | Always **shown** on the Approvals card (disabled until armed) | Keep Claire's behaviour; the new account ordering puts live **last** (rank 3), so the default stays Saxo SIM. |
| `/api/saxo-account` default | `env=sim` | `env=live` | Unrelated; leave. |
| Telegram | Ronathan now has its **own bot** that polls updates itself | Claire uses **Bob's bot** (`~/bob/telegram.env`, Bob relays taps to `POST /api/telegram-decision`) | The approval-card changes (§1.1–1.2) work through Bob's relay unchanged. The **commands** (§1.3) need updates *from* Telegram: either give Claire its own bot (`TELEGRAM_POLL=1`, as Ronathan did), or teach Bob to relay `/analyze /buy /sell /positions /orders /floor` messages and `rb:` button taps. **Ask Richard which.** Never run two `getUpdates` pollers on one token. |
| Search | Twelve Data → Yahoo | Twelve Data → Yahoo, **with a `TWELVEDATA_API_KEY` set** | Claire's search already works (Twelve Data). The Saxo+TradingView fallback (§6) is still worth adding for when Twelve Data fails, and for the "tradable at Saxo SIM" tag. Lower priority. |

Also: `git` in that repo may report "dubious ownership" for other users. That's only a concern when reading it from another account.

---

## Suggested order

1. §2 Saxo SIM default + sim labels (small, everything else assumes it)
2. §8 the wording (Purchase price, Stop-loss, Take-profit, Trailing alert) — every later section uses it
3. §3 Approvals card rebuild (the big one) + §3.6 arbiter's notes
4. §4 Order summary + §9 the shared `orderform.js` panel and the fill-price rule
5. §10 sell forms, §11 Orders → Edit, Portfolio → Edit trailing alert
6. §5 Orders page grouped by stock
7. §12 built-in sim enforces stops
8. §1 Telegram (after deciding own-bot vs Bob relay)
9. §6 Watchlist search fallback, §7 Watchlist "trailing" field, §13 pence fix, §14 maintenance checks

Run `tests/run.sh` after each step. Restart with `bin/claire restart` only when `bin/claire status` says "in flight: nothing" (a graceful stop refuses while a run is mid-pipeline; never `--kill` without asking).

---

## 1. Telegram

### 1.1 Approval card: trailing-alert buttons (`app/telegram_approvals.py`)
**Updated 2026-11-11:** buttons read `alert 5% · ✓ alert 8% · …` (not "floor"); all card text says "Trailing alert". Internals keep the `floor` names.
- `FLOOR_CHOICES = (5, 8, 12, 15)`; helpers `floor_applies(f)` (buys only), `initial_floor(f)` (suggestion → else the approved `trail_pct` on a refused trade → else 8), `floor_choices(f)` (choices ∪ initial), `chosen_floor(f, opts)`, `pct_s()`.
- `send_notice`: `opts["trail"] = initial_floor(f)` for buys.
- `build_keyboard`: a row `floor 5% · ✓ floor 8% · …` (callback op `tf`, arg = value) on buy cards **and** on refused-buy retry cards. Keep the retry button label "🔁 Retry same order".
- `handle_decision`: op `tf` → `_op_floor` (validate value ∈ `floor_choices`, set `opts["trail"]`, **drop any pending ✔** so the confirmation always shows the floor it uses, `_restore` with a note).
- `_op_choose`: approve uses `chosen_floor`; the confirmation text shows "Trailing floor **X%**". Sells get **no** floor (a Telegram sell is always an exit; Ronathan changed the old behaviour and updated `test_suggestion_preselects_account_qty_and_trailing_floor`).
- Retry: `p["trail_pct"]` from the chosen floor; `_execute` passes `changes={"trail_pct": …}` to `approvals.retry_failed`.
- **Claire:** label the buttons/notes "trailing stop" when the selected account has `venue_stops` and the exchange is not local (SGX); the value still travels as `trail_pct`, so execution needs no change.
- Tests: `tests/test_telegram_approvals.py` → the five tests after `# ── trailing floor on the card` in the patch.

### 1.1b Approval card: shares, the fill price, new wording (2026-11-11)
- **Sized in shares**, like the dashboard card: `buy_choices(f, acct, opts)` turns the offered amounts (or a suggestion's `qty` + half) into share counts at the **fill price**; buttons read `9 sh ≈ 933 USD`. Approve sends `qty` (exactly those shares) and `size_base` = `reserve_for()` = shares × the worse of purchase price and live price × FX (the gate's cash check; execution reserves the same). Choices whose reserve exceeds free cash are dropped.
- **Fill price:** `limit_price(f)`, `fill_price(f)` (a buy limit above the market fills at about the market; a sell limit below it likewise), `worst_price(f)`, `fills_now(f)`. `ref_price` now = fill price, so % distances, shares and cost use it.
- `level_problems(f)`: a stop-loss / take-profit on the wrong side of the fill price (not for exits) → a ⚠ line on the card, and the size buttons refuse with that reason ("…would sell straight away. The desk will refuse it; change it on Approvals.").
- Message: `Purchase price: 110.00 — above the current price, so it fills straight away at about 103.67` (or `(-0.5% vs now — waits for that price or better)`); `Stop-loss: … · Take-profit: …`; `Reward : risk`; the suggested (else middle) choice as `About 1,000 USD: 9 shares ≈ 933.03 USD` plus `For those shares: most you can lose −X · you could make +Y`.
- Confirm: `BUY 9 CSCO ≈ 933.03 USD, purchase price 110.00 USD (fills now at about 103.67), stop-loss 96.40, take-profit 116.10 on … ? Trailing alert 8%.`
- `approved_what` prefers `qty` ("12 shares"). Sell buttons are valued at the fill price.
- Price-trigger rule text (`price_triggers.describe`): `(purchase price X)` / `(sale price X)` instead of `(limit X)`.
- Commands (`telegram_commands.py`): help and replies say stop-loss / take-profit / trailing alert; `/floor` keeps its name (and `trail=` / `floor=` both parse).
- Watcher sell-review trigger text: `watcher: trailing alert hit — … the alert is 8%` (was "trailing stop breached"). **Claire:** where the floor is a real Saxo trailing stop, keep "trailing stop" wording there.
- Tests: two new ones at the end of `tests/test_telegram_approvals.py`; ten updated for wording/sizing.

### 1.2 Account labels and default (Telegram side)
`accounts()` in `telegram_approvals.py`: built-in sims labelled `Ronathan sim · alpaca (simulated, not a broker)`, short `Ronathan sim (alpaca)`, kind `simulated`. Use **"Claire sim · …"**. Saxo SIM is already the default there.

### 1.3 Commands (`app/telegram_bot.py`, `app/telegram_commands.py` — new)
Only if Claire gets its own bot, or Bob relays commands (see §0).
- `telegram_bot.py`: long-polls `getUpdates` in the dashboards process when `telegram.env` has `TELEGRAM_POLL=1` (flag read by `alerts.telegram_settings()` → `"poll"`), stores the offset in `var/telegram_offset`, routes callback queries (`cl:` → `handle_decision`, `rb:` → commands) and owner-only private-chat messages (`LIVE`, `/…`). `/id` answers anyone with their user id (setup aid). It works before an approver id is set (so `/id` can be used to find it).
- `telegram_commands.py` (`Commands`, `DeskApi`): `/analyze TICKER [EXCHANGE]` → `/api/run` (rate limit 10/h, trigger `"started from Telegram (/analyze)"`, reports PASS/failed outcomes once via `telegram_run_notices`); `/buy TICKER [EXCHANGE] [SIZE] [trail=PCT]` → `/internal/manual` (live price, stop = 2×ATR14 or 8%, target 2:1, both capped at 24%, `order_type: market`, never suggests the live account); `/sell TICKER [all|half|QTY]` (only what the desk holds — never shorts; whole shares / SGX lots); `/positions` (Sell half / Sell all buttons); `/orders` (Cancel → ✔ confirm → `/internal/order`); `/floor [TICKER] [PCT]` (§1.4). Buttons are opaque `rb:<token>`, single-use, expire after 1 h. **Everything ends at the approval gate** — nothing places an order.
- `telegram_approvals.nudge()` + `_WAKE` event so the pusher sends the new card within seconds.
- Wire-up in `dashboards.main()` after `start_pusher_thread`: `telegram_bot.start_poller_thread(conn, market, os.environ, approvals.default_resume_post("http://127.0.0.1:7788", secret), var / "telegram_offset", commands=telegram_commands.Commands(conn, market, os.environ, telegram_commands.DeskApi("http://127.0.0.1:7788", secret), nudge=telegram_approvals.nudge))`.
- Register the `/` menu with `setMyCommands` (analyze, buy, sell, positions, orders, floor, help).
- Tests: `tests/test_telegram_bot.py`, `tests/test_telegram_commands.py` (new).

### 1.4 `/floor` — change the floor of a held position
Ronathan: `watcher.position_floor()` / `watcher.set_position_floor()` update the `price_alerts` `trail_pct` rule (threshold only; the high-water mark is kept), behind a ✔ Set / ✖ Keep confirmation.
**Claire:** on Saxo (non-SGX) positions the floor is a resting **TrailingStopIfTraded** order. `/floor` must also change that order's trailing distance at Saxo (amend or cancel-and-replace the leg via the adapter, the same way order editing works), then update the watcher rule. Treat this as a money-path change: two-step confirm, refuse rather than leave a position unprotected, test it like the protective-legs tests. If that is too much for one session, ship `/floor` for sims only and say so.

---

## 2. Saxo SIM default + sim labels

`app/dashboards.py`:
- `sim_label(broker)` → `"Claire sim · {broker} (simulated, not a broker)"`.
- `account_rank(a, env)`: Saxo SIM (with `SAXO_SIM_EXECUTION=enabled`) 0, own sims 2, live 3; `ordered_accounts(conn, env)` sorts `broker_accounts` by it.
- Use `ordered_accounts` where the Approvals account list is built **and** in `app/watchlist.py accounts()`, so every dropdown defaults to Saxo SIM.
- `_approval_account`: built-in sim label → `sim_label(...)`; `_account_label` (Orders page / sync line) → `sim_label` for alpaca/moomoo paper.
- Claire appends a placeholder live account when none exists — keep it; it sorts last.
- Update test expectations that matched the old labels (`"alpaca sim"`, `"alpaca built-in sim"`).

---

## 3. Approvals card rebuild (`web/approvals.html` + backend)

### 3.1 Backend: manual limit price and buy-by-shares
- `app/approvals.py _card_levels`: accept `levels.entry` (a typed limit price) → `entry_low = entry_high = entry`, validated (number, > 0) and then checked by the same stop/target/25% rules against the **new** entry. Return the changed keys.
- `app/graph/pipeline.py` (where approval `levels` are applied to the thesis): apply `entry_low`, `entry_high` as well as `stop_loss`, `take_profit`.
- `app/graph/nodes_execution.py` buy sizing: if `ap.qty` is set on a **buy**, buy exactly that many (floored to the board lot, or to whole shares on whole-share venues; refuse if it rounds to 0) instead of re-sizing `size_base` at the live price. **Claire has extra code here** (`_accepts`, `trail_distance`) — merge around it.
- Tests: `test_card_limit_price_replaces_the_entry`, `test_bad_card_limit_price_is_refused` (`tests/test_authorization.py`); `test_buy_by_shares_*` (`tests/test_execution.py`).

### 3.2 "Your order" panel (replaces the old inputs + the `mgrid` summary grid)
- **Order type** dropdown with three options (labels as of 2026-11-11 — no numbers, no "limit"):
  1. `The pipeline's recommendation (<entry> <ccy>)` (retry: "The purchase price you approved"; manual trade: "Your trade's recommendation") — fills purchase price/stop/target from the thesis; price **locked** (read-only, dashed).
  2. `Buy at the current price — fills now` — price = live price (locked); **stop-loss and take-profit recalculated** at the current price keeping the plan's % distances (`stop × live/entry`, `target × live/entry`); fallback when there is no plan: 2×ATR14 stop, 2:1 target; both clamped to ±24% (Saxo's 25% rule). Sends `order_type: "market"` and `levels.entry = live` (so the gate checks the stop against the live price, not the plan's older entry).
  3. `Manual — set your own price` — unlocks the price; sends `levels.entry`.
  Switching to 1/2 refills; switching to 3 keeps the current values. If option 2 is the default (Markets "Buy now" suggests `order_type: market`), fill it when the live price first arrives.
- **Account** dropdown (from `ordered_accounts`; label shows cash).
- Inputs: **Purchase price** (**Sale price** on a sell; `ccy/share`), **Stop-loss** (`ccy`), **Take-profit** (`ccy · optional`), **Trailing alert %** (Claire: **"Trailing stop %"** when the account has `venue_stops` and the exchange is not in `local_exchanges`, else "Trailing alert %").
- **Amount to spend** `[ ] USD` ⇄ **Shares** `[ ]` — type either, the other follows (`LEAD[id]` remembers which you typed). Shares counted at the limit price (options 1/3) or live price (2) × FX (`CTX.fx[acct.ccy]`), floored to lots (SGX 100) / whole shares (Saxo) / 0.1 (sims). Note beside: "whole shares only (Saxo) · 1 share ≈ X USD". Amount shows the account currency.
- **Approve** sends `qty` = the shares shown (what you see is what is bought) **and** `size_base` = shares × max(limit, live) × FX (for the gate's cash check), plus `levels`, `trail_pct`, `order_type`, broker; the confirm dialog reads like the summary.
- Keep form state across the 30 s refresh: add `lvent-` and `shr-` to `CARD_INPUTS`; after `restoreForm()` call `applyMode(id, false)`.
- Removed: the "What these mean" explainer (Jaydn asked for it to go) and `prefillSize`.
- Header gets a `prices in <ccy>` pill. "the entry and floor are yours" → "the limit price and stop-loss are yours".

### 3.3 Layout order on the card
header → market-data table (`ctxTable(c, {held: false})`) → **Your order** (panel → order summary → Approve/Reject) → the "desk holds N shares" line → **Arbiter's notes** → Evidence. Evidence sections are all **collapsed by default** (remove the `open` on the arbiter one).

### 3.4–3.5 (sell / short cards)
Same panel and order types ("Sell at the current price"); quantity is shares only; exits have no stop/target/floor. **Updated 2026-11-11:** the summary is `sellSummary()` from `orderform.js` (§10), not rows.

### 3.6 Arbiter's notes in plain English (`app/arbiter_notes.py` — new)
- The arbiter's `conditions` are rewritten once per (work item, exact notes) by **one text-only LLM call** using the `recorder` agent's model (`registry.model_for(conn, "recorder", work_item_id=…)`, metered), into `{summary, notes:[plain…]}` JSON; stored in `arbiter_notes` (sha256 of the notes as key). Bad/failed answers are not stored.
- Background writer thread in the dashboards process (`start_writer_thread`, every 30 s, back-off 10 min per failure) so notes are ready before the card is opened.
- `GET /api/arbiter-notes?id=` in `dashboards.py`: cached → free; generating → needs the dashboard key (`_paired`).
- Card: collapsed `<details>` "**Arbiter's notes** — <one-line summary>"; each item = plain explanation + small grey "Arbiter wrote: …". Open state survives refresh (`NOPEN`).
- Tests: `tests/test_arbiter_notes.py` (new).

---

## 4. Order summary (under the inputs)

One font throughout (not the old monospace `.yours` box), amounts always 2 dp + currency (`money()` helper). For a buy:
1. **Headline:** "Buy **28 DBK** at up to **31.70 EUR** a share — **887.60 EUR** (≈ 993.49 USD) plus 5.00 USD fee, 1% of the account's cash".
2. **Three tiles:** Most you can lose (−, %, at stop, ≈ account ccy) · You could make (+, %, at target) · Reward : risk ("1.5 : 1" + one-line verdict).
3. **Price line:** stop-loss · your limit (or "you buy") · now · take-profit on one scale; red zone stop→entry, green entry→target; orange "now"; **dashed purple 2-year high/low** markers when within ±25% of the trade's span (labels above the line so they don't collide).
4. **Range rows:** **Today** (day low–high, "now X% of the way up / below the low") and **2 years** (low (Mon YYYY) – high (Mon YYYY), stop→target boxed, "take-profit above the 2-year high" warning).
   Backend: `_range(chart)` + `out["range2y"] = _range(market.chart(sym, "2y", "1wk"))` in `_override_context` (wrapped, optional). Test `test_two_year_range_from_weekly_bars` (`tests/test_override.py`).
5. **Status line:** "● Fills straight away…" / "● Waiting: DBK is at 33.48 EUR and must fall 5.3% to 31.70 EUR before it buys. If it doesn't by the close, the order is cancelled."
6. Account + trailing line. **Claire:** for venue trailing stops say "trailing stop X% — sells automatically at Saxo" (Claire's existing card text already explains this; keep its meaning).
7. Problems as red rows (no stop, stop/target wrong side, Saxo 25%, over cash, manual limit >25% from live).

---

## 5. Orders page (`web/orders.html`, `app/dashboards.py _orders`)
- One card per stock (header: ticker, exchange, last price, accounts, counts), rows grouped: **Orders waiting to fill** (each followed by its stop/target), **Protecting your shares** (working legs, stop first), **Price triggers**. Summary line at top; fired/expired triggers collapsed at the bottom. Same columns everywhere: Type · Shares · Price · Distance · Value/outcome · Account · Status · actions.
- Leg status: **attached** (Saxo leg of an unfilled entry; outcome vs the limit), **working**, **with the order** (queued entry's stop/target). **Updated 2026-11-11:** "not at the broker" is gone — on SGX/built-in sims the leg is **held by Ronathan's sim** (§12).
- Backend: `r["legs_at_broker"]` per row (Saxo, except `SIM_LOCAL_SUFFIXES`). Test line added to `test_orders_page_lists_queued_orders_legs_and_triggers`.
- **Claire:** also show `TrailingStopIfTraded` legs as "🛑 Trailing stop (trails X)".

---

## 6. Watchlist search fallback (`app/tools/market.py`, `saxo_chart.py`, `tradingview.py`)
- `saxo_chart.search(q)`: `/ref/v1/instruments` (Stock, Etf) → Yahoo-style symbols via a MIC→(desk exchange, suffix) map; only desk markets.
- `tradingview.search(q)`: `symbol-search.tradingview.com/symbol_search/v3/` (Origin/Referer tradingview.com), desk markets only (EURONEXT+FR → PARIS, HKEX zero-pad to 4).
- `combined_search(saxo_search)`: Saxo first (`at_saxo: true`), then TradingView extras (`at_saxo: false`); one failing is fine, both failing raises. `Market(search_fallback=…)`; chain = Twelve Data → combined → Yahoo.
- `web/watchlist.html`: results show "tradable at Saxo SIM" / "not offered at Saxo SIM — Claire sim only".
- Tests: last three in `tests/test_providers.py`.

## 7. Watchlist "Buy now" trailing field
`web/watchlist.html`: `b-trail` input (default 8, 0.5–90), validated, in the confirm dialog, sent as `suggested.trail_pct` (server already accepts it). Claire wording per §0.

---

## Verification checklist
- [ ] `tests/run.sh` green after each section.
- [ ] Approvals card: default account Saxo SIM; DBK-style buy shows whole shares; amount⇄shares both ways; option 2 recalculates stop/target; option 3 unlocks limit; approve sends `qty` + `levels` (check the `authorize:approve` event payload).
- [ ] A Saxo SIM approval on Claire still produces the **trailing stop** leg at Saxo with the chosen %, and the card/Orders page call it a trailing stop.
- [ ] Orders page groups by stock; queued buy shows its stop "with the order".
- [ ] Arbiter's notes appear (first one may take ~1 min; then cached).
- [ ] Telegram: alert buttons and share-count buttons on a buy card; commands only if a bot path for them exists.
- [ ] A purchase price above the market: summary shows "fills straight away at about …"; a stop above the live price is blocked on the card and refused in execution.
- [ ] Sell forms show the sell summary with profit/loss vs what was paid.
- [ ] Orders → Edit changes the trailing alert; Portfolio → Trailing alert → Edit works.
- [ ] A sim position's stop-loss appears on Orders as "held by Ronathan's sim" (Claire's sim) and fires in a test.
- [ ] Restart only when idle; report anything not done.


---

# Added 2026-11-11

## 8. Wording, everywhere
| Old | New |
|---|---|
| Limit price / limit (buy) | **Purchase price** |
| Limit price / limit (sell) | **Sale price** |
| Stop / stop | **Stop-loss** |
| Target / target | **Take-profit** |
| Trailing floor (the watcher rule) | **Trailing alert** — "alerts you, never sells" |
Applies to the Approvals card, Override, Watchlist forms, Orders page and its editors, Telegram, trigger descriptions. **Claire:** "Trailing stop" wherever the % is a real Saxo trailing stop (§0).

## 9. Shared order panel (`web/orderform.js`, `web/orderform.css` — new) and the fill-price rule
- One file renders the order summary for every form: `buySummary(o)` (§4), `sellSummary(o)` (§10), `ladder()` (the price line; markers draggable when a page passes `{drag: id}` and defines `window.onLadderDrag(id, key, value, done)` — stop-loss, purchase price, take-profit, trailing-alert start), `ranges()` (today / 2 years), `trailLine()`, `money()`, `sharesFor()`, `suggestLevels()`, `fillPrice()`. Add both files to the dashboards `STATIC` allow-list.
- Used by: Approvals card (+ retry cards), Override a PASS (now also sends `qty`, `broker`, `order_type`; `OverrideBody` / `desk.override` accept them as suggestions), Watchlist **Buy now** and **Price trigger** (trailing alert defaults to 8%), the Sell button (§10), Orders → Edit (§11, helpers only).
- **Fill price** (`fillPrice(lim, live, buy)`): a buy whose purchase price is above the market (a sell whose sale price is below it) fills at once at about the market. Shares, cost, and the stop-loss / take-profit checks use that price; the headline says "now at about X (your purchase price Y is above it)" and the status "● Fills straight away…". Approve / Send are blocked up front when a level is on the wrong side. Triggers use min(limit, trigger price).
- **Backstop in execution:** `nodes_execution._levels_vs_live(th, ref_native, inst, held)`, called in `_intended` after `_price_band`, refuses a buy whose stop ≥ live (or target ≤ live) — reversed for shorts, skipped for closing trades — before anything is sent ("…the stop would sell straight away…"); the trade waits under "Approved but not placed". Three tests in `tests/test_execution.py`.

## 10. Sell forms (`web/sell.js` rewritten) and sell summaries
- Watchlist **Sell now**, Portfolio and Saxo accounts **Sell…**: order type ("Sell at the current price — sells now" default / "Set a sale price"), Sale price (locked to the live price in the first), Shares to sell with **All / Half**, note; the order summary; "✓ Check with Saxo" only on Saxo accounts; Send → `/api/manual-trade` with `suggested.order_type`. Blocks more shares than held, part-shares on Saxo, a sale price > 25% from the live price. Portfolio and Saxo pages now load `orderform.js/.css`.
- `sellSummary(o)`: headline ("Sell 13 DBK at no less than 34.50 EUR — you get about 448.50 EUR, 48% of the 27 shares the desk holds"); tiles You get · Profit/Loss on these shares (vs what you paid) · Left after; price line with "you paid" (dashed accent) · sale price · now; status (waiting: must rise X% / sells straight away). Shorts get buy-style tiles (most you can lose, could make, reward : risk) with the stop above.
- Callers pass `avg` (what you paid) and `symbol` (for the 2-year range): Watchlist `h.avg_cost`, Portfolio `lv.avg_cost_native`, Saxo `p.avg_open_price`.
- Backend: `_override_context` returns `avg_cost` per account (native, pence-corrected) for the Approvals sell card and Override.
- Override fix: an exit's fill price was computed as if it were a buy (`mode(id) === 'buy'` now).

## 11. Edit after approval
- **Orders → Edit** (`web/orders.html`): Purchase/Sale price, Stop-loss, Take-profit, Shares, and **Trailing alert %** for buys; live summary (cost at the fill price, most you can lose / could make, `trailLine`); checks against the fill price. Order rows show "trailing alert X%". Backend: `/api/order-control` forwards `trail_pct`; `OrderControlBody.trail_pct`; `order_control.edit(…, trail_pct)` → on a queued order `watcher.set_position_floor` at once, at the broker it rides on the new card as `suggested.trail_pct`. `_orders` rows carry `trail_pct`. Trigger editor: renamed fields + a Trailing alert field for buy triggers. Tests: two in `tests/test_order_control.py`.
  **Claire:** for a Saxo trailing stop, editing the % must move the venue order too (same caveat as §1.4).
- **Portfolio → 🔔 Trailing alert → Edit / Set…**: % input with a live preview ("moves from 101.2 to 106.7 USD — 3% below the best price since you bought (110), 2.6% above now", red if it would fire at once); `POST /api/position-floor {instrument_id, account_id, side, pct}` (paired key; 0.5–90; only a position the desk holds) → `set_position_floor` (high-water mark kept). `portfolio.levels()` adds `alert_pct`, `alert_ref`. Test in `tests/test_protective_legs.py`.

## 12. The built-in sim enforces stops (`app/tools/brokers.py`, `app/custodian.py`, `app/wiring.py`)
Before: the in-process sim only remembered a bracket's levels; nothing sold at a stop.
- `PaperSimBroker(protect=True, store=conn, venue=name)`: a bracket's stop-loss / take-profit become resting orders of their own (returned as `PlacedOrder.legs`, so `submit_order` books them as leg rows — §11a), one-cancels-other, armed only for what the entry filled; `place_oco` (re-protection), `cancel` (an unfilled entry cancels its legs), `related_order_ids`, `resting()`, `list_working()`, and `check_triggers({symbol: price})`: a sell stop fires at/below its price and fills **at the price seen**; a take-profit fills at its own price; the sibling is cancelled. `protect=False` (default) behaves exactly as before.
- `store`: every sim order is kept in desk.db `sim_orders` (id, venue, client_id, book_json, updated_at), so resting stops and client-id dedupe survive a ronathan-api restart.
- `wiring.build_brokers(env, conn, sim_store=False)`: all sims `protect=True`; `sim_store=True` only from `api/server.py` (the process that owns the sims). `SaxoSimWithLocalMarkets` routes `place_oco` / `related_order_ids` / `resting` / `check_triggers` to the local sim and adds its working orders to `working_orders()` (leg drift).
- `Custodian(quote=market.quote)` + duty `_sim_triggers` (before `_poll_fills`): for each working leg on a sim, if its exchange is open and the quote is fresh, `check_triggers`; `_poll_fills` then books the fill like a Saxo leg and syncs the sibling cancelled. Telegram alert "🛑 Stop-loss reached on the built-in sim…". Report key `sim_triggered`.
- Orders page: sim legs say "held by Ronathan's sim … checked against the live price every 5 minutes while the market is open".
- Tests: `tests/test_sim_stops.py` (new, 14).

## 13. London prices in pence
Bars and levels for LSE arrive in pence (Yahoo "GBp", Saxo mislabelled "GBP") while the quote is in pounds. `dashboards.quote_units(d, price, keys)` divides by 100 when levels sit ~100× the quote; applied in `_override_context` (levels, range2y, avg_cost), `/api/range2y`, the Watchlist, and Telegram `_atr14`. Small amounts display as 0.26, not 0.2600.

## 14. Maintenance checks for the new parts (`app/maintenance.py`)
- `_check_telegram`: token / approver configured, token file mode 600, bot heartbeat (`telegram_bot._beat` writes `service_health` `telegram_bot` at most every 10 min or on a state change; > 30 min silent = bad), approval cards not delivered (`telegram_notices` error / stuck sending for a waiting trade).
- `_check_arbiter_notes`: waiting cards > 15 min old without plain-English notes.
- `_check_sim_stops`: working sim legs, and **bad** if the custodian has not run for 15 min (nothing would sell at them).
- Housekeeping: `prune_sim_orders` (finished sim orders whose book order is finished, after 30 days — a resting stop is never removed), `prune_telegram_notices` (closed/error cards after 90 days).
- Tests: five at the end of `tests/test_maintenance.py`.
