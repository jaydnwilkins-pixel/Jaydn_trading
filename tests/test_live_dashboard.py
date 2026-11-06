"""The live switch on the dashboards (DESIGN.md §22.1): turn on from a
browser on this PC only, turn off from any paired device, limits editable,
metrics for the Orders page. Real HTTP against the stdlib server, offline."""
import json
import threading

import httpx
import pytest

from app import live_trading
from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.dashboards import create_server

T0 = 1_786_456_800
DKEY = "dash-key-123"
ON = {"CLAIRE_LIVE_TRADING": "enabled"}
PHRASE = live_trading.ARM_PHRASE


class NoMarket:
    _stale_keys = set()


def start(env, *, live_account=True):
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    if live_account:
        repo.create_account("saxo-live", "saxo_live", "live", "SGD",
                            fee_model={"type": "flat", "per_trade": "0"},
                            ts=T0, allow_live=True, external_ref="KEY==")
    srv = create_server(conn, repo, NoMarket(), secret="s", dash_key=DKEY,
                        api_base="http://127.0.0.1:9", clock=lambda: T0,
                        port=0, sse_interval=None, env=env)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{srv.server_address[1]}",
                          timeout=5, headers={"X-Dash-Key": DKEY})
    return srv, conn, client


@pytest.fixture
def desk():
    srv, conn, client = start(ON)
    yield srv, conn, client
    srv.shutdown()


def armed(conn):
    return bool(conn.execute("SELECT armed FROM live_trading").fetchone()[0])


def test_status_says_why_it_cannot_be_turned_on():
    for env, acct, why in [({}, True, "CLAIRE_LIVE_TRADING"),
                           (ON, False, "bin/claire live setup")]:
        srv, _, client = start(env, live_account=acct)
        try:
            st = client.get("/api/live-status").json()
            assert not st["can_arm_here"] and why in st["arm_blocked"]
            r = client.post("/api/live-arm", json={"phrase": PHRASE})
            assert r.status_code == 409
        finally:
            srv.shutdown()


def test_turn_on_from_this_pc_with_the_phrase(desk):
    srv, conn, client = desk
    assert client.get("/api/live-status").json()["can_arm_here"]
    # wrong phrase, and no key: refused
    assert client.post("/api/live-arm", json={"phrase": "yes"}).status_code == 409
    assert httpx.post(f"{client.base_url}/api/live-arm",
                      json={"phrase": PHRASE}).status_code == 403
    assert not armed(conn)
    r = client.post("/api/live-arm", json={"phrase": PHRASE})
    assert r.status_code == 200 and r.json()["armed"]
    assert armed(conn)
    ev = conn.execute("SELECT actor, event FROM live_trading_events").fetchall()
    assert [tuple(e) for e in ev] == [("dashboard:this-pc", "armed")]


def test_a_lan_device_can_turn_off_but_never_on(desk):
    srv, conn, client = desk
    srv.RequestHandlerClass._from_this_pc = lambda self: False   # LAN browser
    st = client.get("/api/live-status").json()
    assert not st["can_arm_here"] and "own PC" in st["arm_blocked"]
    r = client.post("/api/live-arm", json={"phrase": PHRASE})
    assert r.status_code == 403 and not armed(conn)
    live_trading.arm(conn, ON, actor="test", phrase=PHRASE, clock=lambda: T0)
    r = client.post("/api/live-disarm", json={})
    assert r.status_code == 200 and not r.json()["armed"] and not armed(conn)


def test_limits_are_editable_and_logged(desk):
    _, conn, client = desk
    r = client.post("/api/live-limits", json={"limits": {
        "max_order_base": 1500, "max_orders_per_day": 3}})
    assert r.status_code == 200
    assert r.json()["limits"] == {"max_order_base": 1500,
                                  "max_orders_per_day": 3,
                                  "max_daily_notional_base": 2000}
    ev = conn.execute("SELECT event, payload FROM live_trading_events"
                      ).fetchone()
    assert ev["event"] == "limits"
    assert json.loads(ev["payload"])["before"]["max_order_base"] == 1000
    for bad in ({"max_order_base": -5}, {"max_orders_per_day": 2.5},
                {"nonsense": 1}, {"max_order_base": True}, {}):
        assert client.post("/api/live-limits",
                           json={"limits": bad}).status_code == 400
    assert httpx.post(f"{client.base_url}/api/live-limits", json={
        "limits": {"max_order_base": 9e9}}).status_code == 403   # unpaired
    assert live_trading.limits(conn)["max_order_base"] == 1500


def test_metrics_track_todays_allowance_and_orders(desk):
    _, conn, client = desk
    repo = Repo(conn)
    repo.add_instrument("SGX:D05", "D05", "SGX", "SGD")
    repo.deposit("saxo-live", to_micro("5000"), ts=T0)
    repo.create_order(id="o1", account_id="saxo-live", instrument_id="SGX:D05",
                      side="buy", qty=to_micro("10"), limit_price=to_micro("40"),
                      status="placed", expires_at=T0 + 3600, ts=T0,
                      fx_rate=to_micro("1"))
    conn.execute("UPDATE orders SET placed_at=? WHERE id='o1'", (T0,))
    live_trading.record_placed(conn, account_id="saxo-live", order_id="o1",
                               notional_base_micro=to_micro("400"),
                               clock=lambda: T0)
    m = client.get("/api/live-status").json()["metrics"]
    a = m["accounts"]["saxo-live"]
    assert (a["orders_today"], a["orders_left"]) == (1, 1)
    assert (a["notional_today"], a["notional_left"]) == (400.0, 1600.0)
    assert a["entries_all_time"] == 1 and a["exits_today"] == 0
    assert [o["id"] for o in m["orders"]] == ["o1"]
