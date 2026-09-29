"""Oracle Database Free logging probe.

Slow queries: V$SQL (shared-pool statistics per SQL) plus session SQL trace (DBMS_SESSION.SESSION_TRACE_ENABLE ->
<diag>/trace/*.trc with elapsed micros per execution); "log everything" = DBMS_MONITOR.DATABASE_TRACE_ENABLE.
Audit: unified auditing (CREATE AUDIT POLICY ... ACTIONS CREATE TABLE, DROP TABLE, DELETE ON lab.inventory; AUDIT POLICY)
read back from UNIFIED_AUDIT_TRAIL. Alert log: <diag>/trace/alert_FREE.log (+ alert/log.xml).
"""
from __future__ import annotations

import re
from typing import Any

from .. import dockerctl
from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe


class OraLogProbe(LogProbe):
    sink = "session SQL trace (DBMS_SESSION.SESSION_TRACE_ENABLE -> .trc) + V$SQL elapsed_time/executions"
    audit_mechanism = "unified auditing: AUDIT POLICY rdlab_pol (CREATE/DROP TABLE, DELETE ON lab.inventory) -> UNIFIED_AUDIT_TRAIL"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.pdb = self.lconf.get("pdb", "FREEPDB1")
        self.trace_dir: str | None = None
        self.trace_file: str | None = None

    def _sysdba(self, sql: str, *, pdb: bool = True) -> str:
        pre = f"ALTER SESSION SET CONTAINER = {self.pdb};\n" if pdb else ""
        script = f"""cat <<'SQL' | sqlplus -s / as sysdba
SET HEADING OFF FEEDBACK OFF PAGESIZE 0 LINESIZE 400 TRIMSPOOL ON
{pre}{sql}
EXIT
SQL"""
        rc, out, err = dockerctl.exec_in(self.container, ["bash", "-c", script], timeout=120)
        return out + err

    def settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        # the app user needs ALTER SESSION for SQL trace and the catalog role to read v$diag_info
        self._sysdba(f"GRANT ALTER SESSION, SELECT_CATALOG_ROLE TO {self.primary.user};")
        txt = self._sysdba("SELECT name || '=' || value FROM v$diag_info WHERE name IN ('Diag Trace', 'Diag Alert', 'Default Trace File');", pdb=False)
        for line in txt.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        self.trace_dir = out.get("Diag Trace")
        txt = self._sysdba("SELECT name || '=' || value FROM v$parameter WHERE name IN ('audit_trail', 'unified_audit_common_systemlog', 'sql_trace', 'timed_statistics', 'statistics_level', 'undo_retention', 'log_archive_dest_1');", pdb=False)
        for line in txt.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        out["log_mode"] = self._sysdba("SELECT log_mode FROM v$database;", pdb=False).strip()
        out["unified_auditing"] = self._sysdba("SELECT value FROM v$option WHERE parameter = 'Unified Auditing';", pdb=False).strip()
        return out

    def slow_query(self, mk: str) -> str:
        return f"SELECT COUNT(*) AS {mk} FROM events e1 JOIN events e2 ON e1.customer_id = e2.customer_id WHERE e1.id < 100000 AND e2.id < 100000"

    def fast_query(self, mk: str) -> str:
        return f"SELECT 1 AS {mk} FROM dual"

    def run_query(self, sql: str) -> None:
        """Run the query in a session with SQL trace switched on (the trace file becomes the slow-query evidence)."""
        c = self.engine.connect(self.primary)
        try:
            c.execute("ALTER SESSION SET TRACEFILE_IDENTIFIER = 'rdlab'", rendered=True, fetch=False)
            c.execute("BEGIN DBMS_SESSION.SESSION_TRACE_ENABLE(waits => TRUE, binds => FALSE); END;", rendered=True, fetch=False)
            c.execute(sql, rendered=True)
            c.execute("BEGIN DBMS_SESSION.SESSION_TRACE_DISABLE; END;", rendered=True, fetch=False)
            try:
                self.trace_file = c.execute("SELECT value FROM v$diag_info WHERE name = 'Default Trace File'", rendered=True)[0][0]
            except Exception:  # noqa: BLE001
                self.trace_file = None
        finally:
            c.close()

    def find_slow(self, mk: str) -> dict[str, Any]:
        out: dict[str, Any] = {"found": False}
        d = self.trace_dir or "/opt/oracle/diag/rdbms/free/FREE/trace"
        rc, files, _ = dockerctl.exec_in(self.container, ["bash", "-c", f"grep -l '{mk}' {d}/*rdlab*.trc 2>/dev/null | head -1"])
        f = files.strip().splitlines()[0] if files.strip() else None
        if f:
            txt = self.read(f, tail_bytes=4_000_000)
            i = txt.find(mk)
            m = re.search(r"PARSE #(\d+):", txt[i:i + 4000]) if i >= 0 else None
            if m:
                cur = m.group(1)
                # the trace records every statement of the traced session; "slow" = EXEC + FETCH elapsed (e=micros) over the threshold
                elapsed = sum(int(x) for x in re.findall(rf"(?:EXEC|FETCH) #{cur}:c=\d+,e=(\d+)", txt[i:]))
                out.update({"trace_file": f, "trace_elapsed_ms": round(elapsed / 1000, 2), "sample": txt[i:i + 500].strip()})
                if elapsed / 1000 >= self.slow_threshold_ms:
                    out["found"] = True
        rows = self._sysdba(f"SELECT executions || '|' || ROUND(elapsed_time/1000, 1) || '|' || SUBSTR(sql_text, 1, 90) FROM v$sql WHERE sql_text LIKE '%{mk}%' AND sql_text NOT LIKE '%v$sql%' AND ROWNUM = 1;")
        if "|" in rows:
            ex, el, txt2 = rows.strip().split("|", 2)
            out["v$sql"] = {"executions": int(ex), "elapsed_ms_total": float(el), "sql": txt2}
            out["duration_ms"] = float(el)
            if float(el) >= self.slow_threshold_ms:
                out["found"] = True
                out.setdefault("sample", f"v$sql: {rows.strip()[:300]}")
        return out

    def full_logging(self, on: bool) -> str | None:
        if on:
            self._sysdba("BEGIN DBMS_MONITOR.DATABASE_TRACE_ENABLE(waits => FALSE, binds => FALSE); END;\n/", pdb=False)
            return "DBMS_MONITOR.DATABASE_TRACE_ENABLE (SQL trace for every session -> one .trc per server process)"
        self._sysdba("BEGIN DBMS_MONITOR.DATABASE_TRACE_DISABLE; END;\n/", pdb=False)
        return None

    def audit_setup(self) -> str | None:
        self._sysdba("""BEGIN EXECUTE IMMEDIATE 'NOAUDIT POLICY rdlab_pol'; EXCEPTION WHEN OTHERS THEN NULL; END;
/
BEGIN EXECUTE IMMEDIATE 'DROP AUDIT POLICY rdlab_pol'; EXCEPTION WHEN OTHERS THEN NULL; END;
/
CREATE AUDIT POLICY rdlab_pol ACTIONS CREATE TABLE, DROP TABLE, DELETE ON lab.inventory;
AUDIT POLICY rdlab_pol;""")
        return "CREATE AUDIT POLICY rdlab_pol ACTIONS CREATE TABLE, DROP TABLE, DELETE ON lab.inventory; AUDIT POLICY rdlab_pol"

    def audit_find(self, mk: str) -> dict[str, Any]:
        txt = self._sysdba(f"SELECT TO_CHAR(event_timestamp, 'HH24:MI:SS.FF3') || ' ' || dbusername || ' ' || action_name || ' ' || object_schema || '.' || object_name || ' ' || SUBSTR(sql_text, 1, 80) "
                           f"FROM unified_audit_trail WHERE object_name = UPPER('audit_{mk}') AND ROWNUM <= 2;")
        line = txt.strip().splitlines()[0] if txt.strip() else ""
        return {"found": bool(line and "ORA-" not in line), "sample": line[:300]}

    def audit_teardown(self) -> None:
        self._sysdba("NOAUDIT POLICY rdlab_pol;")

    def log_dirs(self) -> list[tuple[str, str]]:
        return [(self.container, self.trace_dir or "/opt/oracle/diag/rdbms/free/FREE/trace")]

    def log_files(self) -> list[tuple[str, str]]:
        d = self.trace_dir or "/opt/oracle/diag/rdbms/free/FREE/trace"
        return [(self.container, f"{d}/alert_*.log"), (self.container, f"{d}/*rdlab*.trc")]

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        txt = self._sysdba("SELECT executions || '|' || ROUND(elapsed_time/1000) || '|' || SUBSTR(sql_text, 1, 80) FROM (SELECT * FROM v$sql WHERE parsing_schema_name = 'LAB' ORDER BY elapsed_time DESC) WHERE ROWNUM <= 5;")
        out["v$sql_top5"] = [{"executions": l.split("|")[0], "elapsed_ms": l.split("|")[1], "sql": l.split("|", 2)[2]} for l in txt.strip().splitlines() if l.count("|") >= 2]
        out["unified_audit_rows"] = self._sysdba("SELECT COUNT(*) FROM unified_audit_trail WHERE unified_audit_policies LIKE '%RDLAB_POL%';").strip()
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return OraLogProbe(engine, cfg, log=log)
