"""Orders the venue has finished with must leave the book's working list.

A Saxo day order that expires at the close (or is cancelled on Saxo's own
platform) used to stay `placed` forever: the custodian ignored a terminal
status on entries, and its TTL cancel then failed every run with "order not
found" — so the Orders page showed orders that no longer existed (PG and
XERS, 2026-11-06)."""
import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.custodian import Custodian
from app.tools.brokers import BrokerError
from app.tools.saxo_broker import SaxoBroker

T0 = 1_786_456_800


class Venue:
    """A venue whose order status the test sets; cancel can say 404."""
    def __init__(self):
        self.status, self.cancels, self.cancel_error = {}, [], None

    def order_status(self, oid):
        if oid not in self.status:
            raise BrokerError(f"unknown order {oid}")
        return self.status[oid]

    def cancel(self, oid):
        self.cancels.append(oid)
        if self.cancel_error:
            raise BrokerError(self.cancel_error)


@pytest.fixture
def world():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("saxo-paper", "saxo", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.deposit("saxo-paper", to_micro("10000"), ts=T0)
    repo.add_instrument("NYSE:PG", "PG", "NYSE", "USD")
    venue = Venue()
    clock = {"t": T0}
    c = Custodian(conn, repo, {"saxo": venue}, resume_post=lambda b: (200, {}),
                  account_broker=lambda a: "saxo",
                  fx_rate_for=lambda i, a: to_micro("1"),
                  clock=lambda: clock["t"])
    return conn, repo, venue, c, clock


def working(repo, oid="o1", *, wi="wi_pg", ttl=3600):
    repo.conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " created_at, updated_at) VALUES (?, 'pipeline', 'PG', 'executing',"
        " ?, ?, ?)", (wi, wi, T0, T0))
    repo.create_order(id=oid, work_item_id=wi, account_id="saxo-paper",
                      instrument_id="NYSE:PG", side="buy", qty=to_micro("5"),
                      limit_price=to_micro("162"), status="placed",
                      expires_at=T0 + ttl, ts=T0, fx_rate=to_micro("1"))
    repo.mark_order(oid, "placed", ts=T0, broker_order_id="saxo-" + oid)
    return oid


def state(conn, wi="wi_pg"):
    return conn.execute("SELECT state FROM work_items WHERE id=?",
                        (wi,)).fetchone()[0]


@pytest.mark.parametrize("ended,order_st,item_st", [
    ("DoneForDay", "expired", "expired"),     # day order at the close
    ("Expired", "expired", "expired"),
    ("Cancelled", "cancelled", "cancelled"),  # cancelled on Saxo's platform
])
def test_entry_the_venue_ended_leaves_the_book(world, ended, order_st, item_st):
    conn, repo, venue, c, _ = world
    working(repo)
    venue.status["saxo-o1"] = {"status": "cancelled", "fills": [],
                               "ended": ended}
    r = c.run_once()
    assert repo.order("o1")["status"] == order_st
    assert state(conn) == item_st
    assert not r["flags"] and venue.cancels == []
    assert repo.open_orders() == []                 # off the Orders page


def test_rejected_at_the_venue_fails_the_item(world):
    conn, repo, venue, c, _ = world
    working(repo)
    venue.status["saxo-o1"] = {"status": "rejected", "fills": []}
    c.run_once()
    assert repo.order("o1")["status"] == "rejected"
    assert state(conn) == "failed"


def test_partly_filled_then_expired_keeps_its_shares(world):
    conn, repo, venue, c, _ = world
    working(repo)
    venue.status["saxo-o1"] = {
        "status": "cancelled", "ended": "DoneForDay",
        "fills": [{"fill_id": "f1", "qty": "2", "price": "161.5"}]}
    c.run_once()
    assert repo.order("o1")["status"] == "expired"
    assert repo.position("saxo-paper", "NYSE:PG")[0] == to_micro("2")
    assert state(conn) == "done"                    # the shares it did get


def test_still_working_is_left_alone(world):
    conn, repo, venue, c, _ = world
    working(repo)
    venue.status["saxo-o1"] = {"status": "placed", "fills": []}
    c.run_once()
    assert repo.order("o1")["status"] == "placed"
    assert state(conn) == "executing"


def test_ttl_cancel_says_not_found_but_venue_has_ended_it(world):
    """The live case: poll could not settle it (the venue's status read
    failed that run), then the TTL cancel gets 404 — ask the venue again
    and close, instead of flagging forever."""
    conn, repo, venue, c, clock = world
    working(repo, ttl=60)
    clock["t"] = T0 + 120
    venue.cancel_error = "Saxo DELETE /trade/v2/orders/x/: HTTP 404 OrderNotFound"
    calls = {"n": 0}
    real = venue.order_status

    def flaky(oid):                     # the poll's read fails, expiry's works
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network blip")
        return real(oid)
    venue.order_status = flaky
    venue.status["saxo-o1"] = {"status": "cancelled", "fills": [],
                               "ended": "DoneForDay"}
    r = c.run_once()
    assert repo.order("o1")["status"] == "expired"
    assert not any("expiry error" in f for f in r["flags"])


def test_ttl_cancel_fails_and_venue_still_working_is_flagged(world):
    conn, repo, venue, c, clock = world
    working(repo, ttl=60)
    clock["t"] = T0 + 120
    venue.cancel_error = "HTTP 500"
    venue.status["saxo-o1"] = {"status": "placed", "fills": []}
    r = c.run_once()
    assert repo.order("o1")["status"] == "placed"
    assert any("expiry error" in f for f in r["flags"])


def test_saxo_reports_why_an_order_ended():
    b = SaxoBroker.__new__(SaxoBroker)
    acts = {"Data": [
        {"Status": "Placed", "LogId": 1, "ActivityTime": "2026-11-05T17:57:25Z"},
        {"Status": "DoneForDay", "LogId": 2, "ActivityTime": "2026-11-05T21:00:00Z"}]}
    b._req = lambda m, p: acts
    st = b.order_status("5041548977")
    assert st["status"] == "cancelled" and st["ended"] == "DoneForDay"
