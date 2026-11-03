"""Dashboard-side approval authorization (DESIGN.md §10 steps 2-3).

The dashboards server is the ONLY component that converts a human click into
a resume — and only when the caller PRESENTS the token that was delivered in
the approval card. Localhost access alone approves nothing.
"""
import hmac
import json
import time

import httpx


# A resume claims the token ('resuming') for as long as the graph runs. A
# claim older than this belongs to a process that died mid-resume and may be
# taken over; execution is seconds, so ten minutes is generous.
RESUMING_STALE_S = 600


class AuthError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def default_resume_post(api_base, secret):
    def post(body):
        r = httpx.post(f"{api_base}/internal/resume", json=body,
                       headers={"x-claire-secret": secret}, timeout=30)
        return r.status_code, (r.json() if r.headers.get("content-type", "")
                               .startswith("application/json") else {})
    return post


def thesis_action(conn, payload: dict, resume_post, *, clock=time.time):
    """Authorize (token presented ∧ unexpired ∧ unused ∧ awaiting_approval),
    mark pending_resume, POST the resume, burn on ack. Raises AuthError with
    the HTTP-ish code otherwise."""
    wi = payload.get("work_item_id")
    token = payload.get("token") or ""
    action = payload.get("action")
    if action not in ("approve", "reject"):
        raise AuthError(400, "action must be approve|reject")
    if not token:
        raise AuthError(403, "token required — it is on the approval card")
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wi,)).fetchone()
    if row is None:
        raise AuthError(404, "unknown work item")
    if not row["approval_token"] or not hmac.compare_digest(
            token.encode(), row["approval_token"].encode()):   # constant-time
        raise AuthError(403, "token mismatch")
    if row["token_state"] == "burned":
        raise AuthError(409, "token already used")
    if row["state"] != "awaiting_approval":
        raise AuthError(409, f"item is {row['state']}")
    now = int(clock())
    if row["expires_at"] and now >= row["expires_at"]:
        raise AuthError(410, "approval expired")
    if action == "approve" and not payload.get("broker"):
        raise AuthError(400, "approve needs a broker")
    if action == "approve" and not (payload.get("size_base") or
                                    payload.get("qty")):
        raise AuthError(400, "approve needs size_base (buy) or qty (sell) —"
                             " a positive quantity is mandatory")
    if action == "approve" and payload.get("size_base"):
        acct = conn.execute("SELECT id, base_currency FROM broker_accounts"
                            " WHERE broker=? LIMIT 1",
                            (payload["broker"],)).fetchone()
        if acct is None:
            raise AuthError(400, f"no account for broker "
                                 f"{payload['broker']!r}")
        # what is FREE to spend: cash already promised to open buy orders is
        # not, and approving against it only fails later at the fill
        from .accounting.repo import Repo
        bal = Repo(conn).available_cash(acct["id"])
        if float(payload["size_base"]) > bal / 1e6:
            raise AuthError(400, f"size {payload['size_base']:.2f} exceeds "
                                 f"{acct['id']} cash "
                                 f"{bal / 1e6:.2f} {acct['base_currency']} — "
                                 "top up on the Portfolio page or reduce size")

    # authorize: mark pending_resume (NOT burned) + audit row. Conditional on
    # the state the checks above read, so an item that was resumed, burned or
    # expired in between is refused rather than re-authorized. A re-click on
    # a pending_resume token (claire-api was down) is allowed; the single-use
    # claim that actually matters is taken atomically by /internal/resume.
    cur = conn.execute(
        "UPDATE work_items SET token_state='pending_resume', updated_at=?"
        " WHERE id=? AND approval_token=? AND state='awaiting_approval'"
        " AND token_state IN ('minted','pending_resume')",
        (now, wi, row["approval_token"]))
    if cur.rowcount != 1:
        raise AuthError(409, "token already used or being used")
    conn.execute(
        "INSERT INTO events (item_id, ts, actor, from_state, to_state, payload)"
        " VALUES (?,?,?,?,?,?)",
        (wi, now, "human", "awaiting_approval", f"authorize:{action}",
         json.dumps({k: payload.get(k) for k in
                     ("size_base", "qty", "trail_pct", "broker")})))

    body = {"work_item_id": wi,
            "status": "approved" if action == "approve" else "rejected",
            "token": token, "actor": "human",
            "size_base": payload.get("size_base"), "qty": payload.get("qty"),
            "trail_pct": payload.get("trail_pct"),
            "broker": payload.get("broker")}
    try:
        code, resp = resume_post(body)      # idempotent server-side; retryable
    except Exception as e:                  # noqa: BLE001 — claire-api down
        raise AuthError(502, f"claire-api unreachable ({e}); approval saved, "
                             "will retry") from e
    if code == 200:
        return {"ok": True, **resp}
    # resume refused or claire-api down: token stays pending_resume so a retry
    # (human or custodian) can complete it; surface the reason
    raise AuthError(code if code >= 400 else 502,
                    resp.get("detail", f"resume failed ({code})"))


def retry_pending(conn, resume_post, *, clock=time.time):
    """Crash recovery: re-POST resumes that were authorized but never acked.
    Called by the custodian; idempotent because /internal/resume is."""
    done = []
    now = int(clock())
    # 'resuming' past the stale window = the resuming process died mid-run;
    # without picking those up the approval would be stuck for good
    for row in conn.execute(
            "SELECT id, approval_token FROM work_items"
            " WHERE token_state='pending_resume'"
            " OR (token_state='resuming' AND updated_at < ?)",
            (now - RESUMING_STALE_S,)).fetchall():
        events = conn.execute(
            "SELECT payload, to_state FROM events WHERE item_id=?"
            " AND to_state LIKE 'authorize:%' ORDER BY id DESC LIMIT 1",
            (row["id"],)).fetchone()
        if not events:
            continue
        detail = json.loads(events["payload"] or "{}")
        action = events["to_state"].split(":", 1)[1]
        try:
            code, _ = resume_post({
                "work_item_id": row["id"],
                "status": "approved" if action == "approve" else "rejected",
                "token": row["approval_token"], "actor": "human", **detail})
        except Exception:                   # noqa: BLE001 — api down: next run
            continue
        if code == 200:
            done.append(row["id"])
    return done
