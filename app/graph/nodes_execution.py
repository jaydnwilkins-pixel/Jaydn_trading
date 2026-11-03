"""Deterministic execution nodes (DESIGN.md §11) — no LLM anywhere in this
file. An approved typed thesis contains every number needed; execution is a
function, not a conversation.
"""
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from .. import sessions
from ..accounting.money import from_micro, mul_micro, to_micro
from ..accounting.repo import InsufficientCash
from ..tools.market import yahoo_symbol
# Entry limit vs live quote: beyond this the thesis was priced for something
# else (another stock, a stale quote, pence vs pounds) — refuse, don't trade.
# One band for the gate (pipeline.vet_thesis) and the venue.
from .pipeline import PRICE_BAND


FRACTION_STEP = Decimal("0.1")   # venues that allow fractions: 1/10th share


def position_qty(size_base: Decimal, price_base: Decimal, lot_size: int,
                 step: Decimal = FRACTION_STEP) -> Decimal:
    """Shares purchasable for `size_base`. Board-lot venues (SGX etc.) floor
    to whole lots; elsewhere fractional trading is allowed down to `step`, so
    a small budget still buys something instead of rounding to zero."""
    if price_base <= 0:
        raise ValueError("price must be positive")
    raw = size_base / price_base
    if lot_size > 1:
        lots = int(raw / lot_size)
        return Decimal(lots * lot_size)
    return (raw / step).to_integral_value(rounding="ROUND_FLOOR") * step


# How long a placement claim may sit without a venue acknowledgement before
# the custodian treats its outcome as unknown (the process died mid-call).
SUBMIT_STALE_S = 300


class PlacementUnknown(RuntimeError):
    """A placement was claimed but never acknowledged: the venue may or may
    not hold the order. Never re-place blindly — reconcile first."""


def _accepts_client_id(adapter) -> bool:
    import inspect
    try:
        return "client_order_id" in inspect.signature(
            adapter.place_bracket).parameters
    except (TypeError, ValueError):
        return False


def submit_order(repo, adapter, order_id, *, symbol, ts, claimed=False):
    """Send one persisted order to its venue — the ONLY place that does.

    The order row is written first (M4): a crash between the venue call and
    the bookkeeping used to leave a live order the book knew nothing about,
    or a pending_session order the custodian would place a second time.
    Placement is claimed with a conditional UPDATE (submitted_at IS NULL), so
    of two would-be placers — execute and the custodian, or two custodians —
    exactly one calls the venue. The venue is given the row's
    client_order_id when the adapter accepts one, so it can dedupe a replay.

    Returns the PlacedOrder, or None if someone else holds the claim. A
    venue exception leaves the claim in place (outcome unknown → the
    custodian flags it for reconciliation); a definitive refusal marks the
    row rejected."""
    conn = repo.conn
    if not claimed:
        cur = conn.execute(
            "UPDATE orders SET submitted_at=?, updated_at=? WHERE id=?"
            " AND status='pending_session' AND submitted_at IS NULL",
            (ts, ts, order_id))
        if cur.rowcount != 1:
            return None
    o = repo.order(order_id)
    kw = dict(symbol=symbol, side=o["side"], qty=from_micro(o["qty"]),
              limit=from_micro(o["limit_price"]) if o["limit_price"] else None,
              stop_loss=from_micro(o["stop_loss"]) if o["stop_loss"] else None,
              take_profit=from_micro(o["take_profit"]) if o["take_profit"]
              else None)
    if _accepts_client_id(adapter):
        kw["client_order_id"] = o["client_order_id"] or o["id"]
    placed = adapter.place_bracket(**kw)
    if placed.status == "accepted":
        repo.mark_order(order_id, "placed", ts=ts,
                        broker_order_id=placed.broker_order_id)
    else:
        repo.mark_order(order_id, "rejected", ts=ts)
    return placed


def build_execute(repo, brokers: dict, market, *, account_for,
                  clock=time.time, cal=None):
    """`brokers`: name -> adapter. `account_for(broker_name)` -> account_id."""

    def execute(state):
        ap, th, inst = state.approval, state.thesis, state.instrument
        ts = int(clock())
        now = datetime.fromtimestamp(ts, tz=timezone.utc)
        account_id = account_for(ap.broker)
        symbol = yahoo_symbol(inst.ticker, inst.exchange)
        is_open = sessions.is_open(inst.exchange, now, cal)       # §14a

        # Re-running execute (a retried approval whose first attempt raised,
        # M5) must not order twice: the work item's live order IS the order.
        prior = repo.conn.execute(
            "SELECT * FROM orders WHERE work_item_id=? AND status NOT IN"
            " ('rejected','cancelled','expired') ORDER BY created_at DESC,"
            " rowid DESC LIMIT 1", (state.work_item_id,)).fetchone() \
            if state.work_item_id else None
        if prior is not None:
            if prior["status"] != "pending_session" or not is_open:
                return {"order_ids": [prior["id"]]}
            if prior["submitted_at"] is not None:
                raise PlacementUnknown(
                    f"order {prior['id']} was sent to the venue but never "
                    "acknowledged — reconcile it at the venue before retrying")
            placed = submit_order(repo, brokers[ap.broker], prior["id"],
                                  symbol=symbol, ts=ts)
            if placed is not None and placed.status != "accepted":
                raise RuntimeError(f"broker rejected order: {placed.detail}")
            return {"order_ids": [prior["id"]]}

        qty, ref_native, rate, reserve = _intended(
            ap, th, inst, market, symbol, account_id, repo, state.kind)
        _pre_checks(repo, account_id, inst, th, qty, ref_native, rate,
                    reserve, ts)
        oid = f"ord_{uuid.uuid4().hex[:12]}"
        repo.create_order(
            id=oid, account_id=account_id, instrument_id=inst.id,
            side=th.direction, qty=to_micro(qty),
            limit_price=to_micro(str(th.entry_high)) if th.entry_high else None,
            stop_loss=to_micro(str(th.stop_loss)) if th.stop_loss else None,
            take_profit=to_micro(str(th.take_profit)) if th.take_profit else None,
            # persisted BEFORE the venue call (M4); while the session is
            # closed it simply rests here for the custodian (checks re-run
            # at fill). When open, the row is born already claimed, so the
            # custodian can never race execute to place it.
            status="pending_session", work_item_id=state.work_item_id,
            fx_rate=rate, ref_price=to_micro(str(ref_native))
            if ref_native is not None else None,
            reserved_base=reserve, submitted_at=ts if is_open else None,
            expires_at=int(sessions.close_of(inst.exchange, now,
                                             cal).timestamp()), ts=ts)
        if not is_open:
            _set_trailing_floor(repo, inst, ap, account_id, th.direction)
            return {"order_ids": [oid]}             # custodian places at next open
        placed = submit_order(repo, brokers[ap.broker], oid, symbol=symbol,
                              ts=ts, claimed=True)
        if placed.status != "accepted":
            raise RuntimeError(f"broker rejected order: {placed.detail}")
        _set_trailing_floor(repo, inst, ap, account_id, th.direction)
        return {"order_ids": [oid]}                 # fills arrive ASYNC (§12)

    def _intended(ap, th, inst, market, symbol, account_id, repo_, kind):
        """(qty, reference price native, fx rate ×1e6, cash to reserve).

        Every order is measured in the account's BASE currency (H7), so both
        sides need the live quote and an FX rate. A failed FX lookup raises
        here — it is never replaced by 1.0 (H6): a ¥300,000 order booked as
        $300,000 is worse than no order."""
        acct = repo_.account(account_id)
        base = acct["base_currency"]
        q = market.quote(symbol)
        quote = Decimal(str(q["price"]))
        q_ccy = q.get("currency") or inst.currency
        rate_i = Decimal(market.fx(inst.currency, base))
        rate_q = rate_i if q_ccy == inst.currency \
            else Decimal(market.fx(q_ccy, base))
        if quote <= 0 or rate_i <= 0 or rate_q <= 0:
            raise ValueError(f"no usable price/FX for {inst.id}: quote {quote}"
                             f" {q_ccy}, fx {rate_i}")
        ref_native = quote * rate_q / rate_i       # live price, instrument ccy
        _price_band(th, ref_native, inst)
        rate = to_micro(str(rate_i))
        if th.direction == "sell":
            held, _, _ = repo_.position(account_id, inst.id)
            held_shares = Decimal(held) / 1_000_000
            qty = Decimal(str(ap.qty)) if ap.qty else held_shares
            if kind == "sell_review":
                # an exit review closes a position — it never flips short
                qty = min(qty, held_shares)
            return qty, ref_native, rate, None
        price_base = quote * rate_q
        qty = position_qty(Decimal(str(ap.size_base)), price_base,
                           inst.lot_size)
        # a limit buy fills at or below its limit: reserve the worse of the
        # limit and the quote, so the cash promised covers any fill
        worst = max(price_base, Decimal(str(th.entry_high)) * rate_i
                    if th.entry_high else price_base)
        cost = mul_micro(to_micro(qty), to_micro(str(worst)))
        fee = repo_._fee_for(json.loads(acct["fee_model"]), cost)
        return qty, ref_native, rate, cost + fee

    return execute


def _price_band(th, ref_native, inst):
    """The venue-side twin of the gate's check (pipeline.vet_thesis): the
    limit sent to the venue must be near the price it will meet."""
    lim = th.entry_high
    if lim is None or not ref_native:
        return
    off = abs(Decimal(str(lim)) / ref_native - 1)
    if off > PRICE_BAND:
        raise ValueError(
            f"limit {lim} {inst.currency} is {off * 100:.0f}% from the live "
            f"price {ref_native:.4f} — outside the ±{PRICE_BAND * 100:.0f}% "
            "band; the thesis was priced for something else")


def _set_trailing_floor(repo, inst, ap, account_id, direction):
    """The trailing floor you chose at approval becomes the watcher's rule for
    THIS position (instrument, account, side) — replacing the default 8%.
    One row per position (unique index, M7): the old ON CONFLICT DO NOTHING
    had no constraint to hit, so each approval appended a duplicate.

    peak_base is cleared when the account is flat on that side: a re-bought
    stock must not inherit the old position's high-water mark (it would sit
    far above the new entry and fire at once). The watcher seeds it from the
    new average cost on its next tick and ratchets it from there."""
    if ap.status != "approved":
        return
    side = 1 if direction == "buy" else -1
    held, _, _ = repo.position(account_id, inst.id)
    flat = held == 0 or (held > 0) != (side > 0)
    conn = repo.conn
    if flat:
        conn.execute(
            "UPDATE price_alerts SET peak_base=NULL, fire_count_today=0"
            " WHERE rule='trail_pct' AND instrument_id=? AND account_id=?"
            " AND side=?", (inst.id, account_id, side))
    if not ap.trail_pct:
        return
    cur = conn.execute(
        "UPDATE price_alerts SET threshold=?, armed=1 WHERE rule='trail_pct'"
        " AND instrument_id=? AND account_id=? AND side=?",
        (float(ap.trail_pct), inst.id, account_id, side))
    if cur.rowcount == 0:
        try:
            conn.execute(
                "INSERT INTO price_alerts (instrument_id, account_id, side,"
                " rule, threshold, armed) VALUES (?,?,?,'trail_pct',?,1)",
                (inst.id, account_id, side, float(ap.trail_pct)))
        except sqlite3.IntegrityError:      # lost a race to the watcher's seed
            conn.execute(
                "UPDATE price_alerts SET threshold=?, armed=1"
                " WHERE rule='trail_pct' AND instrument_id=? AND account_id=?"
                " AND side=?", (float(ap.trail_pct), inst.id, account_id,
                                side))


def _pre_checks(repo, account_id, inst, th, qty, ref_native, rate, reserve,
                ts):
    """Everything that must hold BEFORE the venue sees the order (§11):
    a real quantity, cash to cover cost + estimated fee, and risk caps —
    all in the account's base currency."""
    if qty is None or qty <= 0:
        raise ValueError(
            "order quantity is zero — size too small for one lot, or a sell "
            "with no position and no explicit qty")
    limit = to_micro(str(th.entry_high)) if th.entry_high else None
    repo.check_risk(account_id, inst.id, th.direction, to_micro(qty),
                    limit, ts, fx_rate=rate,
                    ref_price=to_micro(str(ref_native))
                    if ref_native is not None else None)
    if th.direction == "buy" and reserve is not None:
        acct = repo.account(account_id)
        # cash already promised to other open buys is not available: two
        # approvals inside one poll interval each used to see the full
        # balance, and the second fill then failed with InsufficientCash
        balance = repo.cash_balance(account_id)
        held_back = repo.reserved_cash(account_id)
        if reserve > balance - held_back:
            raise InsufficientCash(
                f"order needs {from_micro(reserve):.2f} "
                f"{acct['base_currency']} (incl. fee) but {account_id} holds "
                f"{from_micro(balance):.2f}"
                + (f", {from_micro(held_back):.2f} of it committed to open "
                   "orders" if held_back else ""))


def build_record(repo, narratives, *, clock=time.time, prose_fn=None):
    """Terminal bookkeeping: one factual markdown summary per run. State
    transitions live in Desk._sync; money lives in repo — this node only
    writes prose ABOUT the record, never the record. `prose_fn` (the recorder
    agent, §4) adds a short human note when a provider is available; its
    failure never fails a run."""

    def record(state):
        lines = [f"# {state.ticker} — {state.kind} {state.work_item_id}", ""]
        for r in state.reports:
            lines.append(f"- **{r.agent}**: {r.signal} "
                         f"(conviction {r.conviction:.2f}) — {r.summary}")
        if state.thesis:
            t = state.thesis
            lines.append(f"\n**Arbiter**: {t.direction} "
                         f"(conviction {t.conviction:.2f}) "
                         f"entry {t.entry_low}–{t.entry_high} "
                         f"stop {t.stop_loss} target {t.take_profit}")
        if state.approval:
            lines.append(f"**Decision**: {state.approval.status} "
                         f"by {state.approval.actor}")
        if state.order_ids:
            lines.append(f"**Orders**: {', '.join(state.order_ids)}")
        if state.errors:
            lines.append("\n## Errors\n" + "\n".join(f"- {e}" for e in state.errors))
        if prose_fn is not None:
            try:
                lines.append("\n## Recorder note\n" +
                             prose_fn("\n".join(lines)))
            except Exception:                   # noqa: BLE001 — prose is a
                pass                            # nicety, never a dependency
        narratives.write(state.work_item_id, "summary", "\n".join(lines))
        return {}

    return record
