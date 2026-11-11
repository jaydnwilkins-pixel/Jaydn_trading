# Ronathan — LangGraph Multi-Agent Paper-Trading Desk

An AI trading desk: specialist LLM agents research a stock, a bull and a bear
debate it, an arbiter issues a typed verdict, and **nothing executes without
explicit human approval verified by a single-use token**. Execution and
accounting are deterministic Python — no LLM ever touches money. Paper/SIM
by default; real-money trading on Saxo exists but is OFF behind a four-lock
switch (DESIGN.md §22, `bin/ronathan live`). Saxo how-to — signing in, the
Saxo accounts page, SIM and live trading: [docs/SAXO.md](docs/SAXO.md).

Full specification: [DESIGN.md](DESIGN.md). Build status: **all §20 steps
implemented**. On this machine `RONATHAN_INTERNAL_SECRET` is already set in
`etc/ronathan.env` and Ronathan runs live — this section is for setting up a
*fresh* checkout elsewhere.

**Model billing.** Every agent defaults to the `claude_max` provider: the
Claude Agent SDK signed in with the machine's claude.ai login, so model use
counts against the Claude Max subscription, not API credit
(`app/providers/claude_sdk.py`). It needs no API key; it does need `claude`
to have been logged in once as this user (`claude` → `/login`). LangGraph
still owns orchestration and the approval gate. The `anthropic` (API-key)
provider remains available per agent on the Agents page.

Lives at `/home/jaydn/jaydn_trading` (user `jaydn`), next to Richard's
Claire desk on the same machine; Ronathan uses ports 7798 (API) and 7797
(dashboards) so the two never collide.

## Quick start (development)

```sh
python3.14 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp etc/ronathan.env.example etc/ronathan.env && chmod 600 etc/ronathan.env
#   → set RONATHAN_INTERNAL_SECRET (openssl rand -hex 24); ANTHROPIC_API_KEY
#     only if an agent is moved to the API-billed `anthropic` provider
claude   # once, then /login — the default provider uses this sign-in
./venv/bin/python -m app.api.server        # :7798 graph runtime + chat
./venv/bin/python -m app.dashboards        # :7797 pages + watcher
```

Once `etc/ronathan.env` exists, day-to-day control is `bin/ronathan`, not the
raw commands above — see "Day-to-day control" below.

Open http://127.0.0.1:7797 — Mission Control, Trading (launch runs),
Portfolio, Markets, Agents (per-agent provider/model panel), Providers.

Chat seam (voice/assistants integrate here):
`POST :7798/ask {"text": …}` → NDJSON stream · `GET /status` · `GET /feed`.

## Tests

```sh
tests/run.sh          # about 800 offline tests, ~90 s: accounting (exact
                      # micro-unit money), graph structure + interrupt/
                      # resume, authorization (403/409/410), providers,
                      # sessions, executor/custodian/watcher, protective
                      # legs, the built-in sim's stops, Telegram,
                      # dashboards HTTP
```

`tests/test_smoke.py` runs against the LIVE services whenever they are up
(it sends Ronathan one real chat turn). Leave it out with
`pytest tests --deselect tests/test_smoke.py` to stay offline.

## Day-to-day control

`bin/ronathan` is the actual interface used on this machine — not the raw
`python -m app.api.server` commands above, and not the `systemd/*.service`
files in this repo directly (see "Deploy" below for why).

```sh
bin/ronathan start                 # ronathan-api (:7798) + dashboards (:7797)
bin/ronathan stop [--graceful]     # pause schedules, drain work in flight,
                                 # then stop (default; see bin/ronathan for the
                                 # --kill variant and RONATHAN_DRAIN_TIMEOUT)
bin/ronathan restart [--kill]
bin/ronathan status                # what's running, what's in flight
```

**Telegram.** Ronathan has its own bot. `etc/ronathan.env` sets
`RONATHAN_TELEGRAM_ENV=etc/telegram.env` (gitignored, mode 600):
`TELEGRAM_BOT_TOKEN` from @BotFather, `TELEGRAM_CHAT_ID` = your user id
(message the bot `/id`), `TELEGRAM_POLL=1` so the dashboards process reads
button taps itself (`app/telegram_bot.py`) instead of relying on Bob's relay.
Both files are re-read every pass — no restart needed after editing them.
`bin/saxo test-alert` checks delivery.
From the bot (owner only, private chat) you can also **start** trades:
`/analyze TICKER [EXCHANGE]`, `/buy TICKER [EXCHANGE] [SIZE]`,
`/sell TICKER [all|half|QTY]`, `/positions`, `/orders`, `/floor [TICKER] [PCT]`
(`app/telegram_commands.py`). `/buy … trail=10` sets the trailing alert; the
Telegram card has alert buttons (5/8/12/15% + the suggestion), retries too.
The card offers share counts (sized at the price the order really fills at)
and approves exactly those shares, as the dashboard card does.
Every one ends at the approval gate — the card still needs ✔ (and LIVE for
real money); `/sell` never shorts; an `/orders` cancel takes two taps.
`/floor` changes a held position's trailing alert — an alert that starts a
sell review, never a sale (Portfolio → Trailing alert → Edit does the same).

Resetting the book (`bin/ronathan-reset`) is a separate, deliberately harder
to reach command — not part of the daily-use script.

## Deploy

This machine runs Ronathan via one systemd **wrapper** unit that calls
`bin/ronathan`. (Per-service units with `Restart=always` would fight
`bin/ronathan stop`'s graceful drain by relaunching what the script had just
stopped; the templates for them, which pointed at a `/opt/Ronathan` path that
never existed, were removed on 2026-11-11.)

Installed unit (`systemd/ronathan.service`):
```ini
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/jaydn/jaydn_trading
ExecStart=/home/jaydn/jaydn_trading/bin/ronathan start
ExecStop=/home/jaydn/jaydn_trading/bin/ronathan stop --graceful
TimeoutStopSec=320
```
```sh
cp systemd/ronathan.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ronathan.service
```
Gives reboot persistence, not crash recovery — if `ronathan-api` or
`ronathan-dashboards` dies mid-session (not via a reboot), systemd won't
notice or restart it, since the wrapper already exited after the one-shot
start. Recurring jobs (custodian, price triggers, market scans, reconcile,
maintenance) are not systemd timers: the schedules live in desk.db and are
run by the scheduler inside ronathan-api, per the **Schedule page**. Market
scans only run against exchanges that are open at fire time.

Logs: `var/log/api.log` and `var/log/dashboards.log`, one timestamped line
per event (`app/logsetup.py`); `RONATHAN_LOG_LEVEL` in `etc/ronathan.env` sets
the level (default INFO). The maintainer rotates them.

## Layout

```
app/graph/        pipeline StateGraph, typed state, LLM + execution nodes
app/accounting/   desk.db schema + THE ONLY WRITER (integer micro-unit money)
app/providers/    DB-backed multi-LLM registry, metering, health, seeds
app/tools/        market data (Twelve Data > TradingView > Yahoo), news
                  search, narrative jail, run_python sandbox, broker
                  adapters (in-process paper sim, Saxo)
app/api/          ronathan-api :7798 (FastAPI): resume seam, runs, /ask
app/dashboards.py :7797 stdlib server + approval authorization + watcher
app/custodian.py  fills, TTLs, expiry, orphans, ledger check, broker sync
app/logsetup.py   logging for both services
app/maintenance.py daily checks, log rotation, backups, retention + AI note (§23)
app/sessions.py   exchange calendars: is_open / next_open / close_of
web/              dashboard pages (no build step)
systemd/          ronathan.service — the wrapper unit (see "Deploy")
docs/             SAXO.md; past reviews
voice/            Ronathan's Piper TTS voice + custom-trained openWakeWord
                  detection model (gitignored — real binaries). Shared
                  with Bob's voice daemon via ~/voice symlinks; see
                  jaydn_trading's sibling `bob` repo for the full map.
tests/            six layers per DESIGN.md §18
```

## Current limitations

- Saxo SIM is a real venue (`SAXO_SIM_EXECUTION=enabled`); the alpaca and
  moomoo accounts, and SGX on saxo-paper, run on Ronathan's built-in paper
  sim. The sim holds each buy's stop-loss / take-profit as resting orders
  (kept in desk.db `sim_orders`) and the custodian fills them against the
  live price every 5 minutes while the market is open — a fast fall can
  sell below the stop. The `mcp_servers` table and `mcp/saxo_mcp.py` are
  not used by the desk itself.
- Without a `TAVILY_API_KEY` the agents' web search is a news-headline
  search (Bing News / Google News RSS), not a general web search.
- Yahoo is the last-resort data source and rate-limits this machine; a
  refusal is surfaced, never papered over.
- Holiday calendars in `app/sessions.py` are hand-maintained per year.
