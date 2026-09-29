"""Oracle Database via python-oracledb (thin mode, no Instant Client needed)."""
from __future__ import annotations

import datetime as dt
import decimal
from pathlib import Path
from typing import Any

import oracledb

from ..config import Target
from ..datagen import read_csv_rows
from ..dialects import bind, render
from ..schema import Table
from .base import Conn, Engine, EngineError


class OraConn(Conn):
    paramstyle = "numeric"

    def execute(self, sql, params=None, *, rendered=False, fetch=True):
        s = sql if rendered else self.prepare_sql(sql, len(params) if params else 0)
        s = s.rstrip().rstrip(";") if not s.rstrip().endswith("END;") else s
        cur = self.raw.cursor()
        try:
            if not params and s.upper().rstrip().endswith("INTO ?"):
                # RETURNING ... INTO <bind>: Oracle returns generated values through an OUT bind variable
                out = cur.var(int)
                cur.execute(s[: s.rfind("?")] + ":1", [out])
                if self.autocommit:
                    self.raw.commit()
                return [(out.getvalue(),)]
            if params:
                cur.execute(s, [int(p) if isinstance(p, bool) else p for p in params])
            else:
                cur.execute(s)
            rows = None
            if fetch and cur.description is not None:
                rows = [tuple(r) for r in cur.fetchall()]
            if self.autocommit:
                self.raw.commit()
            return rows
        finally:
            cur.close()

    def executemany(self, sql, seq, *, rendered=False):
        s = sql if rendered else self.prepare_sql(sql, len(seq[0]) if seq else 0)
        s = s.rstrip().rstrip(";")
        cur = self.raw.cursor()
        try:
            cur.executemany(s, [[int(p) if isinstance(p, bool) else p for p in row] for row in seq])
            if self.autocommit:
                self.raw.commit()
        finally:
            cur.close()

    def set_autocommit(self, on):
        self.autocommit = on
        self.raw.autocommit = on

    def server_version(self):
        try:
            return self.execute("SELECT banner_full FROM v$version", rendered=True)[0][0]
        except Exception:  # noqa: BLE001
            try:
                return self.raw.version
            except Exception as e:  # noqa: BLE001
                return f"unknown ({e})"

    def role(self):
        try:
            r = self.execute("SELECT database_role, open_mode FROM v$database", rendered=True)[0]
            return "primary" if r[0] == "PRIMARY" else "replica"
        except Exception:  # noqa: BLE001
            return self.target.role

    def explain(self, sql, params, analyze):
        rendered = render(sql, self.dialect)
        s = bind(rendered, self.paramstyle, len(params) if params else 0).rstrip().rstrip(";")
        cur = self.raw.cursor()
        try:
            if analyze:
                # execute with statistics-level hint, then pull the actual plan from the cursor cache
                hinted = s.replace("SELECT", "SELECT /*+ GATHER_PLAN_STATISTICS */", 1) if s.lstrip().upper().startswith("SELECT") else s
                cur.execute(hinted, [int(p) if isinstance(p, bool) else p for p in params]) if params else cur.execute(hinted)
                if cur.description:
                    cur.fetchall()
                cur.execute("SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST'))")
                return "\n".join(r[0] for r in cur.fetchall() if r[0] is not None)
            cur.execute("EXPLAIN PLAN FOR " + s, [int(p) if isinstance(p, bool) else p for p in params]) if params else cur.execute("EXPLAIN PLAN FOR " + s)
            cur.execute("SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY(NULL, NULL, 'TYPICAL'))")
            return "\n".join(r[0] for r in cur.fetchall() if r[0] is not None)
        finally:
            cur.close()


class OracleEngine(Engine):
    driver = "oracledb"

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        dsn = f"{target.host}:{target.port}/{target.extra.get('service', 'FREEPDB1')}"
        raw = oracledb.connect(user=target.user, password=target.password, dsn=dsn, tcp_connect_timeout=timeout)
        raw.autocommit = autocommit
        raw.stmtcachesize = 50
        c = OraConn(self, raw, target)
        c.autocommit = autocommit
        return c

    def adapt_value(self, v, ptype):
        if isinstance(v, bool):
            return int(v)
        return v

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 20000):
        cols = table.colnames
        sql = f"INSERT INTO {table.name} ({', '.join(cols)}) VALUES ({', '.join(f':{i+1}' for i in range(len(cols)))})"
        n = 0
        conn.set_autocommit(False)
        cur = conn.raw.cursor()
        buf: list[list] = []
        try:
            for row in read_csv_rows(csv_path, table.name):
                buf.append([self.adapt_value(v, c.ptype) for v, c in zip(row, table.cols)])
                if len(buf) >= batch:
                    cur.executemany(sql, buf)
                    n += len(buf)
                    buf = []
            if buf:
                cur.executemany(sql, buf)
                n += len(buf)
            conn.raw.commit()
        finally:
            cur.close()
            conn.set_autocommit(True)
        return n, "executemany (array binding, 20k/batch)"

    def fts_setup(self, conn: Conn) -> list[str]:
        s = "CREATE INDEX ix_products_ctx ON products (description) INDEXTYPE IS CTXSYS.CONTEXT"
        try:
            conn.execute(s, rendered=True, fetch=False)
            return [s]
        except Exception as e:  # noqa: BLE001
            # Oracle Text (CTXSYS) is not shipped in the *slim* Free image: fall back to LIKE so q13 still runs
            self.dialect.fts_kind = "like-fallback"
            self.dialect.m_fts = lambda col, words: " AND ".join(f"{col} LIKE '%{w}%'" for w in words.split())
            return [f"FAILED {s}: {str(e)[:120]} -> LIKE fallback used"]

    def max_connections(self, conn: Conn) -> int | None:
        try:
            return int(conn.execute("SELECT value FROM v$parameter WHERE name = 'sessions'", rendered=True)[0][0])
        except Exception:  # noqa: BLE001
            try:
                return int(conn.execute("SELECT value FROM v$parameter WHERE name = 'processes'", rendered=True)[0][0])
            except Exception:  # noqa: BLE001
                return None

    def replication_status(self, conn: Conn) -> dict[str, Any]:
        try:
            r = conn.execute("SELECT database_role, open_mode, protection_mode FROM v$database", rendered=True)[0]
            return {"database_role": r[0], "open_mode": r[1], "protection_mode": r[2]}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}

    def error_code(self, exc: BaseException) -> str | None:
        args = getattr(exc, "args", ())
        if args and hasattr(args[0], "full_code"):
            return args[0].full_code
        if args and hasattr(args[0], "code"):
            return f"ORA-{args[0].code:05d}"
        return super().error_code(exc)
