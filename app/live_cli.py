"""`bin/claire live …` — the real-money switch (DESIGN.md §22).

  status              what the switch says, limits, today's usage
  setup [ACCOUNT_KEY] create the saxo-live account from the signed-in Saxo
                      login (bin/saxo login first); opening cash is synced
  arm                 arm live trading — interactive, typed confirmation,
                      terminal on THIS machine only (not offered anywhere else)
  disarm [reason]     stop new live orders immediately
  limits [k=v ...]    show or change the live limits
"""
import json
import os
import sys
import time

from . import live_trading
from .accounting import db
from .accounting.money import to_micro
from .accounting.repo import Repo
from .providers import seeds

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_ID = "saxo-live"


def _conn():
    c = db.connect(os.path.join(ROOT, "var", "desk.db"))
    db.init(c)
    seeds.seed(c)
    return c


def status(conn):
    print(json.dumps(live_trading.status(conn, os.environ), indent=2,
                     default=str))


def setup(conn, argv):
    if not live_trading.env_enabled(os.environ):
        sys.exit(f"{live_trading.ENV_FLAG}=enabled is not set in etc/claire.env")
    if conn.execute("SELECT 1 FROM broker_accounts WHERE id=?",
                    (LIVE_ID,)).fetchone():
        sys.exit(f"{LIVE_ID} already exists")
    from .tools import saxo_live
    accounts = saxo_live._get("accounts", "live").get("Data", [])
    if not accounts:
        sys.exit("Saxo returned no accounts for the live sign-in")
    # which account: the one named on the command line (AccountKey or
    # AccountId), else SAXO_LIVE_ACCOUNT_ID from claire.env, else the only one
    want = (argv[0] if argv else saxo_live.account_id("live")) or None
    chosen = [a for a in accounts if want in (a.get("AccountKey"),
                                              a.get("AccountId"))] \
        if want else (accounts if len(accounts) == 1 else [])
    if len(chosen) != 1:
        print("Choose the live account (AccountId or AccountKey):")
        for a in accounts:
            print(f"  {a.get('AccountId')}  {a.get('AccountKey')}  "
                  f"{a.get('Currency')} {a.get('AccountType', '')}")
        sys.exit(1)
    chosen = chosen[0]
    bal = saxo_live.balance("live")
    ccy = chosen.get("Currency") or bal.get("Currency")
    cash = bal.get("CashBalance") or 0
    repo, ts = Repo(conn), int(time.time())
    repo.create_account(
        LIVE_ID, "saxo_live", "live", ccy,
        # Saxo's equity commission varies by market and tier; a 0.08% /
        # min-5 model is a placeholder estimate — edit fee_model to match
        # your Saxo price list. Real fees are what Saxo charges.
        fee_model={"type": "pct", "pct": "0.0008", "min": "5"},
        risk_limits={"max_order_base": live_trading.DEFAULT_LIMITS[
            "max_order_base"], "max_trades_per_day": live_trading.
            DEFAULT_LIMITS["max_orders_per_day"]},
        ts=ts, allow_live=True, external_ref=chosen["AccountKey"])
    if cash > 0:
        repo.deposit(LIVE_ID, to_micro(str(cash)), ts=ts,
                     note="opening cash synced from Saxo at live setup")
    print(f"Created {LIVE_ID} for Saxo account {chosen.get('AccountId')} "
          f"({ccy}, opening cash {cash}). Live trading is "
          "still DISARMED — `bin/claire live arm` when you are ready, then "
          "restart the desk (bin/claire restart) so it loads the adapter.")
    print("Note: positions you already hold at Saxo are NOT on the desk's "
          "book, so the desk can never sell them.")


def arm(conn):
    if not sys.stdin.isatty():
        sys.exit("arming needs an interactive terminal on this machine")
    st = live_trading.status(conn, os.environ)
    print("You are about to ARM REAL-MONEY TRADING on Saxo.\n")
    print("  accounts:", ", ".join(a["id"] for a in st["accounts"]) or "none")
    print("  limits:  ", json.dumps(st["limits"]))
    print("\nEvery live trade will still need your approval on the dashboard "
          "(typing LIVE), and limit orders only.\n")
    phrase = input(f"Type '{live_trading.ARM_PHRASE}' to continue: ").strip()
    try:
        live_trading.arm(conn, os.environ, actor=f"terminal:{os.getlogin()}",
                         phrase=phrase)
    except live_trading.LiveTradingBlocked as e:
        sys.exit(f"not armed: {e}")
    print("ARMED. Disarm any time: `bin/claire live disarm`, or the button on "
          "the Approvals page.")


def main(argv):
    cmd, rest = (argv[0] if argv else "status"), argv[1:]
    conn = _conn()
    if cmd == "status":
        status(conn)
    elif cmd == "setup":
        setup(conn, rest)
    elif cmd == "arm":
        arm(conn)
    elif cmd == "disarm":
        changed = live_trading.disarm(conn, actor="terminal",
                                      reason=" ".join(rest) or "manual")
        print("disarmed" if changed else "was not armed")
    elif cmd == "limits":
        if rest:
            new = {}
            for kv in rest:
                k, _, v = kv.partition("=")
                new[k] = float(v)
            print(json.dumps(live_trading.set_limits(conn, new), indent=2))
        else:
            print(json.dumps(live_trading.limits(conn), indent=2))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
