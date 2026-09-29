"""CrateDB via the `crate` HTTP client."""
from __future__ import annotations

import datetime as dt
import decimal
from pathlib import Path
from typing import Any

from crate import client as crate_client

from ..config import Target
from ..datagen import read_csv_rows
from ..dialects import render
from ..schema import Table
from .base import Conn, Engine


class CrateConn(Conn):
    paramstyle = "qmark"

    def _adapt_params(self, params):
        out = []
        for p in params:
            if isinstance(p, decimal.Decimal):
                out.append(float(p))
            elif isinstance(p, dt.datetime):
                out.append(p.strftime("%Y-%m-%dT%H:%M:%S"))
            else:
                out.append(p)
        return tuple(out)

    def execute(self, sql, params=None, *, rendered=False, fetch=True):
        s = sql if rendered else self.prepare_sql(sql, len(params) if params else 0)
        cur = self.raw.cursor()
        try:
            cur.execute(s, self._adapt_params(params) if params else None)
            if fetch and cur.description:
                return [tuple(r) for r in cur.fetchall()]
            return None
        finally:
            cur.close()

    def executemany(self, sql, seq, *, rendered=False):
        s = sql if rendered else self.prepare_sql(sql, len(seq[0]) if seq else 0)
        cur = self.raw.cursor()
        try:
            cur.executemany(s, [self._adapt_params(p) for p in seq])
        finally:
            cur.close()

    def set_autocommit(self, on):
        self.autocommit = on

    def commit(self):
        pass

    def rollback(self):
        pass

    def server_version(self):
        try:
            return "CrateDB " + self.execute("SELECT version['number'] FROM sys.nodes LIMIT 1", rendered=True)[0][0]
        except Exception as e:  # noqa: BLE001
            return f"unknown ({e})"

    def role(self):
        return "node"


class CrateEngine(Engine):
    driver = "crate"

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        raw = crate_client.connect(f"http://{target.host}:{target.port}", username=target.user or None, password=target.password or None, timeout=600)
        c = CrateConn(self, raw, target)
        c.autocommit = autocommit
        return c

    def adapt_value(self, v, ptype):
        if isinstance(v, decimal.Decimal):
            return float(v)
        if isinstance(v, dt.datetime):
            return v.strftime("%Y-%m-%dT%H:%M:%S")
        return v

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 5000):
        import json
        n, m = super().bulk_load(conn, table, csv_path, batch)
        conn.execute(f"REFRESH TABLE {table.name}", rendered=True, fetch=False)
        return n, m + " + REFRESH TABLE"

    def create_schema(self, conn, tables):
        stmts = super().create_schema(conn, tables)
        return stmts

    def count(self, conn, table):
        conn.execute(f"REFRESH TABLE {table}", rendered=True, fetch=False)
        return super().count(conn, table)

    def replication_status(self, conn: Conn) -> dict[str, Any]:
        try:
            rows = conn.execute("SELECT table_name, number_of_shards, number_of_replicas FROM information_schema.tables WHERE table_schema = 'doc'", rendered=True)
            health = conn.execute("SELECT table_name, health, missing_shards, underreplicated_shards FROM sys.health", rendered=True)
            nodes = conn.execute("SELECT name, hostname FROM sys.nodes", rendered=True)
            return {"tables": rows, "health": health, "nodes": nodes}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}
