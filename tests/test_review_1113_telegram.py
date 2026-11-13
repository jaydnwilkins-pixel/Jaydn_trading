"""The 2026-11-13 code review, Ronathan's Telegram side.

7   a size button carried its POSITION in a row that is worked out again at
    every tap, so it could choose a different size from the one it showed
8   /buy sized its suggested stop from its own ATR, not the pages'
10  telegram_run_notices was created on every poll, and not in schema.sql
"""
from app import telegram_approvals as ta
from app.accounting import db
from app.levels import _levels
from tests.test_telegram_commands import Market, make


def test_a_size_button_names_its_shares():
    assert ta.size_arg(9) == "q9" and ta.size_arg(9.0) == "q9"
    assert ta.size_arg(0.5) == "q0.5" and ta.size_arg(1200000) == "q1200000"
    assert len(ta.cb(123456789, "s", ta.size_arg(123456.7891)).encode()) <= 64


def test_a_tapped_size_is_found_by_its_shares_not_its_place():
    # the card showed [4, 9, 19]; by the tap the row is [3, 8]
    assert ta.pick_size("q9", [4, 9, 19]) == 1
    assert ta.pick_size("q9", [3, 8]) is None       # no longer on offer
    assert ta.pick_size("q9", [9, 19]) == 0         # 9 moved to the front
    assert ta.pick_size("q0.5", [1.0, 0.5]) == 1
    # a card sent before buttons carried their shares: its position
    assert ta.pick_size("1", [4, 9]) == 1
    assert ta.pick_size("5", [4, 9]) is None
    assert ta.pick_size("qx", [4, 9]) is None and ta.pick_size("", [4]) is None


def test_buy_sizes_its_stop_from_the_same_atr_as_the_pages():
    conn = db.connect(":memory:")
    db.init(conn)
    m = Market()
    cmds = make(conn, market=m)
    assert cmds._atr14("CSCO", "NASDAQ", 103.0) == \
        _levels(m.chart("CSCO"))["atr14"]

    class Pence(Market):                            # London bars, in pence
        def chart(self, *a, **k):
            c = super().chart(*a, **k)
            return {k2: [x * 100 for x in v] for k2, v in c.items()}
    pounds = make(conn, market=Pence())._atr14("LGEN", "LSE", 103.0)
    assert abs(pounds - _levels(m.chart("LGEN"))["atr14"]) < 1e-3


def test_run_notices_come_from_the_schema_not_from_every_poll():
    conn = db.connect(":memory:")
    db.init(conn)
    assert db.table_ddl("telegram_run_notices")
    cmds = make(conn)
    seen = []
    conn.set_trace_callback(seen.append)
    assert cmds.finished_runs() == []
    assert not [s for s in seen if "CREATE TABLE" in s.upper()
                and "telegram_run_notices" in s]
