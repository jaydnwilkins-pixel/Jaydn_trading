"""Long-only (app/shorts.py): with RONATHAN_ALLOW_SHORTS off, the desk sells
only shares it holds — at the arbiter, on the cards, on price triggers, and
in execute, which every order passes through."""
from decimal import Decimal

import pytest

from app import price_triggers as pt
from app import shorts
from app.graph.nodes_execution import build_execute
from app.graph.pipeline import vet_thesis
from app.graph.state import PipelineState, Thesis
from tests import test_execution as tx
from tests.test_price_triggers import body
from tests.test_protective_legs import World as LegWorld
from tests.test_protective_legs import _desk


def test_the_switch_is_off_unless_set():
    assert not shorts.allowed({})
    assert not shorts.allowed({"RONATHAN_ALLOW_SHORTS": ""})
    assert not shorts.allowed({"RONATHAN_ALLOW_SHORTS": "no"})
    for v in ("1", "yes", "TRUE", "on", " enabled "):
        assert shorts.allowed({"RONATHAN_ALLOW_SHORTS": v})


def _sell(**kw):
    base = dict(ticker="NVDA", direction="sell", conviction=0.7,
                entry_low=99.0, entry_high=101.0, stop_loss=110.0,
                take_profit=90.0, currency="USD")
    base.update(kw)
    return Thesis(**base)


def _state():
    return PipelineState(work_item_id="wi_t", ticker="NVDA",
                         instrument=tx.NVDA)


# ── the arbiter's verdict ───────────────────────────────────────────────────
def test_a_sell_of_a_stock_the_desk_does_not_hold_becomes_a_pass():
    th = vet_thesis(_state(), _sell(), held=lambda i: Decimal(0),
                    allow_shorts=False)
    assert th.direction == "pass"
    assert "would be a short" in th.conditions[-1]
    assert "long-only" in th.conditions[-1]


def test_selling_what_the_desk_holds_is_untouched():
    th = vet_thesis(_state(), _sell(), held=lambda i: Decimal(10),
                    allow_shorts=False)
    assert th.direction == "sell"


def test_shorts_still_pass_through_when_allowed_and_buys_always_do():
    assert vet_thesis(_state(), _sell(), held=lambda i: Decimal(0),
                      allow_shorts=True).direction == "sell"
    buy = Thesis(ticker="NVDA", direction="buy", conviction=0.7,
                 entry_low=99.0, entry_high=101.0, stop_loss=95.0,
                 take_profit=120.0, currency="USD")
    assert vet_thesis(_state(), buy, held=lambda i: Decimal(0),
                      allow_shorts=False).direction == "buy"


# ── execute: the last check ────────────────────────────────────────────────
def _long_only(w):
    w.execute = build_execute(w.repo, {"alpaca": w.broker}, w.market,
                              account_for=lambda b: "acct", clock=w.clock,
                              allow_shorts=False)
    return w


def test_execute_refuses_a_short_when_long_only():
    w = _long_only(tx.World())
    st = w.state(wi="wi_s", direction="sell")
    st = st.model_copy(update={"approval": st.approval.model_copy(
        update={"qty": 5.0})})
    with pytest.raises(ValueError, match="would be a short"):
        w.execute(st)
    assert w.broker.orders == {}


def test_execute_still_sells_what_is_held_when_long_only():
    w = _long_only(tx.World(fill_mode="instant"))
    w.execute(w.state())                        # buy ~10 shares
    w.custodian.run_once()
    st = w.state(wi="wi_exit", direction="sell")
    st = st.model_copy(update={"kind": "sell_review",
                               "approval": st.approval.model_copy(
                                   update={"qty": 10.0})})
    out = w.execute(st)
    assert w.repo.order(out["order_ids"][0])["side"] == "sell"


# ── the cards ──────────────────────────────────────────────────────────────
def _long_only_desk(w):
    desk = _desk(w.conn, w.repo)
    desk.allow_shorts = False
    return desk


def test_a_manual_short_is_refused_when_long_only():
    w = LegWorld()
    desk = _long_only_desk(w)
    with pytest.raises(ValueError, match="would be a short"):
        desk.manual_trade("CSCO", "NASDAQ", "sell", 104.0, stop=110.0,
                          source="portfolio", background=False)


def test_a_manual_exit_still_works_when_long_only():
    w = LegWorld()
    w.buy_with_legs()
    desk = _long_only_desk(w)
    wi = desk.manual_trade("CSCO", "NASDAQ", "sell", 104.0,
                           suggested={"qty": 9, "broker": "saxo"},
                           source="portfolio", background=False)
    assert desk.work_item(wi)["state"] == "awaiting_approval"


# ── price triggers ─────────────────────────────────────────────────────────
def _short_rule(w, env):
    return pt.create(w.conn, body(direction="sell", condition="gte",
                                  trigger_price=110, qty=5, stop=118,
                                  target=100),
                     created_by="t", clock=w.clock, env=env)


def test_a_short_price_trigger_is_refused_when_long_only():
    with pytest.raises(pt.TriggerError, match="would be a short"):
        _short_rule(LegWorld(), {})


def test_a_short_price_trigger_is_allowed_when_shorts_are_on():
    t = _short_rule(LegWorld(), {"RONATHAN_ALLOW_SHORTS": "1"})
    assert t["kind"] == "short"


def test_production_wiring_reads_the_switch(monkeypatch):
    from app import wiring
    seen = {}

    def fake_execute(*a, **k):
        seen["execute"] = k["allow_shorts"]
        return lambda s: {}
    monkeypatch.setattr(wiring, "build_execute", fake_execute)
    monkeypatch.setattr(wiring, "build_brokers", lambda env: {})
    for name in ("default_structured_factory", "build_tool_registry",
                 "make_analyst", "make_debater", "build_record",
                 "account_for_broker", "_recorder_prose"):
        monkeypatch.setattr(wiring, name, lambda *a, **k: None)
    deps = wiring.build_deps(None, None, None, None, env={})
    assert deps.allow_shorts is False and seen["execute"] is False
    deps = wiring.build_deps(None, None, None, None,
                             env={"RONATHAN_ALLOW_SHORTS": "on"})
    assert deps.allow_shorts is True and seen["execute"] is True
