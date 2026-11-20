"""The daily review of stocks the desk holds.

Without it, the only time the desk asked "should we sell this?" was when the
price watcher's trailing alert fired — a position that drifted, or whose
story changed without an 8% fall, was never looked at again. This job runs
hourly; each stock the desk holds gets one exit review per day, in an hour
when its own market is open (so the news is fresh and an exit can be placed
straight away).

The review is the existing sell_review pipeline (news, then the arbiter
weighing it against the analysis that opened the position). A 'pass' means
HOLD and ends there; a 'sell' becomes an approval card like any other trade.
No order is placed without your approval.
"""
import time

TRIGGER = ("daily review: is the reason we hold {ticker} still intact? "
           "(scheduled check, not a price alert)")

# a stock reviewed (by this job OR the price watcher) within this many hours
# is not reviewed again; under a day so the slot can drift with market hours
MIN_HOURS = 20

IN_FLIGHT = ("running", "awaiting_approval", "approved", "executing")


def due(conn, is_open, *, clock=time.time, min_hours=MIN_HOURS) -> list:
    """Stocks the desk holds (long, on any account) whose market is open now
    and that have had no exit review in the last `min_hours` and none still
    in flight. [{instrument_id, ticker, exchange, currency}]"""
    since = int(clock()) - int(min_hours * 3600)
    rows = conn.execute(
        "SELECT i.id, i.ticker, i.exchange, i.currency FROM lots l"
        " JOIN instruments i ON i.id = l.instrument_id"
        " GROUP BY i.id HAVING SUM(l.qty_remaining) > 0"
        " ORDER BY i.exchange, i.ticker").fetchall()
    out = []
    for r in rows:
        if not is_open(r["exchange"]):
            continue
        recent = conn.execute(
            "SELECT 1 FROM work_items WHERE kind='sell_review' AND ticker=?"
            f" AND (created_at >= ? OR state IN ({','.join('?' * len(IN_FLIGHT))}))"
            " LIMIT 1", (r["ticker"], since, *IN_FLIGHT)).fetchone()
        if recent:
            continue
        out.append({"instrument_id": r["id"], "ticker": r["ticker"],
                    "exchange": r["exchange"], "currency": r["currency"]})
    return out


def run_once(conn, start_review, is_open, *, clock=time.time,
             min_hours=MIN_HOURS, max_runs=None) -> dict:
    """Start an exit review for each due stock. `start_review(row, trigger)`
    returns the work item id or raises; one failure never stops the rest."""
    todo = due(conn, is_open, clock=clock, min_hours=min_hours)
    if max_runs is not None:
        todo = todo[:max_runs]
    started, failed = [], []
    for row in todo:
        try:
            wi = start_review(row, TRIGGER.format(ticker=row["ticker"]))
        except Exception as e:                  # noqa: BLE001
            failed.append({"instrument": row["instrument_id"],
                           "error": str(e)[:200] or type(e).__name__})
            continue
        started.append({"work_item": wi, "instrument": row["instrument_id"]})
    return {"started": started, "failed": failed}
