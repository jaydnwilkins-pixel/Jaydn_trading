"""Change a desk account's base currency, converting its whole book at one
rate (2026-11-06: saxo-paper USD → SGD, to match Richard's real Saxo
account; Saxo's own SIM account stays in EUR — that is the venue's money,
not the desk's book).

Every base-currency amount on the account is multiplied by `rate`
(old base → new base, micro-scaled) in ONE transaction:
  cash_transactions, executions (gross, fees, net, fx), orders (reserved
  cash, fx), lots (cost, commission), lot_closures (proceeds, cost,
  commission, realized), price_triggers.amount_base, trailing-alert peaks,
  and the money caps in risk_limits / fee_model.
Amounts that are DEFINED by others are re-derived instead of converted, so
the books stay exact: a fill's net = gross ± fees, its cash rows are −gross
/ +gross and −commission, and realized P/L = proceeds − cost − commission.
History is restated at today's rate (paper money; noted in the audit).

    python -m app.accounting.redenominate saxo-paper SGD [--rate 1.2950]

Run with the desk STOPPED (bin/claire stop) — nothing else may write.
"""
import argparse
import json
import sys
import time
from decimal import Decimal

from .money import MICRO, mul_micro, to_micro


class RedenominateError(Exception):
    pass


def _mul(x, rate):
    return None if x is None else mul_micro(x, rate)


def redenominate(conn, account_id, new_ccy, rate, *, ts=None) -> dict:
    """`rate`: micro-scaled old→new (1.2950 → 1_295_000)."""
    ts = int(ts or time.time())
    acct = conn.execute("SELECT * FROM broker_accounts WHERE id=?",
                        (account_id,)).fetchone()
    if acct is None:
        raise RedenominateError(f"no account {account_id!r}")
    old = acct["base_currency"]
    if old == new_ccy:
        raise RedenominateError(f"{account_id} is already in {new_ccy}")
    if acct["environment"] == "live":
        raise RedenominateError("refusing to re-denominate a live account")
    if rate <= 0:
        raise RedenominateError("rate must be positive")
    cash_before = conn.execute(
        "SELECT COALESCE(SUM(amount_base),0) FROM cash_transactions WHERE"
        " account_id=?", (account_id,)).fetchone()[0]

    conn.execute("BEGIN IMMEDIATE")
    try:
        # executions (via their order's account): convert, re-derive net
        execs = conn.execute(
            "SELECT e.* FROM executions e JOIN orders o ON o.id=e.order_id"
            " WHERE o.account_id=?", (account_id,)).fetchall()
        conv = {}
        for e in execs:
            gross, com = _mul(e["gross_base"], rate), _mul(e["commission"], rate)
            other = _mul(e["other_fees"], rate)
            net = gross + com + other if e["side"] == "buy" else \
                gross - com - other
            conv[e["id"]] = (gross, com)
            conn.execute(
                "UPDATE executions SET gross_base=?, commission=?,"
                " other_fees=?, net_base=?, fx_rate=? WHERE id=?",
                (gross, com, other, net, _mul(e["fx_rate"], rate), e["id"]))
        # cash: rows tied to a fill follow the converted fill exactly
        for c in conn.execute("SELECT * FROM cash_transactions WHERE"
                              " account_id=?", (account_id,)).fetchall():
            ex = conv.get(c["execution_id"])
            if ex and c["kind"] == "trade_buy":
                amt = -ex[0]
            elif ex and c["kind"] == "trade_sell":
                amt = ex[0]
            elif ex and c["kind"] == "commission":
                amt = -ex[1]
            else:
                amt = _mul(c["amount_base"], rate)
            conn.execute("UPDATE cash_transactions SET amount_base=? WHERE"
                         " id=?", (amt, c["id"]))
        for lot in conn.execute("SELECT * FROM lots WHERE account_id=?",
                                (account_id,)).fetchall():
            conn.execute(
                "UPDATE lots SET cost_per_share_base=?, commission_allocated=?"
                " WHERE id=?", (_mul(lot["cost_per_share_base"], rate),
                                _mul(lot["commission_allocated"], rate),
                                lot["id"]))
        for cl in conn.execute(
                "SELECT c.* FROM lot_closures c JOIN lots l ON l.id=c.lot_id"
                " WHERE l.account_id=?", (account_id,)).fetchall():
            p, cost = _mul(cl["proceeds_base"], rate), _mul(cl["cost_base"], rate)
            com = _mul(cl["commission_base"], rate)
            conn.execute(
                "UPDATE lot_closures SET proceeds_base=?, cost_base=?,"
                " commission_base=?, realized_pl_base=? WHERE id=?",
                (p, cost, com, p - cost - com, cl["id"]))
        for o in conn.execute("SELECT id, reserved_base, fx_rate FROM orders"
                              " WHERE account_id=?", (account_id,)).fetchall():
            conn.execute("UPDATE orders SET reserved_base=?, fx_rate=? WHERE"
                         " id=?", (_mul(o["reserved_base"], rate),
                                   _mul(o["fx_rate"], rate), o["id"]))
        for t in conn.execute("SELECT id, amount_base FROM price_triggers"
                              " WHERE account_id=? AND amount_base IS NOT"
                              " NULL", (account_id,)).fetchall():
            conn.execute("UPDATE price_triggers SET amount_base=? WHERE id=?",
                         (_mul(t["amount_base"], rate), t["id"]))
        r = Decimal(rate) / MICRO
        for a in conn.execute("SELECT id, peak_base FROM price_alerts WHERE"
                              " account_id=? AND peak_base IS NOT NULL",
                              (account_id,)).fetchall():
            conn.execute("UPDATE price_alerts SET peak_base=? WHERE id=?",
                         (float(Decimal(str(a["peak_base"])) * r), a["id"]))
        # money caps and fee minimums are in base currency too
        limits = json.loads(acct["risk_limits"] or "{}")
        if limits.get("max_order_base") is not None:
            limits["max_order_base"] = int(round(
                Decimal(str(limits["max_order_base"])) * r))
        fee = json.loads(acct["fee_model"] or "{}")
        for k in ("min", "per_trade"):
            if fee.get(k) not in (None, ""):
                fee[k] = str((Decimal(str(fee[k])) * r).quantize(
                    Decimal("0.01")))
        conn.execute(
            "UPDATE broker_accounts SET base_currency=?, risk_limits=?,"
            " fee_model=? WHERE id=?",
            (new_ccy, json.dumps(limits), json.dumps(fee), account_id))
        cash_after = conn.execute(
            "SELECT COALESCE(SUM(amount_base),0) FROM cash_transactions WHERE"
            " account_id=?", (account_id,)).fetchone()[0]
        from .repo import Repo
        Repo(conn).assert_invariants(account_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"account": account_id, "from": old, "to": new_ccy,
            "rate": str(Decimal(rate) / MICRO), "at": ts,
            "cash_before": cash_before / MICRO, "cash_after": cash_after / MICRO,
            "executions": len(execs), "risk_limits": limits, "fee_model": fee}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("account")
    ap.add_argument("currency")
    ap.add_argument("--rate", help="old→new; default: today's market rate")
    ap.add_argument("--db", default="var/desk.db")
    a = ap.parse_args(argv)
    from . import db
    conn = db.connect(a.db)
    old = conn.execute("SELECT base_currency FROM broker_accounts WHERE id=?",
                       (a.account,)).fetchone()
    if old is None:
        sys.exit(f"no account {a.account}")
    if a.rate:
        rate = to_micro(a.rate)
    else:
        from ..tools.market import Market
        rate = to_micro(str(Market().fx(old["base_currency"], a.currency)))
    print(json.dumps(redenominate(conn, a.account, a.currency, rate),
                     indent=2, default=str))


if __name__ == "__main__":
    main()
