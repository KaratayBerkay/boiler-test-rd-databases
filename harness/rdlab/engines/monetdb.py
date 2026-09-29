"""MonetDB via pymonetdb."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pymonetdb

from ..config import Target
from ..datagen import read_csv_rows
from ..dialects import bind, render
from ..schema import Table
from .base import Conn, Engine


class MonetConn(Conn):
    paramstyle = "pyformat"

    def execute(self, sql, params=None, *, rendered=False, fetch=True):
        s = sql if rendered else self.prepare_sql(sql, len(params) if params else 0)
        cur = self.raw.cursor()
        try:
            if params:
                cur.execute(s, {f"p{i+1}": (int(p) if isinstance(p, bool) else p) for i, p in enumerate(params)})
            else:
                cur.execute(s)
            rows = None
            if fetch and cur.description:
                rows = [tuple(r) for r in cur.fetchall()]
            return rows
        finally:
            cur.close()

    def executemany(self, sql, seq, *, rendered=False):
        s = sql if rendered else self.prepare_sql(sql, len(seq[0]) if seq else 0)
        cur = self.raw.cursor()
        try:
            cur.executemany(s, [{f"p{i+1}": (int(p) if isinstance(p, bool) else p) for i, p in enumerate(row)} for row in seq])
        finally:
            cur.close()

    def set_autocommit(self, on):
        self.autocommit = on
        self.raw.set_autocommit(on)

    def server_version(self):
        try:
            return "MonetDB " + self.execute("SELECT value FROM sys.env() WHERE name = 'monet_version'", rendered=True)[0][0]
        except Exception as e:  # noqa: BLE001
            return f"unknown ({e})"

    def role(self):
        return self.target.role

    def explain(self, sql, params, analyze):
        rendered = render(sql, self.dialect)
        if params:
            # inline literals: PLAN/TRACE do not accept parameters
            rendered = _inline(rendered, params)
        rows = self.execute(("TRACE " if analyze else "PLAN ") + rendered, rendered=True)
        return "\n".join(" | ".join(str(c) for c in r) for r in (rows or [])[:200])


def _inline(sql: str, params) -> str:
    out, i = [], 0
    for ch in sql:
        if ch == "?" and i < len(params):
            p = params[i]
            i += 1
            out.append(str(p) if isinstance(p, (int, float)) else "'" + str(p).replace("'", "''") + "'")
        else:
            out.append(ch)
    return "".join(out)


class MonetDBEngine(Engine):
    driver = "pymonetdb"

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        raw = pymonetdb.connect(username=target.user, password=target.password, hostname=target.host, port=target.port,
                                database=target.database, autocommit=autocommit, connect_timeout=timeout)
        c = MonetConn(self, raw, target)
        c.autocommit = autocommit
        return c

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 5000):
        # COPY INTO ... FROM STDIN via pymonetdb's file upload handler
        from pymonetdb.filetransfer.uploads import Uploader  # noqa: F401
        n = sum(1 for _ in open(csv_path, "rb")) - 1
        conn.raw.set_uploader(_Uploader(csv_path.parent))
        cols = ", ".join(table.colnames)
        sql = (f"COPY {n} OFFSET 2 RECORDS INTO {table.name} ({cols}) FROM '{csv_path.name}' ON CLIENT "
               f"USING DELIMITERS ',', E'\\n', '\"' NULL AS ''")
        conn.execute(sql, rendered=True, fetch=False)
        return self.count(conn, table.name), "COPY INTO ... FROM ... ON CLIENT"

    def max_connections(self, conn: Conn) -> int | None:
        try:
            return int(conn.execute("SELECT value FROM sys.env() WHERE name = 'max_clients'", rendered=True)[0][0])
        except Exception:  # noqa: BLE001
            return None


class _Uploader:
    def __init__(self, base: Path):
        self.base = base

    def handle_upload(self, upload, filename, text_mode, skip_amount):
        p = self.base / filename
        if text_mode:
            w = upload.text_writer()
            with open(p, "r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i < skip_amount:
                        continue
                    w.write(line)
        else:
            w = upload.binary_writer()
            with open(p, "rb") as f:
                w.write(f.read())
