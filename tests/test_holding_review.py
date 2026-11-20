"""The daily review of stocks the desk holds (app/holding_review.py)."""
from app import holding_review as hr
from app import origin, scheduler
from tests import test_execution as tx


def _holding():
    """A World that holds 10 NVDA."""
    w = tx.World(fill_mode="instant")
    w.execute(w.state())
    w.custodian.run_once()
    return w


def _review(w, *, age_hours, state="done", ticker="NVDA"):
    ts = w.clock() - int(age_hours * 3600)
    w.conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " created_at, updated_at) VALUES (?, 'sell_review', ?, ?, ?, ?, ?)",
        (f"wi_sr_{age_hours}_{state}", ticker, state, "t", ts, ts))


def test_a_held_stock_is_due_while_its_market_is_open():
    w = _holding()
    due = hr.due(w.conn, lambda ex: True, clock=w.clock)
    assert [d["instrument_id"] for d in due] == ["NASDAQ:NVDA"]
    assert due[0]["currency"] == "USD"
    assert hr.due(w.conn, lambda ex: False, clock=w.clock) == []


def test_nothing_is_due_when_the_desk_holds_nothing():
    w = tx.World()
    assert hr.due(w.conn, lambda ex: True, clock=w.clock) == []


def test_once_a_day_counting_the_price_watchers_reviews_too():
    w = _holding()
    _review(w, age_hours=3)                     # reviewed this morning
    assert hr.due(w.conn, lambda ex: True, clock=w.clock) == []


def test_due_again_the_next_day():
    w = _holding()
    _review(w, age_hours=25)
    assert len(hr.due(w.conn, lambda ex: True, clock=w.clock)) == 1


def test_never_stacks_on_a_review_still_waiting_for_you():
    w = _holding()
    _review(w, age_hours=30, state="awaiting_approval")
    assert hr.due(w.conn, lambda ex: True, clock=w.clock) == []


def test_a_sold_position_is_not_reviewed():
    w = _holding()
    w.conn.execute("UPDATE lots SET qty_remaining=0")
    assert hr.due(w.conn, lambda ex: True, clock=w.clock) == []


def test_run_once_starts_a_review_with_the_daily_trigger():
    w = _holding()
    calls = []

    def start(row, trigger):
        calls.append((row["ticker"], trigger))
        return "wi_new"
    out = hr.run_once(w.conn, start, lambda ex: True, clock=w.clock)
    assert out == {"started": [{"work_item": "wi_new",
                                "instrument": "NASDAQ:NVDA"}], "failed": []}
    assert calls[0][1].startswith("daily review: ") and "NVDA" in calls[0][1]


def test_a_review_that_fails_to_start_is_reported():
    w = _holding()

    def start(row, trigger):
        raise RuntimeError("api down")
    out = hr.run_once(w.conn, start, lambda ex: True, clock=w.clock)
    assert out["started"] == []
    assert out["failed"] == [{"instrument": "NASDAQ:NVDA",
                              "error": "api down"}]


def test_max_runs_caps_one_tick():
    w = _holding()
    out = hr.run_once(w.conn, lambda r, t: "wi", lambda ex: True,
                      clock=w.clock, max_runs=0)
    assert out == {"started": [], "failed": []}


def test_the_screens_call_it_a_daily_review_not_a_price_alert():
    trig = hr.TRIGGER.format(ticker="NVDA")
    code, _ = origin.classify("sell_review", trig)
    assert code == origin.HOLDING_REVIEW
    code, _ = origin.classify("sell_review", "watcher: trailing alert hit")
    assert code == origin.EXIT_REVIEW


def test_the_job_is_seeded_with_a_valid_schedule():
    w = tx.World()
    scheduler.seed(w.conn)
    row = w.conn.execute("SELECT * FROM schedules WHERE job="
                         "'holding_review'").fetchone()
    assert row is not None and row["enabled"]
    import json
    scheduler.validate_spec(json.loads(row["spec"]))
    assert "holding_review" in scheduler.JOB_INVOKES
