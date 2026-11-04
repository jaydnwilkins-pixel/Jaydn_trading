"""`bin/claire legs-backfill [--dry-run] [--account ID]` — put on the book the
protective stop/target legs that Saxo holds for brackets placed BEFORE the
desk tracked legs (DESIGN.md §11a). One-time, idempotent, read-only at Saxo
(it lists orders and reads the audit trail; it never places, cancels or
changes one).

A leg is adopted only when every detail matches the desk's entry order (see
protection.backfill); everything else is reported and left alone. Back up
desk.db first — the command prints where it would write and refuses to run
without --dry-run unless a backup directory is given or --yes is passed.
"""
import argparse
import json
import os
import sys
import time

from . import protection, wiring
from .accounting import db
from .accounting.repo import Repo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claire legs-backfill")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be inserted; write nothing")
    ap.add_argument("--account", help="only this desk account (e.g. "
                    "saxo-paper)")
    ap.add_argument("--db", default=os.path.join(ROOT, "var", "desk.db"))
    ap.add_argument("--yes", action="store_true",
                    help="write without asking (you backed up desk.db)")
    args = ap.parse_args(argv)
    from .api.server import load_env
    load_env()
    conn = db.connect(args.db)
    db.init(conn)
    repo = Repo(conn)
    brokers = wiring.build_brokers(os.environ, conn)
    if not args.dry_run and not args.yes:
        if not sys.stdin.isatty():
            sys.exit("refusing to write without --yes (back up desk.db first)")
        if input(f"write leg rows into {args.db}? type yes: ").strip() \
                != "yes":
            sys.exit("nothing written")
    out = []
    for acct in conn.execute("SELECT id, broker FROM broker_accounts"
                             ).fetchall():
        if args.account and acct["id"] != args.account:
            continue
        adapter = brokers.get(acct["broker"])
        if adapter is None or not hasattr(adapter, "working_orders"):
            continue                    # a venue that keeps no leg orders
        try:
            rep = protection.backfill(repo, adapter, acct["id"],
                                      ts=int(time.time()),
                                      dry_run=args.dry_run)
        except Exception as e:          # noqa: BLE001 — report, carry on
            rep = [{"account": acct["id"], "action": "error",
                    "why": str(e)[:300]}]
        out.append({"account": acct["id"], "broker": acct["broker"],
                    "results": rep})
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
