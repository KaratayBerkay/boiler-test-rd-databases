"""Microsoft SQL Server via pymssql (FreeTDS)."""
from __future__ import annotations

import datetime as dt
import decimal
from pathlib import Path
from typing import Any

import pymssql

from ..config import Target
from ..datagen import read_csv_rows
from ..dialects import render, bind
from ..schema import Table
from .base import Conn, Engine, EngineError


class MSConn(Conn):
    paramstyle = "format"

    def _adapt_params(self, params):
        return tuple(int(p) if isinstance(p, bool) else p for p in params)

    def set_autocommit(self, on):
        self.autocommit = on
        self.raw.autocommit(on)          # pymssql: autocommit is a method

    def server_version(self):
        try:
            v = self.execute("SELECT @@VERSION", rendered=True)[0][0]
            return " ".join(str(v).split())[:120]
        except Exception as e:  # noqa: BLE001
            return f"unknown ({e})"

    def role(self):
        try:
            r = self.execute("SELECT CAST(ISNULL(DATABASEPROPERTYEX(DB_NAME(), 'Updateability'), 'READ_WRITE') AS NVARCHAR(20))", rendered=True)[0][0]
            return "replica" if r == "READ_ONLY" else "primary"
        except Exception:  # noqa: BLE001
            return self.target.role

    def explain(self, sql, params, analyze):
        """SHOWPLAN_XML (estimated) or STATISTICS XML (actual) — returned as the XML text (truncated by caller)."""
        rendered = render(sql, self.dialect)
        s = bind(rendered, self.paramstyle, len(params) if params else 0)
        cur = self.raw.cursor()
        try:
            if analyze:
                cur.execute("SET STATISTICS XML ON")
                cur.execute(s, self._adapt_params(params)) if params else cur.execute(s)
                # first result set = query rows, next = plan
                out = None
                while True:
                    try:
                        rows = cur.fetchall()
                    except Exception:  # noqa: BLE001
                        rows = []
                    if rows and isinstance(rows[0][0], str) and rows[0][0].lstrip().startswith("<ShowPlanXML"):
                        out = rows[0][0]
                    if not cur.nextset():
                        break
                cur.execute("SET STATISTICS XML OFF")
                return _summarize_plan(out) if out else None
            cur.execute("SET SHOWPLAN_XML ON")
            try:
                cur.execute(s, self._adapt_params(params)) if params else cur.execute(s)
                rows = cur.fetchall()
                out = rows[0][0] if rows else None
            finally:
                cur.execute("SET SHOWPLAN_XML OFF")
            return _summarize_plan(out) if out else None
        finally:
            cur.close()


def _summarize_plan(xml: str) -> str:
    """Pull the interesting bits out of SHOWPLAN XML: operators with estimated/actual rows, warnings."""
    import re
    ops = re.findall(r'<RelOp[^>]*PhysicalOp="([^"]+)"[^>]*EstimateRows="([^"]+)"[^>]*', xml)
    actual = re.findall(r'ActualRows="(\d+)"', xml)
    warns = re.findall(r"<(SpillToTempDb|PlanAffectingConvert|NoJoinPredicate|MissingIndex)[^>]*>", xml)
    lines = ["operators (PhysicalOp, EstimateRows): " + ", ".join(f"{o}={float(r):.0f}" for o, r in ops[:25])]
    if actual:
        lines.append("actual rows per operator: " + ", ".join(actual[:25]))
    if warns:
        lines.append("warnings: " + ", ".join(sorted(set(warns))))
    m = re.search(r'StatementSubTreeCost="([^"]+)"', xml)
    if m:
        lines.append(f"subtree cost: {m.group(1)}")
    m = re.search(r'<QueryTimeStats[^>]*CpuTime="(\d+)"[^>]*ElapsedTime="(\d+)"', xml)
    if m:
        lines.append(f"cpu_ms={m.group(1)} elapsed_ms={m.group(2)}")
    lines.append("raw: " + xml[:2500])
    return "\n".join(lines)


class MSSQLEngine(Engine):
    driver = "pymssql"

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        raw = pymssql.connect(server=target.host, port=str(target.port), user=target.user, password=target.password, database=target.database or "master",
                              login_timeout=int(timeout), timeout=600, autocommit=autocommit, charset="UTF-8", tds_version="7.4", as_dict=False,
                              appname="rdlab")
        c = MSConn(self, raw, target)
        c.autocommit = autocommit
        return c

    def adapt_value(self, v, ptype):
        if isinstance(v, bool):
            return int(v)
        return v

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 1000):
        """Multi-row INSERT (<= 2100 parameters per statement). pymssql's bulk_copy (BCP) sends Python str as raw
        bytes, which lands as UTF-16 mojibake in NVARCHAR columns, so it is not used."""
        cols = table.colnames
        n = 0
        rows_per_stmt = max(1, 2000 // len(cols))
        placeholders = "(" + ", ".join("%s" for _ in cols) + ")"
        conn.set_autocommit(False)
        buf = []
        try:
            for row in read_csv_rows(csv_path, table.name):
                buf.append(tuple(self.adapt_value(v, c.ptype) for v, c in zip(row, table.cols)))
                if len(buf) >= rows_per_stmt:
                    self._multirow(conn, table, buf, placeholders)
                    n += len(buf)
                    buf = []
                    if n % 50_000 < rows_per_stmt:
                        conn.commit()
            if buf:
                self._multirow(conn, table, buf, placeholders)
                n += len(buf)
            conn.commit()
        finally:
            conn.set_autocommit(True)
        return n, f"multi-row INSERT ({rows_per_stmt}/stmt, commit per 50k)"

    def _multirow(self, conn, table, rows, placeholders):
        sql = f"INSERT INTO {table.name} ({', '.join(table.colnames)}) VALUES " + ", ".join(placeholders for _ in rows)
        flat = tuple(x for r in rows for x in r)
        cur = conn.raw.cursor()
        cur.execute(sql, flat)
        cur.close()

    def fts_setup(self, conn: Conn) -> list[str]:
        stmts = ["CREATE FULLTEXT CATALOG lab_ft AS DEFAULT",
                 "CREATE FULLTEXT INDEX ON products (description) KEY INDEX PK__products ON lab_ft WITH CHANGE_TRACKING AUTO"]
        done = []
        try:
            pk = conn.execute("SELECT name FROM sys.indexes WHERE object_id = OBJECT_ID('products') AND is_primary_key = 1", rendered=True)[0][0]
            stmts[1] = stmts[1].replace("PK__products", pk)
        except Exception as e:  # noqa: BLE001
            return [f"FAILED to find PK index: {e}"]
        for s in stmts:
            try:
                conn.execute(s, rendered=True, fetch=False)
                done.append(s)
            except Exception as e:  # noqa: BLE001
                done.append(f"FAILED {s[:60]}: {str(e)[:120]}")
                # the mssql/server container image ships without the full-text component: fall back to LIKE
                self.dialect.fts_kind = "like-fallback"
                self.dialect.m_fts = lambda col, words: " AND ".join(f"{col} LIKE '%{w}%'" for w in words.split())
                return done + ["-> LIKE fallback used for q13"]
        import time
        for _ in range(60):
            try:
                st = conn.execute("SELECT FULLTEXTCATALOGPROPERTY('lab_ft', 'PopulateStatus')", rendered=True)[0][0]
                if st == 0:
                    break
            except Exception:  # noqa: BLE001
                break
            time.sleep(1)
        return done

    def max_connections(self, conn: Conn) -> int | None:
        try:
            return int(conn.execute("SELECT @@MAX_CONNECTIONS", rendered=True)[0][0])
        except Exception:  # noqa: BLE001
            return None

    def replication_status(self, conn: Conn) -> dict[str, Any]:
        try:
            rows = conn.execute("SELECT ag.name, ar.replica_server_name, rs.role_desc, drs.synchronization_state_desc, drs.log_send_queue_size, "
                                "drs.redo_queue_size, drs.last_commit_time FROM sys.dm_hadr_database_replica_states drs "
                                "JOIN sys.availability_replicas ar ON ar.replica_id = drs.replica_id JOIN sys.availability_groups ag ON ag.group_id = drs.group_id "
                                "JOIN sys.dm_hadr_availability_replica_states rs ON rs.replica_id = drs.replica_id", rendered=True)
            return {"hadr": [tuple(str(x) for x in r) for r in rows]} if rows else {"hadr": None}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}

    def promote(self, target: Target) -> str:
        sql = self.cfg.replication.get("promote", {}).get("sql") or "ALTER AVAILABILITY GROUP [ag1] FORCE_FAILOVER_ALLOW_DATA_LOSS"
        admin = Target(name="admin", role="admin", host=target.host, port=target.port, user="sa", password=target.extra.get("sa_password", ""), database="master")
        c = self.connect(admin)
        try:
            c.execute(sql, rendered=True, fetch=False)
        finally:
            c.close()
        return sql

    def error_code(self, exc: BaseException) -> str | None:
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int):
            return str(args[0])
        if args and isinstance(args[0], (bytes, str)):
            import re
            m = re.search(r"\((\d+),", str(args[0]))
            if m:
                return m.group(1)
        return super().error_code(exc)
