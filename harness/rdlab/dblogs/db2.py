"""IBM Db2 logging probe.

Slow statements: the package cache (MON_GET_PKG_CACHE_STMT: per-statement execution counts and times) plus an activity
event monitor written to tables (CREATE EVENT MONITOR ... FOR ACTIVITIES WRITE TO TABLE with COLLECT ACTIVITY DATA on the
default workload) which is the persistent statement log; "log everything" = COLLECT ACTIVITY DATA ON ALL WITH DETAILS.
Audit: db2audit policy (CREATE AUDIT POLICY ... CATEGORIES EXECUTE STATUS BOTH; AUDIT DATABASE USING POLICY), flushed and
extracted with db2audit archive/extract. Diagnostics: db2diag.log (DIAGLEVEL) under the instance's db2dump directory.
"""
from __future__ import annotations

import re
from typing import Any

from .. import dockerctl
from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe


class Db2LogProbe(LogProbe):
    sink = "MON_GET_PKG_CACHE_STMT (package cache) + activity event monitor RDLAB_ACT (WRITE TO TABLE)"
    audit_mechanism = "db2audit policy RDLAB_POL (EXECUTE) -> db2audit archive/extract"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.inst = self.lconf.get("instance", "db2inst1")
        self.diag = self.lconf.get("diag_dir", f"/database/config/{self.inst}/sqllib/db2dump/DIAG0000")

    def _clp(self, cmds: str, *, timeout: int = 300) -> str:
        script = "\n".join(f"db2 -v {c!r}" if not c.startswith("!") else c[1:] for c in cmds.strip().splitlines())
        q = "'" + script.replace("'", "'\"'\"'") + "'"
        rc, out, err = dockerctl.exec_in(self.container, ["bash", "-c", f"su - {self.inst} -c {q}"], timeout=timeout)
        return out + err

    def settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        txt = self._clp(f"get dbm cfg\nget db cfg for {self.primary.database}\n!db2audit describe")
        for key in ("DIAGLEVEL", "DIAGPATH", "LOGARCHMETH1", "TRACKMOD", "MON_ACT_METRICS", "MON_REQ_METRICS", "MON_DEADLOCK", "MON_LOCKWAIT", "NOTIFYLEVEL"):
            m = re.search(rf"\({key}\)\s*=\s*(.*)", txt) or re.search(rf"{key}\s*=\s*(.*)", txt)
            if m:
                out[key] = m.group(1).strip()[:80]
        m = re.search(r"Log path for audit files\s*:\s*(\S+)", txt)
        out["audit_log_path"] = m.group(1) if m else None
        # activity event monitor (idempotent)
        self.q_many_safe([
            "CREATE EVENT MONITOR RDLAB_ACT FOR ACTIVITIES WRITE TO TABLE AUTOSTART",
            "SET EVENT MONITOR RDLAB_ACT STATE 1",
            "ALTER WORKLOAD SYSDEFAULTUSERWORKLOAD COLLECT ACTIVITY DATA ON ALL DATABASE PARTITIONS WITH DETAILS",
        ])
        out["event_monitor"] = "RDLAB_ACT (activities -> ACTIVITYSTMT_RDLAB_ACT)"
        return out

    def q_many_safe(self, stmts: list[str]) -> list[str]:
        errs = []
        c = self.engine.connect(self.primary)
        try:
            for s in stmts:
                try:
                    c.execute(s, rendered=True, fetch=False)
                except Exception as e:  # noqa: BLE001
                    errs.append(str(e)[:120])
        finally:
            c.close()
        return errs

    def slow_query(self, mk: str) -> str:
        return f"SELECT COUNT(*) AS {mk} FROM events e1 JOIN events e2 ON e1.customer_id = e2.customer_id WHERE e1.id < 100000 AND e2.id < 100000"

    def fast_query(self, mk: str) -> str:
        return f"SELECT 1 AS {mk} FROM sysibm.sysdummy1"

    def find_slow(self, mk: str) -> dict[str, Any]:
        out: dict[str, Any] = {"found": False}
        rows = self.q(f"SELECT NUM_EXECUTIONS, TOTAL_ACT_TIME, TOTAL_CPU_TIME, VARCHAR(STMT_TEXT, 100) FROM TABLE(MON_GET_PKG_CACHE_STMT(NULL, NULL, NULL, -2)) "
                      f"WHERE STMT_TEXT LIKE '%{mk}%' AND TOTAL_ACT_TIME >= {self.slow_threshold_ms} FETCH FIRST 1 ROWS ONLY")
        if rows:
            r = rows[0]
            out.update({"found": True, "sample": f"executions={r[0]} total_act_time_ms={r[1]} cpu_us={r[2]} stmt={r[3]}", "duration_ms": int(r[1])})
        try:
            self.q("FLUSH EVENT MONITOR RDLAB_ACT", fetch=False)
            rows = self.q(f"SELECT s.STMT_TEXT, a.TIME_COMPLETED, a.ACT_EXEC_TIME FROM ACTIVITYSTMT_RDLAB_ACT s JOIN ACTIVITY_RDLAB_ACT a ON a.APPL_ID = s.APPL_ID AND a.UOW_ID = s.UOW_ID AND a.ACTIVITY_ID = s.ACTIVITY_ID "
                          f"WHERE s.STMT_TEXT LIKE '%{mk}%' FETCH FIRST 1 ROWS ONLY")
            if rows:
                out["event_monitor"] = f"completed={rows[0][1]} act_exec_time_us={rows[0][2]}"
        except Exception as e:  # noqa: BLE001
            out["event_monitor_error"] = str(e)[:120]
        return out

    def full_logging(self, on: bool) -> str | None:
        if on:
            self.q_many_safe(["ALTER WORKLOAD SYSDEFAULTUSERWORKLOAD COLLECT ACTIVITY DATA ON ALL DATABASE PARTITIONS WITH DETAILS"])
            return "COLLECT ACTIVITY DATA ON ALL WITH DETAILS on the default workload (every statement into the RDLAB_ACT event monitor tables)"
        self.q_many_safe(["ALTER WORKLOAD SYSDEFAULTUSERWORKLOAD COLLECT ACTIVITY DATA NONE"])
        return None

    def audit_setup(self) -> str | None:
        # through the CLP (autocommit): AUDIT DATABASE USING POLICY issued over the driver never showed up in SYSCAT.AUDITUSE
        self._clp(f"""connect to {self.primary.database}
CREATE AUDIT POLICY RDLAB_POL CATEGORIES EXECUTE STATUS BOTH, OBJMAINT STATUS BOTH ERROR TYPE NORMAL
AUDIT DATABASE USING POLICY RDLAB_POL
terminate""")
        return "CREATE AUDIT POLICY RDLAB_POL CATEGORIES EXECUTE, OBJMAINT; AUDIT DATABASE USING POLICY RDLAB_POL (SECADM via CLP)"

    def audit_find(self, mk: str) -> dict[str, Any]:
        db = self.primary.database
        d = self.lconf.get("audit_dir", "/backups/audit")
        # archives accumulate in {d} (each archive moves the active log away; deleting them between polls loses records)
        txt = self._clp(f"""!mkdir -p {d}/out
!db2audit flush
!db2audit archive database {db} to {d}
!db2audit extract delasc to {d}/out from files {d}/db2audit.db.*.log.*
!grep -aih 'audit_{mk}' {d}/out/objmaint.del {d}/out/execute.del | head -2""")
        lines = [l for l in txt.splitlines() if f"audit_{mk}" in l.lower()]
        return {"found": bool(lines), "sample": (lines[0] if lines else "")[:400]}

    def audit_teardown(self) -> None:
        self._clp(f"connect to {self.primary.database}\nAUDIT DATABASE REMOVE POLICY\nterminate")

    def log_bytes(self) -> int:
        try:
            rows = self.q("SELECT COUNT(*) FROM ACTIVITYSTMT_RDLAB_ACT")
            return int(rows[0][0]) * 300
        except Exception:  # noqa: BLE001
            return 0

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, f"{self.diag}/db2diag.log"), (self.container, f"{self.diag}/{self.inst}.nfy")]

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            rows = self.q("SELECT NUM_EXECUTIONS, TOTAL_ACT_TIME, VARCHAR(STMT_TEXT, 90) FROM TABLE(MON_GET_PKG_CACHE_STMT(NULL, NULL, NULL, -2)) "
                          "WHERE STMT_TEXT NOT LIKE '%MON_GET%' ORDER BY TOTAL_ACT_TIME DESC FETCH FIRST 5 ROWS ONLY")
            out["package_cache_top5"] = [{"executions": r[0], "total_ms": r[1], "stmt": r[2]} for r in rows or []]
            out["activity_rows"] = int(self.q("SELECT COUNT(*) FROM ACTIVITYSTMT_RDLAB_ACT")[0][0])
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:150]
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return Db2LogProbe(engine, cfg, log=log)
