"""SQLite storage. Kept behind small helper functions so PostgreSQL can replace it later."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Columns added after a table first shipped: (table, column, type). Applied once, in order,
# to databases created by an older version, so updating never loses collected data.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("markets", "expiration_value", "REAL"),   # v2: the settlement index value Kalshi reports
]


def now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA_FILE.read_text())
            for table, column, ctype in MIGRATIONS:
                cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            if row["v"] is None or row["v"] < SCHEMA_VERSION:
                self._conn.execute("INSERT INTO schema_version VALUES (?, ?)", (SCHEMA_VERSION, now_ms()))
            self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.tx() as c:
            return c.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
        return int(cur.lastrowid)

    def insert_many(self, table: str, rows: list[dict[str, Any]], or_ignore: bool = False) -> int:
        if not rows:
            return 0
        cols = list(rows[0])
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        with self.tx() as c:
            cur = c.executemany(sql, [tuple(r[k] for k in cols) for r in rows])
            return cur.rowcount

    def upsert(self, table: str, row: dict[str, Any], key: str | tuple[str, ...]) -> None:
        keys = (key,) if isinstance(key, str) else key
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c not in keys)
        self.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
            f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}",
            tuple(row.values()))

    # --- small helpers used across the app ---------------------------------
    def log_event(self, component: str, level: str, message: str, details: dict | None = None) -> None:
        self.insert("system_events", {
            "ts_ms": now_ms(), "component": component, "level": level, "message": message,
            "details_json": json.dumps(details) if details else None})

    def log_risk(self, severity: str, code: str, message: str, mode: str | None = None,
                 details: dict | None = None) -> None:
        self.insert("risk_events", {
            "ts_ms": now_ms(), "mode": mode, "severity": severity, "code": code,
            "message": message, "details_json": json.dumps(details) if details else None})

    def heartbeat(self, component: str, info: dict | None = None) -> None:
        self.upsert("heartbeats", {"component": component, "ts_ms": now_ms(),
                                   "info_json": json.dumps(info or {})}, "component")

    def get_control(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM control_state WHERE key=?", (key,))
        return row["value"] if row else default

    def set_control(self, key: str, value: str, by: str) -> None:
        self.upsert("control_state", {"key": key, "value": value, "updated_ms": now_ms(),
                                      "updated_by": by}, "key")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_db(path: Path | str) -> Database:
    db = Database(path)
    db.init_schema()
    return db
