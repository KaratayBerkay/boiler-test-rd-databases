"""ClickHouse logging probe: JSON server log (file + console), system.query_log (every query, with durations, users,
tables — the slow log AND the audit trail), system.session_log (logins), system.text_log, system.part_log."""
from __future__ import annotations

from typing import Any

from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe


class CHLogProbe(LogProbe):
    sink = "system.query_log (every query; slow = WHERE query_duration_ms >= threshold)"
    audit_mechanism = "system.query_log (user, client, query_kind, tables) + system.session_log"

    def settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        rows = self.q("SELECT name, value FROM system.server_settings WHERE name IN ('logger.level','logger.log','logger.errorlog','logger.size','logger.count','logger.formatting.type','logger.console')")
        out.update({r[0]: r[1] for r in rows or []})
        rows = self.q("SELECT name, value FROM system.settings WHERE name IN ('log_queries','log_queries_min_type','log_queries_min_query_duration_ms','log_query_threads','log_processors_profiles','log_query_settings','log_profile_events','log_comment')")
        out.update({r[0]: r[1] for r in rows or []})
        rows = self.q("SELECT name FROM system.tables WHERE database = 'system' AND name IN ('query_log','query_thread_log','text_log','session_log','part_log','trace_log','metric_log','backup_log','error_log')")
        out["system_log_tables"] = [r[0] for r in rows or []]
        return out

    def slow_query(self, mk: str) -> str:
        return f"SELECT sleep({self.slow_sleep_s}) AS {mk}"

    def find_slow(self, mk: str) -> dict[str, Any]:
        self.q("SYSTEM FLUSH LOGS", fetch=False)
        rows = self.q(f"SELECT toString(event_time), query_duration_ms, user, query_kind, read_rows, memory_usage, query FROM system.query_log "
                      f"WHERE type = 'QueryFinish' AND query LIKE '%{mk}%' AND query_duration_ms >= {self.slow_threshold_ms} ORDER BY event_time DESC LIMIT 1")
        if rows:
            r = rows[0]
            return {"found": True, "sample": f"event_time={r[0]} query_duration_ms={r[1]} user={r[2]} kind={r[3]} read_rows={r[4]} memory={r[5]} query={r[6][:100]}", "duration_ms": r[1]}
        return {"found": False}

    def full_logging(self, on: bool) -> str | None:
        return "SET log_queries = 1 (per session; every query is written to system.query_log)" if on else None

    def workload_session_sql(self, on: bool) -> str | None:
        return "SET log_queries = 1" if on else "SET log_queries = 0"

    def audit_setup(self) -> str | None:
        return "query_log is always on"

    def audit_find(self, mk: str) -> dict[str, Any]:
        self.q("SYSTEM FLUSH LOGS", fetch=False)
        rows = self.q(f"SELECT toString(event_time), user, client_hostname, query_kind, query FROM system.query_log WHERE type = 'QueryFinish' AND query LIKE '%audit_{mk}%' AND query_kind = 'Create' LIMIT 1")
        if rows:
            r = rows[0]
            return {"found": True, "sample": f"event_time={r[0]} user={r[1]} client={r[2]} kind={r[3]} query={r[4][:120]}"}
        return {"found": False}

    def log_bytes(self) -> int:
        try:
            self.q("SYSTEM FLUSH LOGS", fetch=False)
            rows = self.q("SELECT sum(bytes_on_disk) FROM system.parts WHERE database = 'system' AND table = 'query_log' AND active")
            return int(rows[0][0] or 0)
        except Exception:  # noqa: BLE001
            return 0

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, "/var/log/clickhouse-server/clickhouse-server.log"), (self.container, "/var/log/clickhouse-server/clickhouse-server.err.log")]

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            self.q("SYSTEM FLUSH LOGS", fetch=False)
            out["query_log_rows"] = int(self.q("SELECT count() FROM system.query_log")[0][0])
            rows = self.q("SELECT normalizedQueryHash(query) h, count(), round(avg(query_duration_ms), 2), round(sum(query_duration_ms)), any(substring(query, 1, 90)) "
                          "FROM system.query_log WHERE type = 'QueryFinish' AND current_database = 'lab' GROUP BY h ORDER BY sum(query_duration_ms) DESC LIMIT 5")
            out["query_log_top5"] = [{"calls": r[1], "avg_ms": r[2], "total_ms": r[3], "query": r[4]} for r in rows or []]
            rows = self.q("SELECT type, count() FROM system.session_log GROUP BY type ORDER BY type")
            out["session_log"] = {str(r[0]): r[1] for r in rows or []}
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:150]
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return CHLogProbe(engine, cfg, log=log)
