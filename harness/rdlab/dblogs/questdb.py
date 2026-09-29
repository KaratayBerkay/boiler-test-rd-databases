"""QuestDB logging probe: query tracing (query.tracing.enabled -> the _query_trace system table with execution_micros
per statement, QuestDB 8.x), server log on stdout (log.conf) with the docker json-file driver."""
from __future__ import annotations

from typing import Any

from .. import dockerctl
from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe


class QuestLogProbe(LogProbe):
    sink = "_query_trace table (query.tracing.enabled; slow = WHERE execution_micros >= threshold)"
    audit_mechanism = "_query_trace records every statement with its principal"

    def settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            rows = self.q("SELECT build")
            out["build"] = str(rows[0][0])[:120] if rows else None
        except Exception:  # noqa: BLE001
            pass
        try:
            out["_query_trace_rows"] = int(self.q("SELECT count() FROM _query_trace")[0][0])
        except Exception as e:  # noqa: BLE001
            out["_query_trace_error"] = str(e)[:150]
        return out

    def slow_query(self, mk: str) -> str:
        return (f"SELECT count() AS {mk} FROM (SELECT * FROM events WHERE id <= 120000) e1 JOIN (SELECT * FROM events WHERE id <= 120000) e2 "
                f"ON e1.customer_id = e2.customer_id WHERE e1.value_num > e2.value_num")

    def find_slow(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT ts, principal, execution_micros, query_text FROM _query_trace WHERE query_text LIKE '%{mk}%' AND execution_micros >= {self.slow_threshold_ms * 1000} ORDER BY ts DESC LIMIT 1")
        if rows:
            r = rows[0]
            return {"found": True, "sample": f"ts={r[0]} principal={r[1]} execution_micros={r[2]} query={str(r[3])[:120]}", "duration_ms": round(r[2] / 1000, 2)}
        return {"found": False}

    def full_logging(self, on: bool) -> str | None:
        return "query tracing is always on for every statement (query.tracing.enabled=true); overhead = tracing on vs off is a restart-time switch" if on else None

    def workload_session_sql(self, on: bool) -> str | None:
        return None

    def audit_setup(self) -> str | None:
        return "_query_trace"

    def audit_find(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT ts, principal, query_text FROM _query_trace WHERE query_text LIKE '%audit_{mk}%' ORDER BY ts LIMIT 1")
        return {"found": bool(rows), "sample": f"ts={rows[0][0]} principal={rows[0][1]} query={rows[0][2]}" if rows else ""}

    def log_bytes(self) -> int:
        lc = dockerctl.log_config(self.container)
        return int(lc.get("host_log_bytes_total") or lc.get("docker_logs_bytes") or 0)

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, "/var/lib/questdb/log/*.log")]

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            rows = self.q("SELECT count(), round(avg(execution_micros)/1000.0, 3), round(max(execution_micros)/1000.0, 2) FROM _query_trace")
            out["_query_trace"] = {"rows": rows[0][0], "avg_ms": rows[0][1], "max_ms": rows[0][2]} if rows else {}
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:150]
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return QuestLogProbe(engine, cfg, log=log)
