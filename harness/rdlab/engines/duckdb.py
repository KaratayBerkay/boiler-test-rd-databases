"""Embedded DuckDB via the duckdb Python package."""
from __future__ import annotations

from pathlib import Path

import duckdb

from ..config import Target
from ..schema import Table
from ..util import ROOT
from .base import Conn, Engine


class DuckConn(Conn):
    paramstyle = "qmark"

    def execute(self, sql, params=None, *, rendered=False, fetch=True):
        # NB: duckdb's cursor() opens a *separate* connection (own transaction), so run on the connection itself
        s = sql if rendered else self.prepare_sql(sql, len(params) if params else 0)
        res = self.raw.execute(s, list(params)) if params else self.raw.execute(s)
        if fetch:
            try:
                return [tuple(r) for r in res.fetchall()]
            except Exception:  # noqa: BLE001
                return None
        return None

    def executemany(self, sql, seq, *, rendered=False):
        s = sql if rendered else self.prepare_sql(sql, len(seq[0]) if seq else 0)
        self.raw.executemany(s, [list(p) for p in seq])

    def set_autocommit(self, on):
        self.autocommit = on
        if not on:
            self.raw.begin()

    def commit(self):
        self.raw.commit()

    def rollback(self):
        try:
            self.raw.rollback()
        except Exception:  # noqa: BLE001
            pass

    def server_version(self):
        return f"DuckDB {duckdb.__version__}"

    def role(self):
        return "embedded"


class DuckDBEngine(Engine):
    driver = "duckdb"
    _shared: dict[str, duckdb.DuckDBPyConnection] = {}

    def db_path(self, target: Target) -> str:
        p = target.database or "data/duckdb/lab.duckdb"
        if p == ":memory:":
            return p
        path = (ROOT / p).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        path = self.db_path(target)
        # one process-wide database handle; each "connection" is a cursor-backed duplicate
        base = self._shared.get(path)
        if base is None:
            threads = int(self.cfg.features.get("threads", 0) or 0)
            cfg = {"threads": threads} if threads else {}
            base = duckdb.connect(path, config=cfg)
            self._shared[path] = base
        raw = base.cursor()
        c = DuckConn(self, raw, target)
        c.autocommit = autocommit
        if not autocommit:
            raw.begin()
        return c

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 5000):
        cols = ", ".join(table.colnames)
        sql = f"INSERT INTO {table.name} ({cols}) SELECT {cols} FROM read_csv('{csv_path}', header = true, nullstr = '', timestampformat = '%Y-%m-%d %H:%M:%S')"
        conn.execute(sql, rendered=True, fetch=False)
        return self.count(conn, table.name), "read_csv"

    def fts_setup(self, conn: Conn) -> list[str]:
        stmts = ["INSTALL fts", "LOAD fts", "PRAGMA create_fts_index('products', 'id', 'description', overwrite = 1)"]
        done = []
        for s in stmts:
            try:
                conn.execute(s, rendered=True, fetch=False)
                done.append(s)
            except Exception as e:  # noqa: BLE001
                done.append(f"FAILED {s}: {e}")
        return done
