"""The live-trading switch (DESIGN.md §22). OFF by default.

A real-money order can only reach Saxo when ALL of these hold, checked at the
moment of placement — not once at approval:

1. ENABLED — `CLAIRE_LIVE_TRADING=enabled` in etc/claire.env. A file on this
   machine, read at process start: nothing reachable over the network (the
   dashboards, Claire's chat, an agent) can set it.
2. ARMED — the `live_trading` row in desk.db says armed. Arming is done only
   from a terminal on this machine (`bin/claire live arm`, typed
   confirmation). Disarming is one click/command from anywhere, and the desk
   disarms ITSELF when anything about a live order is in doubt.
3. HUMAN-APPROVED — the trade went through the normal approval token gate
   (§10), and the approval explicitly confirmed the live account.
4. WITHIN THE LIVE LIMITS — separate, deliberately small caps (below), in
   the live account's base currency: per-order size, orders per day and
   total notional per day. Limit orders only. Sells only reduce positions
   THE DESK opened (it can never sell, or short, what Richard holds himself).

Any failure refuses the order with a reason. Nothing here can make an order
happen; it can only stop one.
"""
import json
import time

ENV_FLAG = "CLAIRE_LIVE_TRADING"
ARM_PHRASE = "ARM LIVE TRADING"

# Conservative on purpose: the first weeks of live trading are for proving
# the plumbing, not for size. Raise them with `bin/claire live limits`.
DEFAULT_LIMITS = {
    "max_order_base": 1000,           # one order, in the account base ccy
    "max_orders_per_day": 2,          # placed live orders per day (UTC+8 day)
    "max_daily_notional_base": 2000,  # sum of today's live order notionals
}


class LiveTradingBlocked(Exception):
    pass


# ── lock 1: the machine-local flag ───────────────────────────────────────
def env_enabled(env) -> bool:
    return (env.get(ENV_FLAG) or "").strip().lower() == "enabled"


# ── lock 2: armed state in desk.db ───────────────────────────────────────
def _row(conn):
    conn.execute("INSERT OR IGNORE INTO live_trading (id, armed, limits)"
                 " VALUES (1, 0, ?)", (json.dumps(DEFAULT_LIMITS),))
    return conn.execute("SELECT * FROM live_trading WHERE id=1").fetchone()


def limits(conn) -> dict:
    return {**DEFAULT_LIMITS, **json.loads(_row(conn)["limits"] or "{}")}


def live_accounts(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM broker_accounts WHERE environment='live'")]


def status(conn, env, *, clock=time.time) -> dict:
    r = _row(conn)
    accts = live_accounts(conn)
    used = {a["id"]: usage_today(conn, a["id"], clock=clock) for a in accts}
    enabled = env_enabled(env)
    return {
        "enabled_in_env": enabled,
        "armed": bool(r["armed"]) and enabled,
        "armed_at": r["armed_at"], "armed_by": r["armed_by"],
        "disarmed_at": r["disarmed_at"], "disarm_reason": r["disarm_reason"],
        "limits": limits(conn),
        "accounts": [{"id": a["id"], "broker": a["broker"],
                      "base_currency": a["base_currency"],
                      "external_ref": a["external_ref"]} for a in accts],
        "today": used,
        "summary": ("LIVE TRADING ARMED" if bool(r["armed"]) and enabled
                    else "live trading OFF" + (
                        "" if enabled else f" ({ENV_FLAG} not enabled)")),
    }


def arm(conn, env, *, actor, phrase, clock=time.time):
    if not env_enabled(env):
        raise LiveTradingBlocked(f"{ENV_FLAG}=enabled is not set in "
                                 "etc/claire.env — live trading cannot be armed")
    if phrase != ARM_PHRASE:
        raise LiveTradingBlocked(f"type exactly: {ARM_PHRASE}")
    if not live_accounts(conn):
        raise LiveTradingBlocked("no live account — run `bin/claire live "
                                 "setup` first")
    _row(conn)
    conn.execute("UPDATE live_trading SET armed=1, armed_at=?, armed_by=?,"
                 " disarm_reason=NULL WHERE id=1", (int(clock()), actor))
    _event(conn, "armed", actor, clock)


def disarm(conn, *, actor, reason, clock=time.time):
    """Always allowed, from anywhere, idempotent. Stops NEW live orders; the
    orders already at Saxo keep their stop-loss/take-profit legs."""
    r = _row(conn)
    if r["armed"]:
        conn.execute("UPDATE live_trading SET armed=0, disarmed_at=?,"
                     " disarm_reason=? WHERE id=1",
                     (int(clock()), f"{actor}: {reason}"[:300]))
        _event(conn, "disarmed", actor, clock, reason=reason)
        return True
    return False


def set_limits(conn, new: dict):
    allowed = set(DEFAULT_LIMITS)
    bad = set(new) - allowed
    if bad:
        raise ValueError(f"unknown live limits {sorted(bad)}")
    for k, v in new.items():
        if not isinstance(v, (int, float)) or v <= 0:
            raise ValueError(f"{k} must be a positive number")
    merged = {**limits(conn), **new}
    conn.execute("UPDATE live_trading SET limits=? WHERE id=1",
                 (json.dumps(merged),))
    return merged


def _event(conn, what, actor, clock, **payload):
    conn.execute("INSERT INTO live_trading_events (ts, actor, event, payload)"
                 " VALUES (?,?,?,?)",
                 (int(clock()), actor, what, json.dumps(payload)))


# ── lock 4: live limits ──────────────────────────────────────────────────
def _day_start(ts):
    # Richard trades from Singapore (UTC+8, no DST): a "day" is his day
    return ts - (ts + 8 * 3600) % 86400


def usage_today(conn, account_id, *, clock=time.time) -> dict:
    start = _day_start(int(clock()))
    r = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(notional_base),0) notional"
        " FROM live_order_log WHERE account_id=? AND ts>=?",
        (account_id, start)).fetchone()
    return {"orders": r["n"], "notional_base": r["notional"] / 1e6}


def check_order(conn, env, *, account_id, side, qty_micro, limit_micro,
                notional_base_micro, desk_held_micro, clock=time.time):
    """Raise LiveTradingBlocked unless a live order may be placed NOW."""
    acct = conn.execute("SELECT * FROM broker_accounts WHERE id=?",
                        (account_id,)).fetchone()
    if acct is None or acct["environment"] != "live":
        return                                  # not a live order
    if not env_enabled(env):
        raise LiveTradingBlocked(f"live trading is not enabled ({ENV_FLAG})")
    if not _row(conn)["armed"]:
        raise LiveTradingBlocked("live trading is disarmed")
    if not limit_micro:
        raise LiveTradingBlocked("live orders must be limit orders")
    if side == "sell" and qty_micro > (desk_held_micro or 0):
        raise LiveTradingBlocked(
            "a live sell may only reduce a position the desk opened — it "
            "would sell or short shares the desk does not hold")
    lim = limits(conn)
    ccy = acct["base_currency"]
    notional = notional_base_micro / 1e6
    if notional > lim["max_order_base"]:
        raise LiveTradingBlocked(
            f"order {notional:.2f} {ccy} exceeds the live per-order limit "
            f"{lim['max_order_base']} {ccy}")
    used = usage_today(conn, account_id, clock=clock)
    if used["orders"] + 1 > lim["max_orders_per_day"]:
        raise LiveTradingBlocked(
            f"live limit of {lim['max_orders_per_day']} orders today reached")
    if used["notional_base"] + notional > lim["max_daily_notional_base"]:
        raise LiveTradingBlocked(
            f"would take today's live notional to "
            f"{used['notional_base'] + notional:.2f} {ccy}, over the "
            f"{lim['max_daily_notional_base']} {ccy} daily limit")


def record_placed(conn, *, account_id, order_id, notional_base_micro,
                  clock=time.time):
    conn.execute("INSERT INTO live_order_log (ts, account_id, order_id,"
                 " notional_base) VALUES (?,?,?,?)",
                 (int(clock()), account_id, order_id, notional_base_micro))
