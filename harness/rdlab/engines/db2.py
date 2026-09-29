"""IBM Db2 via ibm_db (DB-API wrapper ibm_db_dbi)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Target
from ..datagen import read_csv_rows
from ..dialects import bind, render
from ..schema import Table
from .base import Conn, Engine


class Db2Conn(Conn):
    paramstyle = "qmark"

    def _adapt_params(self, params):
        return tuple(int(p) if isinstance(p, bool) else p for p in params)

    def set_autocommit(self, on):
        self.autocommit = on
        self.raw.set_autocommit(on)

    def server_version(self):
        try:
            return self.execute("SELECT service_level FROM SYSIBMADM.ENV_INST_INFO", rendered=True)[0][0]
        except Exception as e:  # noqa: BLE001
            return f"unknown ({e})"

    def role(self):
        return self.target.role

    def explain(self, sql, params, analyze):
        """EXPLAIN PLAN + db2exfmt-style summary via the EXPLAIN tables (requires SYSTOOLS explain tables)."""
        rendered = render(sql, self.dialect)
        s = bind(rendered, self.paramstyle, len(params) if params else 0)
        try:
            self.execute("CALL SYSPROC.SYSINSTALLOBJECTS('EXPLAIN', 'C', CAST(NULL AS VARCHAR(128)), CAST(NULL AS VARCHAR(128)))", rendered=True, fetch=False)
        except Exception:  # noqa: BLE001
            pass
        self.execute("DELETE FROM EXPLAIN_OPERATOR", rendered=True, fetch=False)
        cur = self.raw.cursor()
        try:
            cur.execute("EXPLAIN PLAN FOR " + s, self._adapt_params(params)) if params else cur.execute("EXPLAIN PLAN FOR " + s)
        finally:
            cur.close()
        rows = self.execute("SELECT operator_id, operator_type, total_cost, io_cost, cpu_cost FROM EXPLAIN_OPERATOR ORDER BY operator_id", rendered=True)
        streams = self.execute("SELECT source_id, target_id, stream_count, object_name FROM EXPLAIN_STREAM ORDER BY target_id, source_id", rendered=True)
        out = ["operators: " + "; ".join(f"{r[0]}:{r[1].strip()} cost={float(r[2]):.1f}" for r in rows or [])]
        out.append("streams (src->tgt rows obj): " + "; ".join(f"{r[0]}->{r[1]} {float(r[2]):.0f} {(r[3] or '').strip()}" for r in streams or []))
        if analyze:
            out.append("(Db2 has no per-statement actual-row plan output outside of activity event monitors; costs are estimates)")
        return "\n".join(out)


class Db2Engine(Engine):
    driver = "ibm_db"
    manual_autocommit = False

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        import ibm_db
        import ibm_db_dbi
        cs = (f"DATABASE={target.database};HOSTNAME={target.host};PORT={target.port};PROTOCOL=TCPIP;UID={target.user};PWD={target.password};"
              f"CONNECTTIMEOUT={int(timeout)};")
        raw = ibm_db_dbi.connect(cs, "", "")
        raw.set_autocommit(autocommit)
        c = Db2Conn(self, raw, target)
        c.autocommit = autocommit
        return c

    def adapt_value(self, v, ptype):
        if isinstance(v, bool):
            return int(v)
        return v

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 5000):
        """Commit every batch: the Community Edition's default transaction log (LOGFILSIZ x LOGPRIMARY) is far too
        small for a 1M-row transaction (SQL0964 'transaction log full')."""
        cols = table.colnames
        sql = f"INSERT INTO {table.name} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        n = 0
        conn.set_autocommit(False)
        buf: list[tuple] = []
        try:
            for row in read_csv_rows(csv_path, table.name):
                buf.append(tuple(self.adapt_value(v, c.ptype) for v, c in zip(row, table.cols)))
                if len(buf) >= batch:
                    conn.executemany(sql, buf)
                    conn.commit()
                    n += len(buf)
                    buf = []
            if buf:
                conn.executemany(sql, buf)
                conn.commit()
                n += len(buf)
        finally:
            conn.set_autocommit(True)
        return n, "executemany (commit per 5000 rows)"

    def fts_setup(self, conn: Conn) -> list[str]:
        return ["(Db2 Text Search not enabled in the community container; LIKE fallback used)"]

    def max_connections(self, conn: Conn) -> int | None:
        try:
            r = conn.execute("SELECT value FROM SYSIBMADM.DBMCFG WHERE name = 'max_connections'", rendered=True)
            return int(r[0][0]) if r and str(r[0][0]).lstrip("-").isdigit() else None
        except Exception:  # noqa: BLE001
            return None
