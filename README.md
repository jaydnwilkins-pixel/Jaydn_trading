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
tests/run.sh          # 74 offline tests, <7s: accounting (exact micro-unit
                      # money), graph structure + interrupt/resume,
                      # authorization (403/409/410), providers, sessions,
                      # executor/custodian/watcher, dashboards HTTP
```

Live smoke tests activate automatically when the services are running.

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

Resetting the book (`bin/ronathan-reset`) is a separate, deliberately harder
to reach command — not part of the daily-use script.

## Deploy

This machine runs Ronathan via a systemd **wrapper** unit, not the
`systemd/ronathan-api.service` / `ronathan-dashboards.service` files in this
repo — those hardcode a `/opt/Ronathan` path that was never actually used
here (an earlier design assumed a dedicated deploy path; the repo just
lives at `/home/jaydn/jaydn_trading` instead), and running them directly
would fight `bin/ronathan stop`'s graceful-drain logic: their `Restart=always`
would just relaunch whatever the script had just told to stop.

Installed unit (`systemd/ronathan.service` — a thin wrapper, not the two
files above):
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
start. `ronathan-custodian.timer` (DB-side safety net if ronathan-api is down)
is a real systemd timer and can still be installed/enabled normally if
recurring jobs need it — the schedules themselves live in desk.db, run by
the scheduler inside ronathan-api, per the **/schedule page**. Market scans
only run against exchanges that are open at fire time.

## Layout

```
app/graph/        pipeline StateGraph, typed state, LLM + execution nodes
app/accounting/   desk.db schema + THE ONLY WRITER (integer micro-unit money)
app/providers/    DB-backed multi-LLM registry, metering, health, seeds
app/tools/        Yahoo market data, search, narrative jail, run_python
                  sandbox, broker adapters (paper sim + MCP configs)
app/api/          ronathan-api :7798 (FastAPI): resume seam, runs, /ask
app/dashboards.py :7797 stdlib server + approval authorization + watcher
app/custodian.py  fills, TTLs, expiry, orphans, broker sync (in-process + timer)
app/maintenance.py daily checks, log rotation, backups, retention + AI note (§23)
app/sessions.py   exchange calendars: is_open / next_open / close_of
web/              dashboard pages (no build step)
systemd/          ronathan-api/ronathan-dashboards/ronathan-custodian unit
                  templates (not what's installed — see "Deploy"), plus
                  the custodian timer and per-region pre-open analysis
voice/            Ronathan's Piper TTS voice + custom-trained openWakeWord
                  detection model (gitignored — real binaries). Shared
                  with Bob's voice daemon via ~/voice symlinks; see
                  jaydn_trading's sibling `bob` repo for the full map.
tests/            six layers per DESIGN.md §18
```

## Current limitations

- Broker adapters run against the in-process paper sim; the MCP configs for
  alpaca/saxo/moomoo are in `app/tools/brokers.py` and activate when
  credentials exist.
- Yahoo endpoints are unofficial: occasional 429s are retried once, then
  surfaced honestly.
