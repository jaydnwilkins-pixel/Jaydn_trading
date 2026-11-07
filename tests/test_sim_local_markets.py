"""saxo-paper: SGX shares are simulated in-process because Saxo's SIM lists
them only as CFDs (2026-11-06, U11 refused "no exact Saxo instrument");
everything else still goes to Saxo SIM."""
from app.tools.brokers import PaperSimBroker, PlacedOrder
from app.tools.saxo_broker import SaxoSimWithLocalMarkets


class FakeSaxo:
    environment = "sim"
    fractional = False

    def __init__(self):
        self.calls = []

    def place_bracket(self, **kw):
        self.calls.append(("place", kw["symbol"]))
        return PlacedOrder("5000123", "accepted")

    def place_oco(self, **kw):
        self.calls.append(("oco", kw["symbol"]))
        return PlacedOrder("5000124", "accepted")

    def precheck(self, **kw):
        self.calls.append(("precheck", kw["symbol"]))
        return {"ok": True}

    def order_status(self, oid):
        self.calls.append(("status", oid))
        return {"status": "placed", "fills": []}

    def cancel(self, oid):
        self.calls.append(("cancel", oid))

    def related_order_ids(self, oid):
        return ["9"]

    def working_orders(self):
        return [{"order_id": "9"}]

    def snapshot(self):
        return {"environment": "sim", "currency": "EUR"}


def router():
    saxo = FakeSaxo()
    return SaxoSimWithLocalMarkets(saxo, PaperSimBroker()), saxo


def test_sgx_fills_locally_and_never_reaches_saxo():
    r, saxo = router()
    placed = r.place_bracket(symbol="U11.SI", side="buy", qty=100,
                             limit=36.84, stop_loss=35.36, take_profit=40.85,
                             client_order_id="o1")
    assert placed.status == "accepted"
    assert placed.broker_order_id.startswith("sim-") and placed.legs == []
    st = r.order_status(placed.broker_order_id)
    assert st["status"] == "filled" and st["fills"][0]["price"] == "36.84"
    r.cancel(placed.broker_order_id)
    assert r.related_order_ids(placed.broker_order_id) == []
    assert saxo.calls == []


def test_everything_else_still_goes_to_saxo():
    r, saxo = router()
    placed = r.place_bracket(symbol="DDOG", side="buy", qty=5, limit=200)
    assert placed.broker_order_id == "5000123"
    r.order_status("5000123")
    r.cancel("5000123")
    assert r.precheck(symbol="7203.T", side="buy", qty=1)["ok"] is True
    assert saxo.calls == [("place", "DDOG"), ("status", "5000123"),
                          ("cancel", "5000123"), ("precheck", "7203.T")]
    assert r.working_orders() == [{"order_id": "9"}]


def test_precheck_and_reprotect_explain_the_local_sim():
    r, saxo = router()
    out = r.precheck(symbol="D05.SI", side="buy", qty=100, limit=40)
    assert out["ok"] is None and "CFD" in out["note"]
    assert r.place_oco(symbol="D05.SI", side="sell", qty=100,
                       stop_loss=38).status == "rejected"
    assert r.place_oco(symbol="AAPL", side="sell", qty=1,
                       stop_loss=1).status == "accepted"


def test_adapter_shape_matches_saxo():
    from app.graph.nodes_execution import _accepts_client_id, _fractional
    r, _ = router()
    assert _accepts_client_id(r) and not _fractional(r)
    assert r.environment == "sim" and "SGX" not in r.venue_label
    assert ".SI" in r.venue_label
    assert r.snapshot()["currency"] == "EUR"


def test_wiring_uses_the_router_for_sim_only(tmp_path, monkeypatch):
    from app import wiring
    from app.tools import saxo_live
    saxo_live.token_file("sim").write_text("{}")     # temp dir (conftest)
    env = {"SAXO_SIM_EXECUTION": "enabled", "SAXO_SIM_ACCOUNT_KEY": "K=="}
    b = wiring.build_brokers(env)["saxo"]
    assert isinstance(b, SaxoSimWithLocalMarkets)
    assert type(b.venue).__name__ == "SaxoBroker"
