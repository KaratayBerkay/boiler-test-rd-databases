"""TiDB logging probe: JSON server log to stdout (docker json-file), slow log file (log.slow-threshold) readable through
INFORMATION_SCHEMA.SLOW_QUERY / CLUSTER_SLOW_QUERY, statement summary tables; DDL history (ADMIN SHOW DDL JOBS) as the
DDL audit trail (the audit log plugin is TiDB Enterprise only)."""
from __future__ import annotations

from typing import Any

from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe

VARS = ["tidb_slow_log_threshold", "tidb_enable_slow_log", "tidb_slow_log_masking", "tidb_expensive_query_time_threshold",
        "tidb_enable_stmt_summary", "tidb_stmt_summary_max_stmt_count", "tidb_redact_log", "tidb_gc_life_time", "tidb_log_file_max_days",
        "tidb_enable_collect_execution_info"]


class TiDBLogProbe(LogProbe):
    sink = "tidb-slow.log (log.slow-threshold) exposed as INFORMATION_SCHEMA.SLOW_QUERY"
    audit_mechanism = "DDL job history (ADMIN SHOW DDL JOBS) + [ddl] records in the JSON server log"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.log_dir = self.lconf.get("dir", "/var/log/tidb")

    def settings(self) -> dict[str, Any]:
        out = {}
        c = self.engine.connect(self.primary)
        try:
            for v in VARS:
                try:
                    out[v] = c.execute(f"SELECT @@global.{v}", rendered=True)[0][0]
                except Exception:  # noqa: BLE001
                    pass
            try:
                rows = c.execute("SELECT `key`, value FROM information_schema.cluster_config WHERE type='tidb' AND `key` IN "
                                 "('log.format','log.level','log.slow-threshold','log.slow-query-file','log.file.filename','log.file.max-size','log.file.max-backups','log.expensive-threshold')", rendered=True)
                out["cluster_config"] = {r[0]: r[1] for r in rows}
            except Exception as e:  # noqa: BLE001
                out["cluster_config_error"] = str(e)[:120]
        finally:
            c.close()
        return out

    def slow_query(self, mk: str) -> str:
        return f"SELECT SLEEP({self.slow_sleep_s}) AS {mk}"

    def find_slow(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT time, query_time, user, db, LEFT(query, 120) FROM information_schema.slow_query WHERE query LIKE '%{mk}%' ORDER BY time DESC LIMIT 1")
        if rows:
            r = rows[0]
            return {"found": True, "sample": f"time={r[0]} query_time={r[1]} user={r[2]} db={r[3]} query={r[4]}", "duration_ms": round(float(r[1]) * 1000, 1)}
        return {"found": False}

    def full_logging(self, on: bool) -> str | None:
        self.q(f"SET GLOBAL tidb_slow_log_threshold = {0 if on else self.slow_threshold_ms}", fetch=False)
        return "SET GLOBAL tidb_slow_log_threshold = 0 (every statement written to tidb-slow.log)"

    def audit_setup(self) -> str | None:
        return "always on (DDL jobs are persisted in the DDL history)"

    def audit_find(self, mk: str) -> dict[str, Any]:
        rows = self.q("ADMIN SHOW DDL JOBS 30")
        for r in rows or []:
            if f"audit_{mk}" in str(r):
                return {"found": True, "sample": str(r)[:300]}
        return {"found": False}

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, f"{self.log_dir}/tidb-slow.log")]

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            rows = self.q("SELECT exec_count, ROUND(avg_latency/1e6, 2), ROUND(sum_latency/1e6, 1), LEFT(digest_text, 90) FROM information_schema.statements_summary "
                          "WHERE schema_name = 'lab' ORDER BY sum_latency DESC LIMIT 5")
            out["statements_summary_top5"] = [{"calls": r[0], "avg_ms": float(r[1]), "total_ms": float(r[2]), "digest": r[3]} for r in rows or []]
        except Exception as e:  # noqa: BLE001
            out["statements_summary_error"] = str(e)[:120]
        try:
            out["slow_query_rows"] = int(self.q("SELECT COUNT(*) FROM information_schema.slow_query")[0][0])
        except Exception:  # noqa: BLE001
            pass
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return TiDBLogProbe(engine, cfg, log=log)
