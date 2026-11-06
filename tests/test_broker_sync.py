"""Broker sync status: every account's venue is checked each custodian run,
the time is kept per account, and a failed contact never fakes freshness."""
import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.custodian import Custodian
from app.tools.brokers import PaperSimBroker

T0 = 1_786_456_800


class FlakySaxo:
    """Stands in for SaxoBroker: the sync labels it by class name."""
    environment = "sim"

    def __init__(self):
        self.down = False

    def snapshot(self):
        if self.down:
            raise RuntimeError("HTTP 503")
        return {"cash": 1}

    def order_status(self, oid):
        return {"status": "placed", "fills": []}


FlakySaxo.__name__ = "SaxoBroker"


@pytest.fixture
def world():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    for acct, broker in (("alpaca-paper", "alpaca"), ("saxo-paper", "saxo"),
                         ("moomoo-paper", "moomoo")):
        repo.create_account(acct, broker, "paper", "USD",
                            fee_model={"type": "flat", "per_trade": "0"},
                            ts=T0)
    saxo = FlakySaxo()
    brokers = {"alpaca": PaperSimBroker(), "moomoo": PaperSimBroker(),
               "saxo": saxo}
    clock = {"t": T0}

    def custodian(brokers=brokers):
        return Custodian(conn, repo, brokers,
                         resume_post=lambda b: (200, {}),
                         account_broker=lambda a: a.split("-")[0],
                         fx_rate_for=lambda i, a: to_micro("1"),
                         clock=lambda: clock["t"])
    return conn, repo, saxo, custodian, clock


def rows(conn):
    return {r["account_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM broker_sync")}


def test_every_broker_is_checked_not_just_saxo(world):
    conn, repo, _, custodian, _ = world
    custodian().run_once()
    r = rows(conn)
    assert set(r) == {"alpaca-paper", "saxo-paper", "moomoo-paper"}
    assert r["saxo-paper"]["venue"] == "Saxo sim"
    assert r["alpaca-paper"]["venue"] == "built-in paper sim"
    assert all(x["ok"] == 1 and x["checked_at"] == T0 == x["contact_at"]
               for x in r.values())


def test_counts_the_working_orders_it_checked(world):
    conn, repo, _, custodian, _ = world
    repo.add_instrument("NYSE:PG", "PG", "NYSE", "USD")
    repo.deposit("saxo-paper", to_micro("5000"), ts=T0)
    repo.create_order(id="o1", account_id="saxo-paper", instrument_id="NYSE:PG",
                      side="buy", qty=to_micro("1"), limit_price=to_micro("1"),
                      status="placed", expires_at=T0 + 3600, ts=T0)
    repo.mark_order("o1", "placed", ts=T0, broker_order_id="b1")
    custodian().run_once()
    assert rows(conn)["saxo-paper"]["orders_checked"] == 1
    assert rows(conn)["alpaca-paper"]["orders_checked"] == 0


def test_unreachable_broker_keeps_its_last_good_contact(world):
    conn, _, saxo, custodian, clock = world
    custodian().run_once()
    saxo.down, clock["t"] = True, T0 + 300
    custodian().run_once()
    s = rows(conn)["saxo-paper"]
    assert s["ok"] == 0 and s["checked_at"] == T0 + 300
    assert s["contact_at"] == T0                     # not faked fresh
    assert "cannot reach Saxo sim" in s["error"]
    assert rows(conn)["alpaca-paper"]["contact_at"] == T0 + 300


def test_a_custodian_without_adapters_leaves_the_rows_alone(world):
    """The systemd timer custodian has no broker connections: it must not
    stamp a sync it never did."""
    conn, _, _, custodian, clock = world
    custodian().run_once()
    clock["t"] = T0 + 900
    custodian(brokers={}).run_once()
    assert all(x["checked_at"] == T0 for x in rows(conn).values())
