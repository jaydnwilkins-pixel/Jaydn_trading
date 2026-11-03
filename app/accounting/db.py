"""desk.db connection — WAL + busy_timeout in every process (DESIGN.md §7).

File databases get ONE CONNECTION PER THREAD (thread-local proxy): parallel
graph nodes and API worker threads each own an isolated connection, WAL +
busy_timeout arbitrate between them, and a repo transaction can never be
interleaved by another thread's statements. `:memory:` (tests) stays a single
shared connection.
"""
import sqlite3
import threading
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def _raw_connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None,  # explicit BEGIN in repo
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class ThreadLocalConnection:
    """One real sqlite3 connection per thread, same file, same interface."""

    def __init__(self, path):
        self._path = str(path)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = _raw_connect(self._path)
            self._local.conn = c
        return c

    def __getattr__(self, name):
        return getattr(self._conn(), name)


def connect(path):
    if str(path) == ":memory:":
        return _raw_connect(path)
    return ThreadLocalConnection(path)


# additive migrations for databases created before a column existed;
# "duplicate column" just means it's already applied
MIGRATIONS = [
    "ALTER TABLE work_items ADD COLUMN archived_at INTEGER",
    "ALTER TABLE work_items ADD COLUMN trigger TEXT",
    # price is per MODEL, not per provider: billing haiku at sonnet's rate
    # overstated the news agent threefold while billing opus at sonnet's rate
    # understated the arbiter fivefold, and both errors fed the same total
    "ALTER TABLE provider_models ADD COLUMN cost_per_1k_in REAL",
    "ALTER TABLE provider_models ADD COLUMN cost_per_1k_out REAL",
    # cached input is billed at a different rate from fresh input
    "ALTER TABLE agent_runs ADD COLUMN cache_write_tokens INTEGER",
    "ALTER TABLE agent_runs ADD COLUMN cache_read_tokens INTEGER",
    # a human override points back at the PASS verdict it overturned, so the
    # approval card can show the evidence the desk declined on
    "ALTER TABLE work_items ADD COLUMN override_of TEXT",
    # a sell review points back at the run that opened the position, so it can
    # reuse that analysis instead of paying to think about the name again
    "ALTER TABLE work_items ADD COLUMN prior_run TEXT",
    # the best price seen since entry — highest for a long, LOWEST for a
    # short. Without it a "trailing" floor never moved off the entry price.
    "ALTER TABLE price_alerts ADD COLUMN peak_base REAL",
    # the old name described the old behaviour; anything still on it would
    # silently keep measuring from entry
    "UPDATE price_alerts SET rule='trail_pct' WHERE rule='drop_pct_from_entry'",
    # M4: the order row exists before the venue call, carrying the id the
    # venue is given; submitted_at is the claim that stops a second placement
    "ALTER TABLE orders ADD COLUMN client_order_id TEXT",
    "ALTER TABLE orders ADD COLUMN submitted_at INTEGER",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_id"
    " ON orders(client_order_id)",
    # decision time: queued (pending_session) orders count toward the daily
    # trade cap too — they used to be invisible to it until placed
    "ALTER TABLE orders ADD COLUMN created_at INTEGER",
    "UPDATE orders SET created_at=COALESCE(placed_at, updated_at)"
    " WHERE created_at IS NULL",
    # cash committed to open buys; without it two approvals could both pass
    # the cash check and the second fill would be refused at the venue's
    # expense (the sim fills instantly, the book debits at poll time)
    "ALTER TABLE orders ADD COLUMN reserved_base INTEGER",
    # M7: trailing rules belong to a POSITION (instrument, account, side), and
    # there is exactly one. The old ON CONFLICT DO NOTHING had no constraint to
    # hit, so every approval appended another row. Dedupe (keep the newest —
    # it carries the latest threshold) BEFORE the unique index can be built.
    "ALTER TABLE price_alerts ADD COLUMN account_id TEXT",
    "ALTER TABLE price_alerts ADD COLUMN side INTEGER",
    "DELETE FROM price_alerts WHERE rule='trail_pct' AND id NOT IN"
    " (SELECT MAX(id) FROM price_alerts WHERE rule='trail_pct'"
    "  GROUP BY instrument_id, IFNULL(account_id,''), IFNULL(side,0))",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_trail_unique"
    " ON price_alerts(instrument_id, IFNULL(account_id,''), IFNULL(side,0))"
    " WHERE rule='trail_pct'",
]


def init(conn) -> None:
    conn.executescript(SCHEMA.read_text())
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
    _widen_provider_kinds(conn)


def _widen_provider_kinds(conn) -> None:
    """Databases created before the Claude Agent SDK provider have a CHECK
    on providers.kind that rejects 'claude_sdk'. SQLite cannot alter a CHECK,
    so rebuild the table in place (create-copy-drop-rename; no indexes or
    triggers hang off this table). Rows and ids are preserved, so the
    agents/provider_models references stay valid."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                       " AND name='providers'").fetchone()
    if row is None or "claude_sdk" in row[0]:
        return
    new_sql = row[0].replace("'anthropic','openai_compatible'",
                             "'anthropic','claude_sdk','openai_compatible'")
    if "claude_sdk" not in new_sql:
        raise RuntimeError("providers table has an unexpected kind CHECK; "
                           "migrate it by hand")
    for old in ("CREATE TABLE IF NOT EXISTS providers", "CREATE TABLE providers"):
        if new_sql.startswith(old):
            new_sql = "CREATE TABLE providers_new" + new_sql[len(old):]
            break
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                           " AND name='providers'").fetchone()
        if "claude_sdk" in cur[0]:          # another process won the race
            conn.execute("COMMIT")
            return
        conn.execute(new_sql)
        conn.execute("INSERT INTO providers_new SELECT * FROM providers")
        conn.execute("DROP TABLE providers")
        conn.execute("ALTER TABLE providers_new RENAME TO providers")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"foreign key check failed: {list(bad)[:3]}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
