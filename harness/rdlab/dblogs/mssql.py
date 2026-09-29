"""SQL Server logging probe.

init/setup.sh creates: an Extended Events session `rdlab_slow` (sql_statement_completed with duration >= 100 ms ->
/var/opt/mssql/log/rdlab_slow*.xel), a second session `rdlab_all` (no duration filter, started only for the overhead test),
a SQL Server Audit `rdlab_audit` (logins + schema changes + DELETE on lab -> /var/opt/mssql/log/rdlab_audit*.sqlaudit) and
Query Store on the lab database. The errorlog lives in /var/opt/mssql/log/errorlog.
"""
from __future__ import annotations

import re
from typing import Any

from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe


class MSLogProbe(LogProbe):
    sink = "Extended Events session rdlab_slow (sql_statement_completed, duration >= 100 ms) -> rdlab_slow*.xel"
    audit_mechanism = "SQL Server Audit rdlab_audit (SCHEMA_OBJECT_CHANGE_GROUP, DELETE on lab, logins) -> *.sqlaudit"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.log_dir = self.lconf.get("dir", "/var/opt/mssql/log")

    def settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        c = self.engine.connect(self.primary)
        try:
            out["errorlog"] = c.execute("SELECT CONVERT(varchar(200), SERVERPROPERTY('ErrorLogFileName'))", rendered=True)[0][0]
            out["xe_sessions"] = [r[0] for r in c.execute("SELECT name FROM sys.dm_xe_sessions", rendered=True)]
            out["audits"] = [(r[0], r[1]) for r in c.execute("SELECT name, is_state_enabled FROM sys.server_audits", rendered=True)]
            out["query_store"] = [str(r) for r in c.execute("SELECT actual_state_desc, query_capture_mode_desc, max_storage_size_mb FROM sys.database_query_store_options", rendered=True)]
            out["configurations"] = {r[0]: r[1] for r in c.execute("SELECT name, value_in_use FROM sys.configurations WHERE name IN ('default trace enabled','blocked process threshold (s)','remote query timeout (s)')", rendered=True)}
        finally:
            c.close()
        return out

    def slow_query(self, mk: str) -> str:
        return f"WAITFOR DELAY '00:00:00.300'; SELECT 1 AS {mk}"

    def _xe_find(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT TOP 1 CAST(event_data AS nvarchar(max)) FROM sys.fn_xe_file_target_read_file(N'{self.log_dir}/rdlab_slow*.xel', NULL, NULL, NULL) "
                      f"WHERE CAST(event_data AS nvarchar(max)) LIKE N'%{mk}%'")
        if not rows:
            return {"found": False}
        xml = rows[0][0]
        m = re.search(r'name="duration".*?<value>(\d+)</value>', xml, re.S)
        st = re.search(r'name="statement".*?<value>(.*?)</value>', xml, re.S)
        return {"found": True, "sample": f"duration_us={m.group(1) if m else '?'} statement={(st.group(1) if st else '')[:150]}", "duration_ms": round(int(m.group(1)) / 1000, 1) if m else None}

    def find_slow(self, mk: str) -> dict[str, Any]:
        return self._xe_find(mk)

    def full_logging(self, on: bool) -> str | None:
        self.q(f"ALTER EVENT SESSION rdlab_all ON SERVER STATE = {'START' if on else 'STOP'}", fetch=False)
        return "ALTER EVENT SESSION rdlab_all ON SERVER STATE = START (sql_statement_completed without a duration filter -> rdlab_all*.xel)"

    def audit_setup(self) -> str | None:
        return "configured at stack init (server audit + database audit specification)"

    def audit_find(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT TOP 1 event_time, action_id, server_principal_name, database_name, statement FROM sys.fn_get_audit_file(N'{self.log_dir}/rdlab_audit*.sqlaudit', DEFAULT, DEFAULT) "
                      f"WHERE statement LIKE N'%audit_{mk}%' ORDER BY event_time DESC")
        if rows:
            r = rows[0]
            return {"found": True, "sample": f"time={r[0]} action={r[1]} principal={r[2]} db={r[3]} statement={str(r[4])[:120]}"}
        return {"found": False}

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, f"{self.log_dir}/errorlog")]

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            rows = self.q("SELECT TOP 5 rs.count_executions, CAST(rs.avg_duration/1000.0 AS decimal(12,3)), LEFT(qt.query_sql_text, 90) "
                          "FROM sys.query_store_runtime_stats rs JOIN sys.query_store_plan p ON p.plan_id = rs.plan_id "
                          "JOIN sys.query_store_query q ON q.query_id = p.query_id JOIN sys.query_store_query_text qt ON qt.query_text_id = q.query_text_id "
                          "ORDER BY rs.count_executions DESC")
            out["query_store_top5"] = [{"executions": r[0], "avg_ms": float(r[1]), "query": r[2]} for r in rows or []]
            out["xe_files"] = [r[0] for r in self.q(f"SELECT DISTINCT file_name FROM sys.fn_xe_file_target_read_file(N'{self.log_dir}/rdlab_*.xel', NULL, NULL, NULL)") or []][:6]
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:150]
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return MSLogProbe(engine, cfg, log=log)
