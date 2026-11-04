"""Scheduler tests — spec validation, next-run math across timezones and
weekends, due-job firing, and the screener's open-market guard."""
import json

import pytest

from app import scheduler
from app.accounting import db

T0 = 1_786_456_800   # Tue 2026-08-11 14:00 UTC


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    scheduler.seed(c)
    return c


def test_seed_is_idempotent(conn):
    scheduler.seed(conn)
    (n,) = conn.execute("SELECT COUNT(*) FROM schedules").fetchone()
    assert n == len(scheduler.DEFAULTS)


def test_interval_next_run():
    spec = {"type": "interval", "minutes": 5}
    assert scheduler.next_run_ts(spec, None, T0) == T0          # fire now
    assert scheduler.next_run_ts(spec, T0, T0 + 10) == T0 + 300


def test_daily_next_run_respects_tz_and_weekend():
    spec = {"type": "daily", "time": "10:00", "tz": "Asia/Singapore",
            "days": [1, 2, 3, 4, 5]}
    # last ran Tue 10:00 SGT (02:00 UTC); next is Wed 10:00 SGT
    tue_10_sgt = T0 - 12 * 3600
    nxt = scheduler.next_run_ts(spec, tue_10_sgt, T0)
    assert nxt == tue_10_sgt + 86400
    # Fri run → next is MONDAY (weekend skipped)
    fri_10_sgt = tue_10_sgt + 3 * 86400
    assert scheduler.next_run_ts(spec, fri_10_sgt, fri_10_sgt + 60) == \
        fri_10_sgt + 3 * 86400


def test_daily_never_backfires_on_boot():
    spec = {"type": "daily", "time": "03:00", "tz": "UTC",
            "days": [1, 2, 3, 4, 5, 6, 7]}
    nxt = scheduler.next_run_ts(spec, None, T0)     # 14:00 UTC, 03:00 passed
    assert nxt > T0                                  # tomorrow, not immediately


def test_validate_spec():
    scheduler.validate_spec({"type": "interval", "minutes": 5})
    with pytest.raises(ValueError):
        scheduler.validate_spec({"type": "interval", "minutes": 0})
    with pytest.raises(ValueError):
        scheduler.validate_spec({"type": "daily", "time": "25:00",
                                 "tz": "UTC", "days": [1]})
    with pytest.raises(Exception):
        scheduler.validate_spec({"type": "daily", "time": "10:00",
                                 "tz": "Mars/Olympus", "days": [1]})


def test_update_merges_and_validates(conn):
    scheduler.update(conn, "custodian", spec_patch={"minutes": 15})
    row = conn.execute("SELECT spec FROM schedules WHERE job='custodian'"
                       ).fetchone()
    assert json.loads(row["spec"])["minutes"] == 15
    with pytest.raises(ValueError):
        scheduler.update(conn, "custodian", spec_patch={"minutes": 9999})
    with pytest.raises(KeyError):
        scheduler.update(conn, "nope", enabled=False)


def test_tick_fires_due_jobs_once_and_records_result(conn):
    fired = []
    sched = scheduler.Scheduler(
        conn, {"custodian": lambda spec: (fired.append("c") or
                                          {"fills": 2, "flags": []})},
        clock=lambda: T0)
    assert sched.tick() == ["custodian"]            # interval job: due at boot
    assert sched.tick() == []                       # not due again for 5 min
    row = conn.execute("SELECT * FROM schedules WHERE job='custodian'"
                       ).fetchone()
    assert row["last_run_at"] == T0
    assert json.loads(row["last_result"])["fills"] == 2
    # disabled jobs never fire
    scheduler.update(conn, "custodian", enabled=False)
    sched2 = scheduler.Scheduler(
        conn, {"custodian": lambda spec: fired.append("x")},
        clock=lambda: T0 + 9999)
    assert sched2.tick() == []


def test_tick_captures_job_errors(conn):
    def boom(spec):
        raise RuntimeError("venue exploded")
    sched = scheduler.Scheduler(conn, {"custodian": boom}, clock=lambda: T0)
    assert sched.tick() == ["custodian"]
    row = conn.execute("SELECT last_result FROM schedules WHERE"
                       " job='custodian'").fetchone()
    assert "venue exploded" in row["last_result"]


def test_analysis_requires_open_markets_at_fire_time():
    """The wiring in api/server.run_analysis filters exchanges via
    sessions.is_open; verify the guard itself: at Tue 14:00 UTC only the US
    exchanges in scope are open."""
    from datetime import datetime, timezone

    from app import sessions
    from app.autonomous import REGIONS
    now = datetime.fromtimestamp(T0, tz=timezone.utc)
    assert [ex for ex in REGIONS["asia"]
            if sessions.is_open(ex, now)] == []          # Asia asleep
    assert [ex for ex in REGIONS["us"]
            if sessions.is_open(ex, now)] == ["NASDAQ", "NYSE"]


# ── H4 / M10: daily firing rule ─────────────────────────────────────────────
DAILY_UTC = {"type": "daily", "time": "15:00", "tz": "UTC",
             "days": [1, 2, 3, 4, 5, 6, 7]}
SLOT = T0 + 3600            # Tue 2026-08-11 15:00 UTC


def _sim(conn, job, spec, start, end, step=20):
    """Drive Scheduler.tick() on a fake clock; return the fire timestamps."""
    scheduler.update(conn, job, enabled=True, spec_patch=spec)
    now = [start]
    fires = []
    sched = scheduler.Scheduler(
        conn, {job: lambda spec: fires.append(now[0]) or {}},
        clock=lambda: now[0])
    while now[0] < end:
        sched.tick()
        now[0] += step
    return fires


def test_never_run_daily_job_fires_at_its_next_slot(conn):
    """H4: last_run_at NULL used to mean 'next slot after now' on every tick,
    so the job never fired. Over a simulated week it must fire once a day."""
    fires = _sim(conn, "reconcile", DAILY_UTC, T0, T0 + 7 * 86400)
    assert len(fires) == 7
    assert fires[0] - SLOT < 60                       # on time, not late
    assert all(b - a == 86400 for a, b in zip(fires, fires[1:]))


def test_never_run_daily_job_at_exact_slot():
    assert scheduler.next_run_ts(DAILY_UTC, None, SLOT) == SLOT
    assert scheduler.next_run_ts(DAILY_UTC, None, SLOT + 30) == SLOT
    # already ran this slot → tomorrow
    assert scheduler.next_run_ts(DAILY_UTC, SLOT + 5, SLOT + 30) == \
        SLOT + 86400


def test_missed_daily_slot_is_skipped_not_backfired():
    """M10: process down over the slot. Boot 2h later: the stale slot is NOT
    fired; the job waits for tomorrow's."""
    yesterday = SLOT - 86400
    late = SLOT + 2 * 3600
    assert scheduler.next_run_ts(DAILY_UTC, yesterday, late) == SLOT + 86400
    assert scheduler.next_run_ts(DAILY_UTC, None, late) == SLOT + 86400


def test_missed_slot_inside_grace_window_still_fires():
    g = scheduler.DAILY_GRACE_SECONDS
    yesterday = SLOT - 86400
    assert scheduler.next_run_ts(DAILY_UTC, yesterday, SLOT + g) == SLOT
    assert scheduler.next_run_ts(DAILY_UTC, yesterday, SLOT + g + 1) == \
        SLOT + 86400


def test_downtime_does_not_fire_late_in_tick(conn):
    # last ran yesterday; the desk boots 3h after today's slot
    scheduler.update(conn, "reconcile", enabled=True, spec_patch=DAILY_UTC)
    conn.execute("UPDATE schedules SET last_run_at=? WHERE job='reconcile'",
                 (SLOT - 86400,))
    sched = scheduler.Scheduler(conn, {"reconcile": lambda s: {}},
                                clock=lambda: SLOT + 3 * 3600)
    assert sched.tick() == []


def test_weekday_only_daily_never_run_skips_weekend(conn):
    spec = {"type": "daily", "time": "02:00", "tz": "Asia/Singapore",
            "days": [1, 2, 3, 4, 5]}
    fires = _sim(conn, "analysis_asia", spec, T0, T0 + 7 * 86400, step=60)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    days = [datetime.fromtimestamp(f, ZoneInfo("Asia/Singapore")).isoweekday()
            for f in fires]
    assert days == [3, 4, 5, 1, 2]                    # Wed..Fri, Mon, Tue


# ── "local" timezone is the host's real zone, DST-aware ─────────────────────
def test_local_tz_honours_dst(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    tz = scheduler._tz("local")
    assert getattr(tz, "key", None) == "Europe/London"
    spec = {"type": "daily", "time": "09:00", "tz": "local",
            "days": [1, 2, 3, 4, 5, 6, 7]}
    # Sat 2026-10-24 12:00 UTC (BST); clocks go back Sun 25 Oct. The slot on
    # Mon 26 Oct 09:00 is GMT = 09:00 UTC, not 08:00 UTC as a fixed +1h
    # offset taken on the Saturday would give.
    from datetime import datetime, timezone
    sat = int(datetime(2026, 10, 24, 12, tzinfo=timezone.utc).timestamp())
    sun_slot = scheduler.next_run_ts(spec, sat, sat)
    mon_slot = scheduler._next_slot_after(spec, sun_slot)
    assert datetime.fromtimestamp(sun_slot, timezone.utc).hour == 9   # GMT
    assert datetime.fromtimestamp(mon_slot, timezone.utc).hour == 9


def test_local_tz_falls_back_to_etc_localtime(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    tz = scheduler._tz("local")
    # whatever the host is, it must be a real zone that knows about DST
    # transitions, not a frozen datetime.timezone offset
    from datetime import timezone
    assert not isinstance(tz, timezone)


def test_a_job_that_causes_other_writes_still_records_its_run(tmp_path):
    """Regression (2026-11-04): the custodian's retry made claire-api write
    from another connection while tick() still held its SELECT cursor open;
    the stale read snapshot could not be upgraded, the last_run_at UPDATE
    failed with 'database is locked', and the job re-fired every 20 s."""
    import sqlite3
    from app.accounting import db as desk_db
    from app.scheduler import Scheduler
    path = tmp_path / "d.db"
    conn = desk_db.connect(path)
    desk_db.init(conn)
    scheduler.seed(conn)
    # as on the live desk: several jobs enabled, the custodian NOT the last
    # row — so the SELECT cursor is still open while it runs
    conn.execute("UPDATE schedules SET enabled = job IN ('custodian',"
                 " 'watcher', 'event_scan')")

    def custodian_like(spec):
        other = sqlite3.connect(path, isolation_level=None)   # e.g. claire-api
        other.execute("INSERT INTO service_health (service, checked_at, ok)"
                      " VALUES ('x', 1, 'ok')")
        other.close()
        return {"ok": True}
    s = Scheduler(conn, {"custodian": custodian_like}, clock=lambda: 1_800_000_000)
    assert s.tick() == ["custodian"]
    assert conn.execute("SELECT last_run_at FROM schedules WHERE"
                        " job='custodian'").fetchone()[0] == 1_800_000_000
    assert s.tick() == []                       # not due again 0 s later
