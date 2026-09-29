"""Embedded SQLite via the stdlib sqlite3 module.

Logging strategy for an embedded engine lives in the application: every statement is timed in SQLiteConn.execute and,
when it exceeds RDLAB_SQLITE_SLOW_MS (default 100; 0 = everything), appended as a JSON line to
RDLAB_SQLITE_SLOW_LOG (default results/sqlite/logs/slow.jsonl). Worker processes inherit the environment, so the
logging phase can switch "log everything" on for a workload by setting the variable before spawning workers.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import sqlite3
import time
from pathlib import Path

from ..config import Target
from ..schema import Table
from ..util import ROOT
from .base import Conn, Engine


_LOG_HANDLES: dict[tuple[str, int], "object"] = {}


def _slow_log_handle(path: Path):
    key = (str(path), os.getpid())
    h = _LOG_HANDLES.get(key)
    if h is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        h = open(path, "a", encoding="utf-8", buffering=1)      # line-buffered, one handle per process
        _LOG_HANDLES[key] = h
    return h


def _slow_log_settings() -> tuple[float | None, Path | None]:
    v = os.environ.get("RDLAB_SQLITE_SLOW_MS")
    if v is None:
        return None, None
    try:
        return float(v), Path(os.environ.get("RDLAB_SQLITE_SLOW_LOG") or (ROOT / "results" / "sqlite" / "logs" / "slow.jsonl"))
    except ValueError:
        return None, None


class SQLiteConn(Conn):
    paramstyle = "qmark"

    def execute(self, sql, params=None, *, rendered=False, fetch=True):
        thr, path = _slow_log_settings()
        if thr is None:
            return super().execute(sql, params, rendered=rendered, fetch=fetch)
        t0 = time.perf_counter()
        try:
            return super().execute(sql, params, rendered=rendered, fetch=fetch)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            if ms >= thr and path is not None:
                try:
                    _slow_log_handle(path).write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"), "duration_ms": round(ms, 3),
                                                             "pid": os.getpid(), "database": self.target.database, "sql": sql[:500]}) + "\n")
                except OSError:
                    pass

    def _adapt_params(self, params):
        out = []
        for p in params:
            if isinstance(p, bool):
                out.append(int(p))
            elif isinstance(p, dt.datetime):
                out.append(p.strftime("%Y-%m-%d %H:%M:%S"))
            elif isinstance(p, decimal.Decimal):
                out.append(float(p))
            else:
                out.append(p)
        return tuple(out)

    def server_version(self):
        return f"SQLite {sqlite3.sqlite_version}"

    def role(self):
        return "embedded"


class SQLiteEngine(Engine):
    driver = "sqlite3"
    manual_autocommit = False

    def db_path(self, target: Target) -> str:
        p = target.database or "data/sqlite/lab.db"
        if p == ":memory:":
            return p
        path = (ROOT / p).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        # Python 3.12 transaction control: autocommit=False keeps a transaction open at all times (so a SELECT
        # establishes a read snapshot, which is what the isolation tests need); autocommit=True = plain SQLite autocommit.
        raw = sqlite3.connect(self.db_path(target), timeout=timeout, autocommit=True, check_same_thread=False)
        raw.execute("PRAGMA journal_mode = WAL")
        raw.execute("PRAGMA synchronous = NORMAL")
        raw.execute("PRAGMA cache_size = -262144")   # 256 MB
        raw.execute("PRAGMA temp_store = MEMORY")
        raw.execute("PRAGMA foreign_keys = ON")
        raw.autocommit = autocommit                  # pragmas must run outside a transaction
        c = SQLiteConn(self, raw, target)
        c.autocommit = autocommit
        return c

    def adapt_value(self, v, ptype):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, dt.datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(v, decimal.Decimal):
            return float(v)
        return v

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 20000):
        n, m = super().bulk_load(conn, table, csv_path, batch)
        return n, "executemany (single transaction)"

    def fts_setup(self, conn: Conn) -> list[str]:
        stmts = ["CREATE VIRTUAL TABLE products_fts USING fts5(description, content='products', content_rowid='id')",
                 "INSERT INTO products_fts(products_fts) VALUES ('rebuild')"]
        done = []
        for s in stmts:
            try:
                conn.execute(s, rendered=True, fetch=False)
                done.append(s)
            except Exception as e:  # noqa: BLE001
                done.append(f"FAILED {s}: {e}")
        return done

    def max_connections(self, conn: Conn) -> int | None:
        return None

