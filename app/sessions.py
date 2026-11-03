"""Market sessions (DESIGN.md §14a) — pure functions over session data.
No LLM, no network, no clock reads: callers pass aware datetimes.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import json


@dataclass(frozen=True)
class Session:
    exchange: str
    tz: str
    open_time: str          # local wall time, "09:30"
    close_time: str
    lunch_break: str | None = None   # "12:00-13:00"
    holidays: frozenset = frozenset()  # ISO dates
    # half days: ((ISO date, "HH:MM" early close), ...). Not stored in the
    # exchange_sessions table (no column); load() takes them from DEFAULTS.
    early_closes: tuple = ()


# Holiday lists are HAND-MAINTAINED yearly (DESIGN.md §14a) and deliberately
# conservative — only well-established dates; a missed holiday delays a queued
# order by a day, nothing more. 2026 set (flat lists, no per-year structure);
# review each January and add the new year's dates.
_US26 = ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
         "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"]
# NYSE/Nasdaq: day after Thanksgiving and Christmas Eve close 13:00 ET
_US26_HALF = (("2026-11-27", "13:00"), ("2026-12-24", "13:00"))

DEFAULTS = {s.exchange: s for s in [
    Session("NASDAQ", "America/New_York", "09:30", "16:00",
            holidays=frozenset(_US26), early_closes=_US26_HALF),
    Session("NYSE",   "America/New_York", "09:30", "16:00",
            holidays=frozenset(_US26), early_closes=_US26_HALF),
    Session("LSE",    "Europe/London",    "08:00", "16:30",
            holidays=frozenset(["2026-01-01", "2026-04-03", "2026-04-06",
                                "2026-05-04", "2026-05-25", "2026-08-31",
                                "2026-12-25", "2026-12-28"]),
            early_closes=(("2026-12-24", "12:30"), ("2026-12-31", "12:30"))),
    Session("XETRA",  "Europe/Berlin",    "09:00", "17:30",
            holidays=frozenset(["2026-01-01", "2026-04-03", "2026-04-06",
                                "2026-05-01", "2026-12-24", "2026-12-25",
                                "2026-12-31"])),
    Session("PARIS",  "Europe/Paris",     "09:00", "17:30",
            holidays=frozenset(["2026-01-01", "2026-04-03", "2026-04-06",
                                "2026-05-01", "2026-12-25"]),
            early_closes=(("2026-12-24", "14:05"), ("2026-12-31", "14:05"))),
    # SGX: Hari Raya Puasa (Sat 21 Mar) needs no weekday closure
    Session("SGX",    "Asia/Singapore",   "09:00", "17:00",
            holidays=frozenset(["2026-01-01", "2026-02-17", "2026-02-18",
                                "2026-04-03", "2026-05-01",
                                "2026-05-27",               # Hari Raya Haji
                                "2026-06-01",               # Vesak (Sun 31 May)
                                "2026-08-10",               # National Day obs.
                                "2026-11-09",               # Deepavali (Sun 8 Nov)
                                "2026-12-25"]),
            early_closes=(("2026-12-24", "12:00"), ("2026-12-31", "12:00"))),
    Session("HKEX",   "Asia/Hong_Kong",   "09:30", "16:00", "12:00-13:00",
            holidays=frozenset(["2026-01-01", "2026-02-17", "2026-02-18",
                                "2026-02-19", "2026-04-03", "2026-04-06",
                                "2026-04-07",               # day after Easter Mon
                                "2026-05-01",               # Labour Day
                                "2026-05-25",               # Buddha's B'day obs.
                                "2026-06-19",               # Tuen Ng
                                "2026-07-01", "2026-10-01",
                                "2026-10-19",               # Chung Yeung obs.
                                "2026-12-25"]),
            # Christmas Eve / New Year's Eve: morning session only
            early_closes=(("2026-12-24", "12:00"), ("2026-12-31", "12:00"))),
    Session("TSE",    "Asia/Tokyo",       "09:00", "15:30", "11:30-12:30",
            holidays=frozenset(["2026-01-01", "2026-01-02",
                                "2026-01-12",               # Coming of Age Day
                                "2026-02-11", "2026-02-23",
                                "2026-03-20",               # Vernal Equinox
                                "2026-04-29", "2026-05-04", "2026-05-05",
                                "2026-05-06",
                                "2026-07-20",               # Marine Day
                                "2026-08-11",
                                "2026-09-21", "2026-09-22",  # Aged / Kokumin
                                "2026-09-23",               # Autumnal Equinox
                                "2026-10-12",               # Sports Day
                                "2026-11-03", "2026-11-23", "2026-12-31"])),
    Session("ASX",    "Australia/Sydney", "10:00", "16:00",
            holidays=frozenset(["2026-01-01", "2026-01-26", "2026-04-03",
                                "2026-04-06", "2026-06-08", "2026-12-25",
                                "2026-12-28"]),
            early_closes=(("2026-12-24", "14:10"), ("2026-12-31", "14:10"))),
    # NSE 2026 trading-holiday circular (weekday closures only)
    Session("NSE",    "Asia/Kolkata",     "09:15", "15:30",
            holidays=frozenset(["2026-01-15",               # Maharashtra polls
                                "2026-01-26",               # Republic Day
                                "2026-03-03",               # Holi
                                "2026-03-26",               # Ram Navami
                                "2026-03-31",               # Mahavir Jayanti
                                "2026-04-03",               # Good Friday
                                "2026-04-14",               # Ambedkar Jayanti
                                "2026-05-01",               # Maharashtra Day
                                "2026-05-28",               # Bakri Id
                                "2026-06-26",               # Muharram
                                "2026-09-14",               # Ganesh Chaturthi
                                "2026-10-02",               # Gandhi Jayanti
                                "2026-10-20",               # Dussehra
                                "2026-11-10",               # Diwali Balipratipada
                                "2026-11-24",               # Guru Nanak Jayanti
                                "2026-12-25"])),            # Christmas
]}


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _session(exchange, sessions) -> Session:
    table = sessions or DEFAULTS
    if exchange not in table:
        raise KeyError(f"no session calendar for exchange {exchange!r}")
    return table[exchange]


def _is_trading_day(s: Session, d) -> bool:
    return d.weekday() < 5 and d.isoformat() not in s.holidays


def _close_on(s: Session, d) -> time:
    """That day's close: the early close on a half day, else close_time."""
    iso = d.isoformat()
    for day, hhmm in s.early_closes:
        if day == iso:
            return _t(hhmm)
    return _t(s.close_time)


def is_open(exchange: str, ts: datetime, sessions=None) -> bool:
    s = _session(exchange, sessions)
    local = ts.astimezone(ZoneInfo(s.tz))
    if not _is_trading_day(s, local.date()):
        return False
    t = local.time()
    if not (_t(s.open_time) <= t < _close_on(s, local.date())):
        return False
    if s.lunch_break:
        lo, hi = s.lunch_break.split("-")
        if _t(lo) <= t < _t(hi):
            return False
    return True


def next_open(exchange: str, ts: datetime, sessions=None) -> datetime:
    """Earliest instant >= ts at which the exchange is open (exchange-local tz)."""
    s = _session(exchange, sessions)
    local = ts.astimezone(ZoneInfo(s.tz))
    if is_open(exchange, ts, sessions):
        return local
    for day in range(15):
        d = (local + timedelta(days=day)).date()
        if not _is_trading_day(s, d):
            continue
        candidates = [_t(s.open_time)]
        if s.lunch_break:
            candidates.append(_t(s.lunch_break.split("-")[1]))
        for c in candidates:
            if c >= _close_on(s, d):           # half day: no afternoon session
                continue
            candidate = datetime.combine(d, c, tzinfo=ZoneInfo(s.tz))
            if candidate >= local:
                return candidate
    raise ValueError(f"no session for {exchange} within 15 days — holiday data broken?")


def close_of(exchange: str, ts: datetime, sessions=None) -> datetime:
    """End of the session in effect: today's close while open (or pre-open on a
    trading day), else the close of the next session. Drives order TTLs and
    session-aware approval expiry."""
    s = _session(exchange, sessions)
    local = ts.astimezone(ZoneInfo(s.tz))
    for day in range(15):
        d = (local + timedelta(days=day)).date()
        if not _is_trading_day(s, d):
            continue
        close = datetime.combine(d, _close_on(s, d), tzinfo=ZoneInfo(s.tz))
        if close > local:
            return close
    raise ValueError(f"no session for {exchange} within 15 days — holiday data broken?")


# ── DB bridge: the exchange_sessions table is the deployed source of truth ──
def seed_db(conn):
    for s in DEFAULTS.values():
        conn.execute(
            "INSERT OR IGNORE INTO exchange_sessions"
            " (exchange, tz, open_time, close_time, lunch_break, holidays)"
            " VALUES (?,?,?,?,?,?)",
            (s.exchange, s.tz, s.open_time, s.close_time, s.lunch_break,
             json.dumps(sorted(s.holidays))))
        # holidays added to DEFAULTS reach an already-seeded DB: union the
        # default dates into the stored list. Operator-added dates are kept;
        # (trade-off) a default date an operator deleted comes back.
        row = conn.execute("SELECT holidays FROM exchange_sessions"
                           " WHERE exchange=?", (s.exchange,)).fetchone()
        stored = set(json.loads(row["holidays"] or "[]")) if row else set()
        merged = sorted(stored | set(s.holidays))
        if merged != sorted(stored):
            conn.execute("UPDATE exchange_sessions SET holidays=?"
                         " WHERE exchange=?", (json.dumps(merged), s.exchange))


def load(conn) -> dict:
    # half days have no DB column: they always come from DEFAULTS
    return {
        r["exchange"]: Session(r["exchange"], r["tz"], r["open_time"],
                               r["close_time"], r["lunch_break"],
                               frozenset(json.loads(r["holidays"])),
                               DEFAULTS[r["exchange"]].early_closes
                               if r["exchange"] in DEFAULTS else ())
        for r in conn.execute("SELECT * FROM exchange_sessions")
    }
