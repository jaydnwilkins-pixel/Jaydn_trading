"""Re-denominating a paper book (USD → SGD): every base amount converts,
derived amounts stay exact, and the books still reconcile."""
import json

import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.redenominate import RedenominateError, redenominate
from app.accounting.repo import Repo

T0 = 1_786_456_800
RATE = to_micro("1.2950")


@pytest.fixture
def book():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("saxo-paper", "saxo", "paper", "USD",
                        fee_model={"type": "pct", "pct": "0.0008", "min": "5"},
                        risk_limits={"max_order_base": 25000,
                                     "max_trades_per_day": 20}, ts=T0)
    repo.create_account("alpaca-paper", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.add_instrument("NASDAQ:DDOG", "DDOG", "NASDAQ", "USD")
    repo.deposit("saxo-paper", to_micro("100000"), ts=T0)
    repo.deposit("alpaca-paper", to_micro("1000"), ts=T0)
    repo.create_order(id="b", account_id="saxo-paper",
                      instrument_id="NASDAQ:DDOG", side="buy",
                      qty=to_micro("10"), limit_price=to_micro("175.71"),
                      status="placed", expires_at=T0 + 3600, ts=T0,
                      fx_rate=to_micro("1"))
    repo.record_fill("b", broker_fill_id="f1", qty=to_micro("10"),
                     price_native=to_micro("175.71"), fx_rate=to_micro("1"),
                     ts=T0)
    repo.create_order(id="s", account_id="saxo-paper",
                      instrument_id="NASDAQ:DDOG", side="sell",
                      qty=to_micro("4"), limit_price=to_micro("180.33"),
                      status="placed", expires_at=T0 + 3600, ts=T0 + 60,
                      fx_rate=to_micro("1"))
    repo.record_fill("s", broker_fill_id="f2", qty=to_micro("4"),
                     price_native=to_micro("180.33"), fx_rate=to_micro("1"),
                     ts=T0 + 60)
    conn.execute("INSERT INTO price_alerts (instrument_id, rule, threshold,"
                 " peak_base, account_id, side) VALUES ('NASDAQ:DDOG',"
                 " 'trail_pct', 8, 188.72, 'saxo-paper', 1)")
    return conn, repo


def test_whole_book_converts_and_still_reconciles(book):
    conn, repo = book
    cash0 = repo.cash_balance("saxo-paper")
    held0, cost0, _ = repo.position("saxo-paper", "NASDAQ:DDOG")
    pl0 = conn.execute("SELECT realized_pl_base FROM lot_closures").fetchone()[0]
    out = redenominate(conn, "saxo-paper", "SGD", RATE, ts=T0 + 120)

    assert repo.account("saxo-paper")["base_currency"] == "SGD"
    repo.assert_invariants("saxo-paper")
    # cash and position cost move by the rate (to a few micro-units)
    assert abs(repo.cash_balance("saxo-paper") - cash0 * 1.295) < 10
    held, cost, _ = repo.position("saxo-paper", "NASDAQ:DDOG")
    assert held == held0 and abs(cost - cost0 * 1.295) < 10
    # derived amounts are exact, not merely close
    for e in conn.execute("SELECT * FROM executions"):
        sign = 1 if e["side"] == "buy" else -1
        assert e["net_base"] == e["gross_base"] + sign * (
            e["commission"] + e["other_fees"])
        cash = {r["kind"]: r["amount_base"] for r in conn.execute(
            "SELECT kind, amount_base FROM cash_transactions WHERE"
            " execution_id=?", (e["id"],))}
        assert cash["commission"] == -e["commission"]
        assert cash.get("trade_buy", 0) == (-e["gross_base"] if sign > 0 else 0)
        assert e["fx_rate"] == RATE                # USD stock → SGD book
    cl = conn.execute("SELECT * FROM lot_closures").fetchone()
    assert cl["realized_pl_base"] == (cl["proceeds_base"] - cl["cost_base"]
                                      - cl["commission_base"])
    assert abs(cl["realized_pl_base"] - pl0 * 1.295) < 10
    # caps, fee minimum and trailing peak are base-currency too
    acct = repo.account("saxo-paper")
    assert json.loads(acct["risk_limits"])["max_order_base"] == 32375
    assert json.loads(acct["risk_limits"])["max_trades_per_day"] == 20
    assert json.loads(acct["fee_model"])["min"] == "6.48"
    peak = conn.execute("SELECT peak_base FROM price_alerts").fetchone()[0]
    assert peak == pytest.approx(188.72 * 1.295)
    assert out["from"] == "USD" and out["to"] == "SGD"


def test_other_accounts_are_untouched(book):
    conn, repo = book
    before = repo.cash_balance("alpaca-paper")
    redenominate(conn, "saxo-paper", "SGD", RATE)
    assert repo.cash_balance("alpaca-paper") == before
    assert repo.account("alpaca-paper")["base_currency"] == "USD"


def test_refusals(book):
    conn, _ = book
    with pytest.raises(RedenominateError, match="already in USD"):
        redenominate(conn, "saxo-paper", "USD", RATE)
    with pytest.raises(RedenominateError, match="no account"):
        redenominate(conn, "nope", "SGD", RATE)
    with pytest.raises(RedenominateError, match="positive"):
        redenominate(conn, "saxo-paper", "SGD", 0)


def test_a_failure_rolls_everything_back(book, monkeypatch):
    conn, repo = book
    cash0 = repo.cash_balance("saxo-paper")

    def boom(self, account_id):
        raise RuntimeError("invariant broke")
    monkeypatch.setattr(Repo, "assert_invariants", boom)
    with pytest.raises(RuntimeError):
        redenominate(conn, "saxo-paper", "SGD", RATE)
    assert repo.cash_balance("saxo-paper") == cash0
    assert repo.account("saxo-paper")["base_currency"] == "USD"
