"""SQLite persistence for the single pricing day.

A :class:`Database` opens connections in autocommit mode
(``isolation_level=None``); write operations explicitly run
``BEGIN IMMEDIATE`` so concurrent submissions are totally ordered by SQLite's
write lock.  An in-process :class:`threading.RLock` serializes writers inside
the single uvicorn worker as well.

The small ``stat`` mechanism only counts rows fetched by labelled reads; it is
used by the scaling check to prove adjudication reads a constant number of
summary rows as historical orders grow.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seq (
    name TEXT PRIMARY KEY,
    n    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS currency (
    code TEXT PRIMARY KEY,
    bit  INTEGER NOT NULL UNIQUE CHECK (bit BETWEEN 0 AND 31)
);

CREATE TABLE IF NOT EXISTS currency_state (
    code     TEXT PRIMARY KEY REFERENCES currency(code),
    group_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_currency_state_group ON currency_state(group_id);

CREATE TABLE IF NOT EXISTS group_summary (
    group_id        TEXT PRIMARY KEY,
    mask            INTEGER NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL CHECK (status IN ('active','suspended')),
    reason          TEXT,
    currencies_json TEXT NOT NULL,
    summary_json    TEXT NOT NULL,
    created_seq     INTEGER NOT NULL,
    updated_seq     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS quote (
    quote_no  TEXT PRIMARY KEY,
    source    TEXT NOT NULL,
    target    TEXT NOT NULL,
    low_n     INTEGER NOT NULL,
    low_d     INTEGER NOT NULL,
    high_n    INTEGER NOT NULL,
    high_d    INTEGER NOT NULL,
    low_text  TEXT NOT NULL,
    high_text TEXT NOT NULL,
    status    TEXT NOT NULL CHECK (status IN ('active','withdrawn')),
    created_seq INTEGER NOT NULL,
    updated_seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quote_source ON quote(source);
CREATE INDEX IF NOT EXISTS idx_quote_target ON quote(target);
CREATE INDEX IF NOT EXISTS idx_quote_status ON quote(status);

CREATE TABLE IF NOT EXISTS batch (
    batch_id    TEXT PRIMARY KEY,
    seq         INTEGER NOT NULL UNIQUE,
    order_count INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ord_item (
    order_no   TEXT PRIMARY KEY,
    batch_id   TEXT NOT NULL REFERENCES batch(batch_id),
    seq        INTEGER NOT NULL,
    group_id   TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    orig_text  TEXT NOT NULL,
    decl_text  TEXT NOT NULL,
    orig_n INTEGER NOT NULL, orig_d INTEGER NOT NULL,
    decl_n INTEGER NOT NULL, decl_d INTEGER NOT NULL,
    rate_n INTEGER NOT NULL, rate_d INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ord_item_batch ON ord_item(batch_id);
CREATE INDEX IF NOT EXISTS idx_ord_item_group ON ord_item(group_id);

CREATE TABLE IF NOT EXISTS conversion_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batch(batch_id),
    group_id    TEXT NOT NULL,
    mask        INTEGER NOT NULL,
    anchor      TEXT NOT NULL,
    table_json  TEXT NOT NULL,
    seq         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_order_lookup
    ON conversion_snapshot(batch_id, group_id);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.write_lock = threading.RLock()
        self._local = threading.local()
        self._init_schema()

    # ------------------------------------------------------------------ #
    # connections / transactions
    # ------------------------------------------------------------------ #

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,  # explicit BEGIN / COMMIT
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO seq(name,n) VALUES('submission',0)"
            )
            conn.commit() if False else None  # autocommit mode: statements persist
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self):
        """Serialize + open one ``BEGIN IMMEDIATE`` write transaction."""
        with self.write_lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @contextlib.contextmanager
    def reader(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def next_seq(self, conn: sqlite3.Connection) -> int:
        conn.execute(
            "INSERT INTO seq(name,n) VALUES('submission',1) "
            "ON CONFLICT(name) DO UPDATE SET n=n+1"
        )
        row = conn.execute("SELECT n FROM seq WHERE name='submission'").fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ #
    # read statistics (for the constant-read scaling proof)
    # ------------------------------------------------------------------ #

    @contextlib.contextmanager
    def collect_stats(self):
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        bucket: dict[str, int] = {}
        stack.append(bucket)
        try:
            yield bucket
        finally:
            stack.pop()

    def _count(self, stat: Optional[str], rows: int) -> None:
        if not stat:
            return
        stack = getattr(self._local, "stack", None)
        if stack:
            bucket = stack[-1]
            entry = bucket.setdefault(stat, {"calls": 0, "rows": 0})
            entry["calls"] += 1
            entry["rows"] += max(0, rows)

    def all(self, conn, sql: str, params: tuple = (), stat: Optional[str] = None):
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        self._count(stat, len(rows))
        return rows

    def one(self, conn, sql: str, params: tuple = (), stat: Optional[str] = None):
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        self._count(stat, 1 if row is not None else 0)
        return row
