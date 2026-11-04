"""The live-trading switch and the Saxo adapter (DESIGN.md §22) — offline.
Saxo response shapes are taken from real SIM responses (2026-11-03)."""
import json
from decimal import Decimal

import httpx
import pytest

from app import live_trading
from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import LedgerError, Repo
from app.graph.nodes_execution import submit_order
from app.tools.brokers import PlacedOrder
from app.tools.saxo_broker import SaxoBroker, round_price, split_symbol

T0 = 1_786_456_800                       # Tue 2026-08-11 14:00 UTC
ON = {"CLAIRE_LIVE_TRADING": "enabled"}

AAPL_DETAILS = {"Symbol": "AAPL:xnas", "TickSize": None,
                "TickSizeScheme": {"DefaultTickSize": 0.01, "Elements": [
                    {"HighPrice": 0.9999, "TickSize": 0.0001}]},
                "LotSize": None, "LotSizeType": "OddLotsNotAllowed",
                "MinimumLotSize": 1.0, "IsTradable": True,
                "Format": {"Decimals": 2, "OrderDecimals": 2},
                "CurrencyCode": "USD"}


class Venue:
    """Records placements; the adapter under test is never reached when the
    switch refuses."""
    def __init__(self):
        self.placed = []

    def place_bracket(self, **kw):
        self.placed.append(kw)
        return PlacedOrder(f"saxo-{len(self.placed)}", "accepted")


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr("os.environ", {**ON})
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("saxo-live", "saxo_live", "live", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0,
                        allow_live=True, external_ref="KEY==")
    repo.add_instrument("NASDAQ:AAPL", "AAPL", "NASDAQ", "USD")
    repo.deposit("saxo-live", to_micro("5000"), ts=T0)
    return conn, repo


def order(repo, *, qty="2", limit="200", side="buy", oid="o1"):
    repo.create_order(id=oid, account_id="saxo-live",
                      instrument_id="NASDAQ:AAPL", side=side,
                      qty=to_micro(qty), limit_price=to_micro(limit)
                      if limit else None, status="pending_session",
                      expires_at=T0 + 3600, ts=T0, fx_rate=to_micro("1"))
    return oid


def arm(conn):
    live_trading.arm(conn, ON, actor="test", phrase=live_trading.ARM_PHRASE,
                     clock=lambda: T0)


# ── lock 1 + 2: off by default, armed only deliberately ─────────────────
def test_off_by_default(world):
    conn, _ = world
    st = live_trading.status(conn, {})
    assert st["armed"] is False and st["enabled_in_env"] is False
    assert "OFF" in st["summary"]


def test_live_account_needs_explicit_setup():
    conn = db.connect(":memory:")
    db.init(conn)
    with pytest.raises(LedgerError, match="unrepresentable"):
        Repo(conn).create_account("x", "saxo_live", "live", "USD",
                                  fee_model={}, ts=T0)


def test_arming_needs_env_flag_phrase_and_account(world):
    conn, _ = world
    with pytest.raises(live_trading.LiveTradingBlocked, match="not set"):
        live_trading.arm(conn, {}, actor="t", phrase=live_trading.ARM_PHRASE)
    with pytest.raises(live_trading.LiveTradingBlocked, match="type exactly"):
        live_trading.arm(conn, ON, actor="t", phrase="yes")
    arm(conn)
    assert live_trading.status(conn, ON)["armed"]
    # the env flag is lock 1: armed in the DB means nothing without it
    assert live_trading.status(conn, {})["armed"] is False


def test_disarm_is_always_allowed_and_logged(world):
    conn, _ = world
    arm(conn)
    assert live_trading.disarm(conn, actor="dashboard", reason="test")
    assert not live_trading.disarm(conn, actor="dashboard", reason="again")
    events = [r["event"] for r in conn.execute(
        "SELECT event FROM live_trading_events ORDER BY id")]
    assert events == ["armed", "disarmed"]


# ── the gate in submit_order: the ONE place orders reach a venue ────────
def test_disarmed_live_order_never_reaches_saxo(world):
    conn, repo = world
    venue = Venue()
    placed = submit_order(repo, venue, order(repo), symbol="AAPL", ts=T0)
    assert placed.status == "rejected" and "disarmed" in placed.detail
    assert venue.placed == []
    assert repo.order("o1")["status"] == "rejected"


def test_armed_live_order_places_and_is_counted(world):
    conn, repo = world
    arm(conn)
    venue = Venue()
    placed = submit_order(repo, venue, order(repo), symbol="AAPL", ts=T0)
    assert placed.status == "accepted" and len(venue.placed) == 1
    used = live_trading.usage_today(conn, "saxo-live", clock=lambda: T0)
    assert used == {"orders": 1, "notional_base": 400.0}


@pytest.mark.parametrize("kw,why", [
    (dict(qty="10", limit="200"), "per-order limit"),     # 2000 > 1000
    (dict(limit=None), "limit orders"),
    (dict(side="sell"), "position the desk opened"),       # holds nothing
])
def test_live_limits_refuse(world, kw, why):
    conn, repo = world
    arm(conn)
    venue = Venue()
    placed = submit_order(repo, venue, order(repo, **kw), symbol="AAPL",
                          ts=T0)
    assert placed.status == "rejected" and why in placed.detail
    assert venue.placed == []


def test_daily_order_and_notional_limits(world):
    conn, repo = world
    arm(conn)
    venue = Venue()
    for i in range(2):
        assert submit_order(repo, venue, order(repo, oid=f"o{i}"),
                            symbol="AAPL", ts=T0).status == "accepted"
    third = submit_order(repo, venue, order(repo, oid="o9"), symbol="AAPL",
                         ts=T0)
    assert third.status == "rejected" and "orders today" in third.detail
    # next Singapore day: the count resets
    assert submit_order(repo, venue, order(repo, oid="o10"), symbol="AAPL",
                        ts=T0 + 86400).status == "accepted"
    live_trading.set_limits(conn, {"max_orders_per_day": 10,
                                   "max_daily_notional_base": 500})
    big = submit_order(repo, venue, order(repo, oid="o11", qty="1"),
                       symbol="AAPL", ts=T0 + 86400)
    assert big.status == "rejected" and "daily limit" in big.detail


def test_paper_orders_are_untouched_by_the_switch():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("p", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.add_instrument("NASDAQ:AAPL", "AAPL", "NASDAQ", "USD")
    repo.create_order(id="p1", account_id="p", instrument_id="NASDAQ:AAPL",
                      side="buy", qty=to_micro("1"), limit_price=to_micro("1"),
                      status="pending_session", expires_at=T0 + 60, ts=T0)
    assert submit_order(repo, Venue(), "p1", symbol="AAPL",
                        ts=T0).status == "accepted"


# ── approval: live needs armed + typed confirmation ──────────────────────
def _awaiting(conn):
    conn.execute("INSERT INTO work_items (id, kind, ticker, state, thread_id,"
                 " approval_token, token_state, created_at, updated_at)"
                 " VALUES ('wi','pipeline','AAPL','awaiting_approval','wi',"
                 " 'tok','minted',?,?)", (T0, T0))


def test_live_approval_requires_armed_and_typed_live(world):
    from app import approvals
    conn, _ = world
    _awaiting(conn)
    body = {"work_item_id": "wi", "action": "approve", "token": "tok",
            "broker": "saxo_live", "size_base": 100}
    with pytest.raises(approvals.AuthError, match="not available"):
        approvals.thesis_action(conn, body, lambda b: (200, {}),
                                clock=lambda: T0)
    arm(conn)
    with pytest.raises(approvals.AuthError, match="typing LIVE"):
        approvals.thesis_action(conn, body, lambda b: (200, {}),
                                clock=lambda: T0)
    out = approvals.thesis_action(conn, {**body, "confirm_live": "LIVE"},
                                  lambda b: (200, {}), clock=lambda: T0)
    assert out["ok"]


# ── custodian: doubt about a live order disarms ──────────────────────────
def test_custodian_alarm_disarms(world):
    from app.custodian import Custodian
    conn, repo = world
    arm(conn)
    order(repo)
    c = Custodian(conn, repo, {}, resume_post=lambda b: (200, {}),
                  account_broker=lambda a: "saxo_live",
                  fx_rate_for=lambda i, a: to_micro("1"), clock=lambda: T0)
    report = {"flags": ["RECONCILE order o1: venue does not know it"]}
    c._live_alarm(report)
    assert not live_trading.status(conn, ON)["armed"]
    assert any("DISARMED" in f for f in report["flags"])


# ── migration ────────────────────────────────────────────────────────────
def test_old_databases_can_represent_live(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = db.SCHEMA.read_text().replace("IN ('paper','sim','live'))",
                                        "IN ('paper','sim'))")
    raw = sqlite3.connect(path)
    raw.executescript(old)
    raw.execute("INSERT INTO broker_accounts (id, broker, environment,"
                " base_currency, fee_model, opened_at) VALUES"
                " ('a','alpaca','paper','USD','{}',1)")
    raw.commit()
    raw.close()
    c = db.connect(path)
    db.init(c)
    Repo(c).create_account("l", "saxo_live", "live", "USD", fee_model={},
                           ts=1, allow_live=True)
    assert {r["id"] for r in c.execute("SELECT id FROM broker_accounts")} \
        == {"a", "l"}


# ── the Saxo adapter ─────────────────────────────────────────────────────
def saxo(handler, env="sim", guard=None):
    return SaxoBroker(env, token=lambda: "T", account_key="KEY==",
                      guard=guard,
                      client=httpx.Client(transport=httpx.MockTransport(
                          handler)))


def ref_handler(extra=None):
    def h(req):
        p = req.url.path
        if p.endswith("/ref/v1/instruments/"):
            return httpx.Response(200, json={"Data": [
                {"Symbol": "AAPL:xnas", "Identifier": 211},
                {"Symbol": "AAPL:xmil", "Identifier": 15777171}]})
        if p.endswith("/details/211/Stock"):
            return httpx.Response(200, json=AAPL_DETAILS)
        return extra(req)
    return h


def test_symbol_mapping():
    assert split_symbol("D05.SI") == ("D05", "SI")
    assert split_symbol("AAPL") == ("AAPL", "")
    assert split_symbol("BRK-B") == ("BRK-B", "")


def test_tick_rounding_is_always_in_the_safe_direction():
    assert round_price(AAPL_DETAILS, "201.239", direction="down") == 201.23
    assert round_price(AAPL_DETAILS, "201.231", direction="up") == 201.24
    assert round_price(AAPL_DETAILS, "0.12345", direction="down") == 0.12


def test_place_sends_bracket_with_request_id():
    seen = {}

    def trade(req):
        seen["body"] = json.loads(req.content)
        seen["rid"] = req.headers.get("x-request-id")
        return httpx.Response(200, json={"OrderId": "5001", "Orders": [
            {"OrderId": "5002"}, {"OrderId": "5003"}]})
    b = saxo(ref_handler(trade))
    p = b.place_bracket(symbol="AAPL", side="buy", qty=3, limit=201.239,
                        stop_loss=180.001, take_profit=230.001,
                        client_order_id="ord_x")
    assert p.status == "accepted" and p.broker_order_id == "5001"
    body = seen["body"]
    assert seen["rid"] == "ord_x"
    assert (body["Uic"], body["BuySell"], body["Amount"], body["OrderType"],
            body["OrderPrice"], body["ManualOrder"]) == \
        (211, "Buy", 3, "Limit", 201.23, False)
    legs = {o["OrderType"]: o for o in body["Orders"]}
    assert legs["StopIfTraded"]["OrderPrice"] == 180.0
    assert legs["Limit"]["OrderPrice"] == 230.01
    assert all(o["BuySell"] == "Sell" for o in body["Orders"])


def test_no_exact_instrument_refuses_rather_than_guessing():
    b = saxo(ref_handler())
    p = b.place_bracket(symbol="D05.SI", side="buy", qty=100, limit=40)
    assert p.status == "rejected" and "refusing to guess" in p.detail


def test_saxo_4xx_is_a_definitive_rejection_5xx_is_unknown():
    from app.tools.brokers import BrokerError
    b = saxo(ref_handler(lambda r: httpx.Response(
        400, json={"ErrorInfo": {"ErrorCode": "TooFarFromMarket"}})))
    p = b.place_bracket(symbol="AAPL", side="buy", qty=1, limit=1)
    assert p.status == "rejected" and "TooFarFromMarket" in p.detail
    b = saxo(ref_handler(lambda r: httpx.Response(503)))
    with pytest.raises(BrokerError):
        b.place_bracket(symbol="AAPL", side="buy", qty=1, limit=100)


def test_live_adapter_guard_refuses_without_any_http():
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(500)

    def guard():
        raise live_trading.LiveTradingBlocked("live trading OFF")
    p = saxo(h, env="live", guard=guard).place_bracket(
        symbol="AAPL", side="buy", qty=1, limit=100)
    assert p.status == "rejected" and "OFF" in p.detail and calls == []


def test_fills_are_rebuilt_from_the_activity_log():
    def h(req):
        return httpx.Response(200, json={"Data": [
            {"LogId": "1", "ActivityTime": "2026-11-03T15:00:00Z",
             "Status": "Placed", "SubStatus": "Confirmed"},
            {"LogId": "2", "ActivityTime": "2026-11-03T15:00:01Z",
             "Status": "Fill", "SubStatus": "Confirmed", "FillAmount": 2,
             "FilledAmount": 2, "AveragePrice": 100},
            {"LogId": "3", "ActivityTime": "2026-11-03T15:00:02Z",
             "Status": "FinalFill", "SubStatus": "Confirmed", "FillAmount": 3,
             "FilledAmount": 5, "AveragePrice": 101.2}]})
    st = saxo(h).order_status("5001")
    assert st["status"] == "filled"
    assert [(f["fill_id"], Decimal(f["qty"]), Decimal(f["price"]))
            for f in st["fills"]] == [("saxo-2", 2, 100), ("saxo-3", 3, 102)]


def test_wiring_only_builds_live_adapter_when_enabled(world):
    from app.wiring import build_brokers
    conn, _ = world
    assert "saxo_live" not in build_brokers({}, conn)
    assert build_brokers(ON, conn)["saxo_live"].environment == "live"
    assert isinstance(build_brokers(ON, conn)["saxo"],
                      __import__("app.tools.brokers", fromlist=["x"]
                                 ).PaperSimBroker)


def test_sim_trading_prefers_the_oauth_sign_in():
    from app import wiring
    from app.tools import saxo_live
    env = {"SAXO_SIM_EXECUTION": "enabled", "SAXO_SIM_ACCOUNT_KEY": "K==",
           "SAXO_SIM_TOKEN": "pasted"}
    # no SIM sign-in yet: the pasted 24h token is the fallback
    assert wiring.sim_token_source(env)() == "pasted"
    saxo_live.token_file("sim").write_text("{}")     # temp dir (conftest)
    src = wiring.sim_token_source(env)
    assert src.which == "sim"                        # bound to SIM, always
    b = wiring.build_brokers(env)["saxo"]
    assert b.environment == "sim" and b._token.which == "sim"
    # the LIVE sign-in alone never serves SIM trading
    saxo_live.token_file("sim").unlink()
    saxo_live.token_file("live").write_text("{}")
    assert wiring.sim_token_source({}) is None
    # execution stays off unless explicitly enabled
    assert type(wiring.build_brokers({**env, "SAXO_SIM_EXECUTION": ""})
                ["saxo"]).__name__ == "PaperSimBroker"


def test_live_adapter_is_bound_to_the_live_sign_in(world):
    from app.wiring import build_brokers
    conn, _ = world
    assert build_brokers(ON, conn)["saxo_live"]._token.which == "live"


def test_saxo_orders_are_sized_in_whole_shares():
    """$1,000 of a $103.15 stock is 9.6 shares on the fractional sim but 9
    on Saxo — the 9.6-share order Saxo refused on 2026-11-04."""
    from app.graph.nodes_execution import FRACTION_STEP, _fractional, position_qty
    from app.tools.brokers import PaperSimBroker
    assert _fractional(PaperSimBroker()) and not _fractional(
        SaxoBroker("sim", token=lambda: "t", account_key="k"))
    assert position_qty(Decimal(1000), Decimal("103.15"), 1,
                        FRACTION_STEP) == Decimal("9.6")
    assert position_qty(Decimal(1000), Decimal("103.15"), 1,
                        Decimal(1)) == 9


def test_precheck_sends_the_exact_bracket_and_reports_saxos_verdict():
    seen = {}

    def pre(req):
        assert req.url.path.endswith("/trade/v2/orders/precheck")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "PreCheckResult": "Error",
            "ErrorInfo": {"ErrorCode": "TooFarFromEntryOrder",
                          "Message": "Order price is too far from the entry"},
            "Orders": [{"ErrorInfo": {"ErrorCode": "OrderNotPlaced"}}]})
    b = saxo(ref_handler(pre))
    out = b.precheck(symbol="AAPL", side="buy", qty=66, limit=15,
                     stop_loss=12.57, take_profit=19.85)
    assert out["ok"] is False and "TooFarFromEntryOrder" in out["error"]
    assert "OrderNotPlaced" not in out["error"]         # noise filtered
    assert seen["body"]["Amount"] == 66 and len(seen["body"]["Orders"]) == 2

    def ok(req):
        return httpx.Response(200, json={"PreCheckResult": "Ok",
                                         "EstimatedCashRequired": 990.0,
                                         "EstimatedCashRequiredCurrency": "EUR"})
    good = saxo(ref_handler(ok)).precheck(symbol="AAPL", side="buy", qty=66,
                                          limit=15, stop_loss=14.2)
    assert good["ok"] and good["estimated_cash"] == 990.0


def test_resume_accepts_the_live_broker_name():
    """/internal/resume refused broker 'saxo_live' with a 422 (found
    2026-11-04): a live approval could never have reached execution."""
    from app.api.main import ResumeBody
    b = ResumeBody(work_item_id="w", status="approved", token="t",
                   actor="human", broker="saxo_live", size_base=10)
    assert b.broker == "saxo_live"
