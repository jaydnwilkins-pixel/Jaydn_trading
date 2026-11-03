"""The custodian (DESIGN.md §12): fills, expiry, retries, orphans, snapshots.
Deterministic; cannot approve or execute — /internal/resume accepts only
status:"expired" from actor:"reaper" on this path.
"""
import json
import time
from datetime import datetime, timezone

from . import approvals, sessions
from .accounting.money import to_micro


class Custodian:
    def __init__(self, conn, repo, brokers, *, resume_post, account_broker,
                 fx_rate_for, clock=time.time, cal=None):
        self.conn, self.repo = conn, repo
        self.brokers = brokers                  # broker name -> adapter
        self.resume_post = resume_post
        self.account_broker = account_broker    # account_id -> broker name
        self.fx_rate_for = fx_rate_for          # (instrument_id, account_id) -> micro rate
        self.clock, self.cal = clock, cal

    # each duty runs on its own: one failing duty (or one bad order inside
    # it) used to abort run_once, so expiry, retries, orphan checks and drift
    # all stopped — on every run, for as long as the bad order existed (H8)
    DUTIES = ("_place_pending_session", "_poll_fills", "_expire_orders",
              "_expire_approvals", "_retry_resumes", "_orphans", "_drift",
              "_archive")

    def run_once(self) -> dict:
        report = {"placed": [], "fills": 0, "completed": [], "order_expired": [],
                  "approvals_expired": [], "resume_retried": [], "flags": []}
        for duty in self.DUTIES:
            try:
                getattr(self, duty)(report)
            except Exception as e:              # noqa: BLE001 — net must not die silent
                report["flags"].append(f"custodian {duty.strip('_')} error: {e}")
        ok = "degraded" if report["flags"] else "ok"
        self.conn.execute(
            "INSERT INTO service_health (service, checked_at, ok, detail)"
            " VALUES ('custodian', ?, ?, ?)",
            (int(self.clock()), ok, json.dumps(report, default=str)))
        return report

    def _retry_resumes(self, report):
        report["resume_retried"] = approvals.retry_pending(
            self.conn, self.resume_post, clock=self.clock)

    # ── orders ───────────────────────────────────────────────────────────
    def _adapter(self, account_id):
        """None when this custodian instance has no adapter for the venue —
        the timer-run custodian does DB-only duties; fill polling happens in
        the claire-api process, which owns the (possibly in-memory) venues."""
        return self.brokers.get(self.account_broker(account_id))

    def _place_pending_session(self, report):
        from .graph.nodes_execution import SUBMIT_STALE_S, submit_order
        from .tools.market import yahoo_symbol
        ts = int(self.clock())
        now = datetime.fromtimestamp(ts, tz=timezone.utc)
        for o in self.conn.execute(
                "SELECT o.*, i.ticker, i.exchange FROM orders o"
                " JOIN instruments i ON i.id=o.instrument_id"
                " WHERE o.status='pending_session'").fetchall():
            try:
                if o["submitted_at"] is not None:
                    # claimed but never acknowledged: the placer died mid-call
                    # and the venue may hold it. Placing again could double
                    # the position — a human reconciles (M4).
                    if o["submitted_at"] < ts - SUBMIT_STALE_S:
                        report["flags"].append(
                            f"RECONCILE order {o['id']}: sent to the venue at "
                            f"{o['submitted_at']} but never acknowledged — "
                            "check the venue for client id "
                            f"{o['client_order_id'] or o['id']}")
                    continue
                if not sessions.is_open(o["exchange"], now, self.cal):
                    continue
                adapter = self._adapter(o["account_id"])
                if adapter is None:
                    continue
                placed = submit_order(
                    self.repo, adapter, o["id"],
                    symbol=yahoo_symbol(o["ticker"], o["exchange"]), ts=ts)
                if placed is None:
                    continue                    # another placer holds the claim
                if placed.status == "accepted":
                    report["placed"].append(o["id"])
                else:
                    report["flags"].append(
                        f"order {o['id']} rejected: {placed.detail}")
                    self._close_item(o, "failed", report, reason=(
                        f"venue rejected the queued order: {placed.detail}"))
            except Exception as e:              # noqa: BLE001 — per order
                report["flags"].append(f"order {o['id']} placement error: {e}")

    def _close_item(self, o, to_state, report, *, reason, **payload):
        """Move an order's work item out of `executing` when the order can no
        longer complete it. A rejected pending_session order used to leave
        its item `executing` forever, with no orphan check to notice (M6)."""
        if not o["work_item_id"]:
            return
        wi = self.conn.execute("SELECT state FROM work_items WHERE id=?",
                               (o["work_item_id"],)).fetchone()
        if wi and wi["state"] == "executing":
            self.repo.set_state(o["work_item_id"], to_state, actor="custodian",
                                ts=int(self.clock()),
                                payload={"order": o["id"], "reason": reason,
                                         **payload})

    def _poll_fills(self, report):
        from .tools.brokers import BrokerError
        for o in self.conn.execute(
                "SELECT * FROM orders WHERE status IN"
                " ('placed','partially_filled')").fetchall():
            adapter = self._adapter(o["account_id"])
            if adapter is None or not o["broker_order_id"]:
                continue
            try:
                status = adapter.order_status(o["broker_order_id"])
            except BrokerError as e:
                # the in-memory sim forgets every order on a claire-api
                # restart; a real venue can lose one too. The book cannot know
                # what filled, so it waits for a human rather than guessing.
                report["flags"].append(
                    f"RECONCILE order {o['id']}: venue does not know "
                    f"{o['broker_order_id']} ({e}) — fills unknown")
                continue
            except Exception as e:              # noqa: BLE001 — per order
                report["flags"].append(f"order {o['id']} poll error: {e}")
                continue
            for f in status["fills"]:
                try:
                    # a failed FX lookup raises here and the fill waits for
                    # the next run — it is never booked at 1.0 (H6)
                    ex = self.repo.record_fill(
                        o["id"], broker_fill_id=f["fill_id"],
                        qty=to_micro(f["qty"]),
                        price_native=to_micro(f["price"]),
                        fx_rate=self.fx_rate_for(o["instrument_id"],
                                                 o["account_id"]),
                        intended_price=o["limit_price"],  # → slippage metric
                        ts=int(self.clock()))
                except Exception as e:          # noqa: BLE001 — per fill
                    report["flags"].append(
                        f"FILL NOT BOOKED order {o['id']} fill "
                        f"{f['fill_id']}: {e} — the venue filled it, the book "
                        "has not; reconcile")
                    continue
                if ex:
                    report["fills"] += 1
            self._maybe_complete(o["id"], report)

    def _maybe_complete(self, order_id, report):
        o = self.repo.order(order_id)
        if o["status"] == "filled" and o["work_item_id"]:
            wi = self.conn.execute("SELECT state FROM work_items WHERE id=?",
                                   (o["work_item_id"],)).fetchone()
            if wi and wi["state"] == "executing":
                self.repo.set_state(o["work_item_id"], "done", actor="custodian",
                                    ts=int(self.clock()))
                report["completed"].append(o["work_item_id"])

    def _expire_orders(self, report):
        """An order is marked expired only once it can no longer fill: never
        sent, or cancelled at its venue. The timer custodian has no adapters
        and used to mark live orders expired anyway — then refuse the fills
        that followed, so book and broker diverged (H8). Without an adapter
        it now skips and flags; the in-process custodian does the cancel."""
        now = int(self.clock())
        for o in self.conn.execute(
                "SELECT * FROM orders WHERE status IN"
                " ('placed','partially_filled','pending_session')"
                " AND expires_at < ?", (now,)).fetchall():
            try:
                if o["broker_order_id"]:
                    adapter = self._adapter(o["account_id"])
                    if adapter is None:
                        report["flags"].append(
                            f"order {o['id']} is past its TTL but this "
                            "custodian has no adapter to cancel it at the "
                            "venue — left open")
                        continue
                    adapter.cancel(o["broker_order_id"])
                elif o["submitted_at"] is not None:
                    report["flags"].append(
                        f"RECONCILE order {o['id']}: past its TTL, sent but "
                        "never acknowledged — cannot cancel what the book "
                        "cannot name; left open")
                    continue
                (fills,) = self.conn.execute(
                    "SELECT COUNT(*) FROM executions WHERE order_id=?",
                    (o["id"],)).fetchone()
                self.repo.mark_order(o["id"], "expired", ts=now)
                report["order_expired"].append(o["id"])
                self._close_item(o, "done" if fills else "expired", report,
                                 reason="order TTL", partial_fill=bool(fills))
            except Exception as e:              # noqa: BLE001 — per order
                report["flags"].append(f"order {o['id']} expiry error: {e}")

    # ── approvals ────────────────────────────────────────────────────────
    APPROVED_TTL_S = 3600

    def _expire_approvals(self, report):
        now = int(self.clock())
        for r in self.conn.execute(
                "SELECT id, approval_token FROM work_items"
                " WHERE state='awaiting_approval' AND expires_at < ?",
                (now,)).fetchall():
            try:
                code, _ = self.resume_post({
                    "work_item_id": r["id"], "status": "expired",
                    "actor": "reaper", "token": r["approval_token"]})
            except Exception as e:              # noqa: BLE001 — api down
                code = f"unreachable ({e})"
            if code == 200:
                report["approvals_expired"].append(r["id"])
            else:
                report["flags"].append(f"expiry resume failed for {r['id']}: {code}")
        # DESIGN §12: `approved` with no order past 60 min → expired, token
        # burned. An approval whose execution kept failing used to sit
        # `approved` forever, retried on every run (M5). Measured from the
        # approval event, not updated_at, which every retry bumps.
        for r in self.conn.execute(
                "SELECT w.id FROM work_items w WHERE w.state='approved'"
                " AND NOT EXISTS (SELECT 1 FROM orders o WHERE"
                "  o.work_item_id=w.id AND o.status NOT IN"
                "  ('rejected','cancelled','expired'))"
                " AND COALESCE((SELECT MAX(e.ts) FROM events e WHERE"
                "  e.item_id=w.id AND e.to_state='approved'), w.updated_at)"
                " < ?", (now - self.APPROVED_TTL_S,)).fetchall():
            cur = self.conn.execute(
                "UPDATE work_items SET state='expired', token_state='burned',"
                " updated_at=? WHERE id=? AND state='approved'"
                " AND COALESCE(token_state,'') != 'resuming'", (now, r["id"]))
            if cur.rowcount == 1:
                self.repo._event(r["id"], now, "custodian", "approved",
                                 "expired",
                                 {"reason": "approved but no order after "
                                            f"{self.APPROVED_TTL_S // 60} min"})
                report["approvals_expired"].append(r["id"])

    # ── orphans & guards ─────────────────────────────────────────────────
    def _orphans(self, report):
        now = int(self.clock())
        for r in self.conn.execute(
                "SELECT id FROM work_items WHERE state='running'"
                " AND updated_at < ?", (now - 7200,)):
            report["flags"].append(f"work item {r['id']} running > 2h")
        for r in self.conn.execute(
                "SELECT id FROM work_items WHERE state='approved'"
                " AND updated_at < ?", (now - 600,)):
            report["flags"].append(f"approved item {r['id']} has no order after 10m")
        # executing with nothing left that could complete it (M6): every
        # order terminal and none filled — the item would never move again
        for r in self.conn.execute(
                "SELECT w.id FROM work_items w WHERE w.state='executing'"
                " AND NOT EXISTS (SELECT 1 FROM orders o WHERE"
                "  o.work_item_id=w.id AND o.status IN"
                "  ('pending_session','placed','partially_filled','filled'))"
                " AND w.updated_at < ?", (now - 600,)):
            report["flags"].append(
                f"executing item {r['id']} has no live or filled order")
        for r in self.conn.execute(
                "SELECT l.id FROM lots l WHERE l.qty_remaining < 0"):
            report["flags"].append(f"lot {r['id']} negative remainder")
        # UNPROTECTED: an open long position whose opening order carried no
        # stop — the watcher is its only safety net, and you should know (§11)
        for r in self.conn.execute(
                "SELECT DISTINCT l.instrument_id FROM lots l"
                " JOIN executions e ON e.id = l.open_execution_id"
                " LEFT JOIN orders o ON o.id = e.order_id"
                " WHERE l.qty_remaining > 0 AND l.qty_opened > 0"
                " AND (o.stop_loss IS NULL OR o.id IS NULL)"):
            report["flags"].append(
                f"UNPROTECTED position {r['instrument_id']} — no resting stop"
                " at the venue; watcher alerts are the only backstop")

    def _drift(self, report):
        """Reconcile the latest broker snapshot against the book (§12): the
        broker is presumed right; drift is FLAGGED, never silently fixed.
        Sim snapshots carry no cash/positions and are skipped."""
        for acct in self.conn.execute("SELECT id FROM broker_accounts"):
            snap = self.conn.execute(
                "SELECT * FROM broker_snapshots WHERE account_id=?"
                " ORDER BY taken_at DESC LIMIT 1", (acct["id"],)).fetchone()
            if snap is None:
                continue
            if snap["cash"] is not None:
                book = self.repo.cash_balance(acct["id"])
                if abs(snap["cash"] - book) > 1_000_000:      # > $1 drift
                    report["flags"].append(
                        f"CASH DRIFT {acct['id']}: broker reports "
                        f"{snap['cash'] / 1e6:.2f}, book says {book / 1e6:.2f}")
            positions = {}
            try:
                positions = (json.loads(snap["positions_json"] or "{}")
                             .get("positions") or {})
            except (TypeError, ValueError):
                pass
            for inst_id, broker_qty in positions.items():
                held, _, _ = self.repo.position(acct["id"], inst_id)
                if abs(held / 1e6 - float(broker_qty)) > 1e-6:
                    report["flags"].append(
                        f"POSITION DRIFT {acct['id']} {inst_id}: broker "
                        f"{broker_qty}, book {held / 1e6}")

    def _archive(self, report=None) -> int:
        """Terminal items > 30 days old get archived (hidden from the default
        run list; nothing is deleted — the audit trail is forever)."""
        now = int(self.clock())
        cur = self.conn.execute(
            "UPDATE work_items SET archived_at=? WHERE archived_at IS NULL"
            " AND state IN ('done','rejected','expired','failed')"
            " AND updated_at < ?", (now, now - 30 * 86400))
        if report is not None:
            report["archived"] = cur.rowcount
        return cur.rowcount

    def snapshot_brokers(self):
        ts = int(self.clock())
        for acct in self.conn.execute("SELECT id, broker FROM broker_accounts"):
            adapter = self.brokers.get(acct["broker"])
            if adapter is None:
                continue
            try:
                snap = adapter.snapshot()
            except Exception as e:              # noqa: BLE001
                snap = {"error": str(e)}
            self.conn.execute(
                "INSERT INTO broker_snapshots (account_id, taken_at, cash,"
                " positions_json) VALUES (?,?,?,?)",
                (acct["id"], ts, None, json.dumps(snap, default=str)))


def _account_broker_from_db(conn):
    def account_broker(account_id):
        row = conn.execute("SELECT broker FROM broker_accounts WHERE id=?",
                           (account_id,)).fetchone()
        return row["broker"] if row else ""
    return account_broker


def main(argv=None):
    """Timer entrypoint (claire-custodian.timer): DB-side duties — approval
    expiry, authorize-crash retries, orphan flags — plus --snapshot for the
    daily reconcile and a nightly VACUUM backup. Fill polling runs inside
    claire-api, which owns the broker adapters."""
    import argparse
    import os

    from . import sessions as sess
    from .accounting import db
    from .accounting.repo import Repo
    from .api.server import ROOT, load_env
    from .approvals import default_resume_post

    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true",
                    help="also VACUUM-backup desk.db")
    args = ap.parse_args(argv)
    load_env()
    var = ROOT / "var"
    conn = db.connect(var / "desk.db")
    db.init(conn)
    repo = Repo(conn)
    resume_post = default_resume_post(
        "http://127.0.0.1:7788", os.environ.get("CLAIRE_INTERNAL_SECRET", ""))
    def no_fx(instrument_id, account_id):
        # this custodian has no adapters, so it never books a fill; if that
        # ever changes, a rate of 1.0 must not be what it books (H6)
        raise RuntimeError("timer custodian has no FX source")

    c = Custodian(conn, repo, {}, resume_post=resume_post,
                  account_broker=_account_broker_from_db(conn),
                  fx_rate_for=no_fx, cal=sess.load(conn) or None)
    report = c.run_once()
    if args.snapshot:
        backup = var / "desk.backup.db"
        backup.unlink(missing_ok=True)
        conn.execute("VACUUM INTO ?", (str(backup),))
        report["backup"] = str(backup)
    print(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
