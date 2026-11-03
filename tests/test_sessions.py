"""Session calendar tests (DESIGN.md §18 layer 5). 2026-08-11 is a Tuesday."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import sessions
from app.sessions import Session, close_of, is_open, next_open

SGT = ZoneInfo("Asia/Singapore")
NY = ZoneInfo("America/New_York")
HK = ZoneInfo("Asia/Hong_Kong")


def at(tz, y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=tz)


def test_sgx_hours():
    assert is_open("SGX", at(SGT, 2026, 8, 11, 10))
    assert not is_open("SGX", at(SGT, 2026, 8, 11, 8))       # pre-open
    assert not is_open("SGX", at(SGT, 2026, 8, 11, 17, 30))  # post-close
    assert not is_open("SGX", at(SGT, 2026, 8, 8, 10))       # Saturday


def test_cross_timezone_input():
    # 02:00 UTC on a Tuesday is 10:00 in Singapore — open regardless of the
    # timezone the caller happens to hold
    assert is_open("SGX", datetime(2026, 8, 11, 2, 0, tzinfo=ZoneInfo("UTC")))


def test_hkex_lunch_break():
    assert is_open("HKEX", at(HK, 2026, 8, 11, 11))
    assert not is_open("HKEX", at(HK, 2026, 8, 11, 12, 30))
    assert is_open("HKEX", at(HK, 2026, 8, 11, 13, 30))


def test_sgx_and_nyse_never_overlap():
    for hour in range(24):
        ts = datetime(2026, 8, 11, hour, 0, tzinfo=ZoneInfo("UTC"))
        assert not (is_open("SGX", ts) and is_open("NYSE", ts))


def test_next_open_over_weekend():
    fri_evening = at(SGT, 2026, 8, 7, 18)
    # Mon 2026-08-10 is SGX National Day (observed) → Tuesday
    assert next_open("SGX", fri_evening) == at(SGT, 2026, 8, 11, 9)


def test_next_open_during_lunch_is_lunch_end():
    assert next_open("HKEX", at(HK, 2026, 8, 11, 12, 15)) == at(HK, 2026, 8, 11, 13)


def test_next_open_when_open_is_now():
    now = at(SGT, 2026, 8, 11, 10)
    assert next_open("SGX", now) == now


def test_close_of_session():
    assert close_of("SGX", at(SGT, 2026, 8, 11, 10)) == at(SGT, 2026, 8, 11, 17)
    # Friday post-close → next SESSION's close: Monday is a holiday → Tuesday
    assert close_of("SGX", at(SGT, 2026, 8, 7, 18)) == at(SGT, 2026, 8, 11, 17)


def test_holidays_respected():
    cal = {"SGX": Session("SGX", "Asia/Singapore", "09:00", "17:00",
                          holidays=frozenset({"2026-08-10"}))}  # Monday off
    assert not is_open("SGX", at(SGT, 2026, 8, 10, 10), cal)
    assert next_open("SGX", at(SGT, 2026, 8, 7, 18), cal) == at(SGT, 2026, 8, 11, 9)


def test_unknown_exchange_fails_loudly():
    with pytest.raises(KeyError):
        is_open("MOON", at(SGT, 2026, 8, 11, 10))


def test_db_roundtrip():
    from app.accounting import db
    conn = db.connect(":memory:")
    db.init(conn)
    sessions.seed_db(conn)
    loaded = sessions.load(conn)
    assert is_open("SGX", at(SGT, 2026, 8, 11, 10), loaded)
    assert loaded["HKEX"].lunch_break == "12:00-13:00"


# ── M11: calendar completeness ─────────────────────────────────────────────
TK = ZoneInfo("Asia/Tokyo")
IST = ZoneInfo("Asia/Kolkata")
LDN = ZoneInfo("Europe/London")
SYD = ZoneInfo("Australia/Sydney")
PAR = ZoneInfo("Europe/Paris")


@pytest.mark.parametrize("exchange,tz,day", [
    ("SGX", SGT, (2026, 5, 27)),    # Hari Raya Haji
    ("SGX", SGT, (2026, 6, 1)),     # Vesak observed
    ("SGX", SGT, (2026, 11, 9)),    # Deepavali observed
    ("HKEX", HK, (2026, 4, 7)), ("HKEX", HK, (2026, 5, 1)),
    ("HKEX", HK, (2026, 5, 25)), ("HKEX", HK, (2026, 6, 19)),
    ("HKEX", HK, (2026, 10, 19)),
    ("TSE", TK, (2026, 1, 12)), ("TSE", TK, (2026, 3, 20)),
    ("TSE", TK, (2026, 7, 20)), ("TSE", TK, (2026, 9, 21)),
    ("TSE", TK, (2026, 9, 22)), ("TSE", TK, (2026, 9, 23)),
    ("TSE", TK, (2026, 10, 12)),
    ("NSE", IST, (2026, 11, 10)), ("NSE", IST, (2026, 11, 24)),
    ("NSE", IST, (2026, 12, 25)),
])
def test_missing_holidays_now_closed(exchange, tz, day):
    assert not is_open(exchange, at(tz, *day, 10, 30))


def test_every_default_holiday_is_a_weekday():
    """A weekend date in a holiday list is dead weight and usually means the
    observed (substitute) weekday was forgotten."""
    from datetime import date
    for s in sessions.DEFAULTS.values():
        for iso in s.holidays:
            assert date.fromisoformat(iso).weekday() < 5, (s.exchange, iso)
        for iso, _ in s.early_closes:
            assert date.fromisoformat(iso).weekday() < 5, (s.exchange, iso)
            assert iso not in s.holidays, (s.exchange, iso)


def test_deepavali_pushes_next_open_and_close():
    fri = at(SGT, 2026, 11, 6, 18)
    assert next_open("SGX", fri) == at(SGT, 2026, 11, 10, 9)
    assert close_of("SGX", fri) == at(SGT, 2026, 11, 10, 17)


@pytest.mark.parametrize("exchange,tz,day,close", [
    ("NYSE", NY, (2026, 11, 27), (13, 0)),
    ("NASDAQ", NY, (2026, 12, 24), (13, 0)),
    ("LSE", LDN, (2026, 12, 24), (12, 30)),
    ("LSE", LDN, (2026, 12, 31), (12, 30)),
    ("ASX", SYD, (2026, 12, 24), (14, 10)),
    ("ASX", SYD, (2026, 12, 31), (14, 10)),
    ("PARIS", PAR, (2026, 12, 24), (14, 5)),
    ("PARIS", PAR, (2026, 12, 31), (14, 5)),
])
def test_half_days_close_early(exchange, tz, day, close):
    h, m = close
    before = at(tz, *day, h - 1, m)
    assert is_open(exchange, before)
    assert not is_open(exchange, at(tz, *day, h, m))
    assert close_of(exchange, before) == at(tz, *day, h, m)
    # a normal day keeps its normal close
    assert close_of(exchange, at(tz, 2026, 12, 22, 11)) == \
        datetime.combine(datetime(2026, 12, 22).date(),
                         sessions._t(sessions.DEFAULTS[exchange].close_time),
                         tzinfo=tz)


def test_hkex_half_day_has_no_afternoon_session():
    # after the 12:00 early close the next open is the following session,
    # not 13:00 the same day
    assert not is_open("HKEX", at(HK, 2026, 12, 24, 13, 30))
    assert next_open("HKEX", at(HK, 2026, 12, 24, 12, 5)) == \
        at(HK, 2026, 12, 28, 9, 30)


def test_half_days_survive_the_db_roundtrip_and_new_dates_reach_old_dbs():
    import json as _json
    from app.accounting import db
    conn = db.connect(":memory:")
    db.init(conn)
    # a DB seeded by the old code: SGX without the new dates, plus an
    # operator-added closure that must be kept
    conn.execute("INSERT INTO exchange_sessions (exchange, tz, open_time,"
                 " close_time, holidays) VALUES ('SGX', 'Asia/Singapore',"
                 " '09:00', '17:00', ?)",
                 (_json.dumps(["2026-01-01", "2026-03-04"]),))
    sessions.seed_db(conn)
    loaded = sessions.load(conn)
    assert "2026-11-09" in loaded["SGX"].holidays          # merged in
    assert "2026-03-04" in loaded["SGX"].holidays          # operator kept
    assert loaded["NYSE"].early_closes == sessions.DEFAULTS["NYSE"].early_closes
    assert not is_open("NYSE", at(NY, 2026, 11, 27, 14), loaded)
