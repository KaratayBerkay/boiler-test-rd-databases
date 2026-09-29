"""H2 logging probe: SET QUERY_STATISTICS (INFORMATION_SCHEMA.QUERY_STATISTICS with min/max/avg execution time per
statement) and the trace file (SET TRACE_LEVEL_FILE 2 -> <db>.trace.db: every statement with `t:<ms>` and a
"slow query: N ms" line for statements over 100 ms).

H2 in server mode closes a database when its last connection goes away, and QUERY_STATISTICS is not persisted, so the
probe keeps one connection open for the whole phase."""
from __future__ import annotations

from typing import Any

from ..config import StackConfig, Target
from ..engines import Engine
from .common import LogProbe


class H2LogProbe(LogProbe):
    sink = "INFORMATION_SCHEMA.QUERY_STATISTICS (SET QUERY_STATISTICS TRUE) + trace file (SET TRACE_LEVEL_FILE 2, t:<ms> per statement)"
    audit_mechanism = "trace file level 2 (every statement, DDL included)"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self._keep = engine.connect(self.primary)          # keeps the database open (settings live as long as it is open)

    def q(self, sql: str, *, target: Target | None = None, fetch: bool = True, autocommit: bool = True):
        if target is None:
            return self._keep.execute(sql, rendered=True, fetch=fetch)
        return super().q(sql, target=target, fetch=fetch, autocommit=autocommit)

    def settings(self) -> dict[str, Any]:
        self.q("SET QUERY_STATISTICS TRUE", fetch=False)
        self.q("SET QUERY_STATISTICS_MAX_ENTRIES 1000", fetch=False)
        self.q("SET TRACE_MAX_FILE_SIZE 64", fetch=False)
        self.q("SET TRACE_LEVEL_FILE 1", fetch=False)
        out = {"trace_level_file": 1, "query_statistics": True}
        try:
            rows = self.q("SELECT SETTING_NAME, SETTING_VALUE FROM INFORMATION_SCHEMA.SETTINGS WHERE SETTING_NAME IN ('QUERY_STATISTICS','QUERY_STATISTICS_MAX_ENTRIES','TRACE_LEVEL_FILE','TRACE_LEVEL_SYSTEM_OUT','TRACE_MAX_FILE_SIZE','MODE','info.VERSION')")
            out.update({r[0]: r[1] for r in rows or []})
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:150]
        return out

    def slow_query(self, mk: str) -> str:
        return f"SELECT COUNT(*) AS {mk} FROM events e1 JOIN events e2 ON e1.customer_id = e2.customer_id WHERE e1.id < 4000 AND e2.id < 4000"

    def find_slow(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT SQL_STATEMENT, EXECUTION_COUNT, MAX_EXECUTION_TIME, AVERAGE_EXECUTION_TIME, MAX_ROW_COUNT FROM INFORMATION_SCHEMA.QUERY_STATISTICS "
                      f"WHERE SQL_STATEMENT LIKE '%{mk}%' AND MAX_EXECUTION_TIME >= {self.slow_threshold_ms}")
        if rows:
            r = rows[0]
            return {"found": True, "sample": f"count={r[1]} max_ms={r[2]} avg_ms={r[3]} rows={r[4]} sql={str(r[0])[:100]}", "duration_ms": float(r[2])}
        return {"found": False}

    def full_logging(self, on: bool) -> str | None:
        self.q(f"SET TRACE_LEVEL_FILE {2 if on else 1}", fetch=False)
        return "SET TRACE_LEVEL_FILE 2 (every statement with its time written to /data/lab.trace.db)"

    def audit_setup(self) -> str | None:
        self.q("SET TRACE_LEVEL_FILE 2", fetch=False)
        return "SET TRACE_LEVEL_FILE 2"

    def audit_find(self, mk: str) -> dict[str, Any]:
        line = self.grep_files(f"audit_{mk}", ["/data/lab.trace.db"])
        return {"found": bool(line), "sample": (line or "")[:300]}

    def audit_teardown(self) -> None:
        self.q("SET TRACE_LEVEL_FILE 1", fetch=False)

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, "/data/lab.trace.db")]

    def extra_report(self) -> dict[str, Any]:
        try:
            rows = self.q("SELECT SQL_STATEMENT, EXECUTION_COUNT, AVERAGE_EXECUTION_TIME FROM INFORMATION_SCHEMA.QUERY_STATISTICS ORDER BY EXECUTION_COUNT DESC FETCH FIRST 5 ROWS ONLY")
            return {"query_statistics_top5": [{"sql": str(r[0])[:90], "calls": r[1], "avg_ms": float(r[2])} for r in rows or []]}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:150]}


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return H2LogProbe(engine, cfg, log=log)
