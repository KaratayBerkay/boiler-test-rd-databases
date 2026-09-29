"""QuestDB via its PostgreSQL wire endpoint (psycopg) — bulk load via the HTTP /imp endpoint."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ..config import Target
from ..schema import Table
from .base import Conn
from .pg import PgConn, PsycopgEngine


class QDBConn(PgConn):
    def server_version(self):
        try:
            b = self.execute("SELECT build()", rendered=True)[0][0]
            return str(b).split(",")[0].replace("Build Information: ", "")[:80]
        except Exception:  # noqa: BLE001
            return "QuestDB"

    def role(self):
        return "primary"


class QuestDBEngine(PsycopgEngine):
    driver = "questdb"

    def connect(self, target, *, autocommit=True, timeout=10.0):
        import psycopg
        raw = psycopg.connect(self.conninfo(target, timeout), autocommit=autocommit)
        c = QDBConn(self, raw, target, prepare=False)
        c.autocommit = autocommit
        return c

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 5000):
        http = self.cfg.features.get("http", "http://127.0.0.1:19009")
        with open(csv_path, "rb") as f:
            r = httpx.post(f"{http}/imp", params={"name": table.name, "fmt": "json", "overwrite": "false", "timestamp": table.ts_col or ""},
                           files={"data": (csv_path.name, f, "text/csv")}, timeout=600)
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "OK":
            raise RuntimeError(f"questdb import failed: {j}")
        return int(j.get("rowsImported", 0)), "HTTP /imp CSV import"

    def fts_setup(self, conn):
        return []

    def after_load(self, conn, tables):
        return []

    def max_connections(self, conn):
        return None

    def replication_status(self, conn):
        return {}
