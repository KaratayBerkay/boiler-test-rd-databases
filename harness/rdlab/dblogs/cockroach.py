"""CockroachDB logging probe: log channels routed to JSON file groups (conf/log.yaml): SQL_PERF (slow query log,
sql.log.slow_query.latency_threshold), SENSITIVE_ACCESS (admin audit + table audit), SQL_EXEC, OPS/HEALTH also on stderr.
Statement statistics come from crdb_internal.statement_statistics."""
from __future__ import annotations

import json
from typing import Any

from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe

SETTINGS = ["sql.log.slow_query.latency_threshold", "sql.log.slow_query.experimental_full_table_scans.enabled", "sql.log.admin_audit.enabled",
            "sql.log.user_audit", "sql.log.all_statements.enabled", "sql.trace.stmt.enable_threshold", "sql.log.slow_query.internal_queries.enabled",
            "sql.stats.flush.enabled", "server.auth_log.sql_connections.enabled", "server.auth_log.sql_sessions.enabled"]


class CRDBLogProbe(LogProbe):
    sink = "slow_query events (SQL_EXEC/SQL_PERF channels -> logs/cockroach-sql-exec.log, json) via sql.log.slow_query.latency_threshold"
    audit_mechanism = "SENSITIVE_ACCESS channel: sql.log.admin_audit + ALTER TABLE ... EXPERIMENTAL_AUDIT SET READ WRITE"
    settle_s = 6.0                # cluster settings reach the nodes through the settings rangefeed

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.log_dir = self.lconf.get("dir", "/cockroach/cockroach-data/logs")

    def settings(self) -> dict[str, Any]:
        out = {}
        c = self.engine.connect(self.primary)
        try:
            for s in SETTINGS:
                try:
                    out[s] = c.execute(f"SHOW CLUSTER SETTING {s}", rendered=True)[0][0]
                except Exception:  # noqa: BLE001
                    pass
        finally:
            c.close()
        files = []
        try:
            from .. import dockerctl
            files = dockerctl.list_files(self.container, f"{self.log_dir}/cockroach-*.log")
        except Exception:  # noqa: BLE001
            pass
        out["log_files"] = [f.rsplit("/", 1)[-1] for f in files][:12]
        return out

    def slow_query(self, mk: str) -> str:
        return f"SELECT pg_sleep({self.slow_sleep_s}) AS {mk}"

    def find_slow(self, mk: str) -> dict[str, Any]:
        # v26 emits the slow_query event on SQL_EXEC (older releases: SQL_PERF) - look in both file groups
        line = self.grep_files(mk, [f"{self.log_dir}/cockroach-sql-exec.log", f"{self.log_dir}/cockroach-sql-perf.log"])
        if not line:
            return {"found": False}
        try:
            rec = json.loads(line)
            ev = rec.get("event", {})
            keep = {"channel": rec.get("channel"), "EventType": ev.get("EventType"), "User": ev.get("User"), "ApplicationName": ev.get("ApplicationName"),
                    "Statement": ev.get("Statement"), "Age": ev.get("Age"), "NumRows": ev.get("NumRows")}
            return {"found": True, "sample": json.dumps(keep)[:600], "duration_ms": ev.get("Age")}
        except json.JSONDecodeError:
            return {"found": True, "sample": line[:500]}

    def full_logging(self, on: bool) -> str | None:
        v = "1us" if on else f"{self.slow_threshold_ms}ms"
        self.q(f"SET CLUSTER SETTING sql.log.slow_query.latency_threshold = '{v}'", fetch=False)
        return "SET CLUSTER SETTING sql.log.slow_query.latency_threshold = '1us' (every statement logged as a slow_query event)"

    def audit_setup(self) -> str | None:
        self.q("SET CLUSTER SETTING sql.log.admin_audit.enabled = true", fetch=False)
        self.q("ALTER TABLE inventory SET (schema_locked = false)", fetch=False)     # v25+: tables are schema-locked by default
        self.q("ALTER TABLE inventory EXPERIMENTAL_AUDIT SET READ WRITE", fetch=False)
        return "sql.log.admin_audit.enabled + ALTER TABLE inventory EXPERIMENTAL_AUDIT SET READ WRITE"

    def audit_actions(self, mk: str) -> list[str]:
        return [f"CREATE TABLE audit_{mk} (id INT PRIMARY KEY)", f"SELECT count(*) AS audit_{mk} FROM inventory", f"DROP TABLE audit_{mk}"]

    def audit_find(self, mk: str) -> dict[str, Any]:
        line = self.grep_files(f"audit_{mk}", [f"{self.log_dir}/cockroach-sql-audit.log"])
        if not line:
            return {"found": False}
        try:
            rec = json.loads(line)
            ev = rec.get("event", {})
            keep = {"channel": rec.get("channel"), "EventType": ev.get("EventType"), "User": ev.get("User"), "Statement": ev.get("Statement"),
                    "TableName": ev.get("TableName"), "AccessMode": ev.get("AccessMode")}
            return {"found": True, "sample": json.dumps(keep)[:500]}
        except json.JSONDecodeError:
            return {"found": True, "sample": line[:500]}

    def audit_teardown(self) -> None:
        self.q("ALTER TABLE inventory EXPERIMENTAL_AUDIT SET OFF", fetch=False)
        self.q("ALTER TABLE inventory SET (schema_locked = true)", fetch=False)

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, f"{self.log_dir}/cockroach-sql-perf.log"), (self.container, f"{self.log_dir}/cockroach-sql-audit.log"),
                (self.container, f"{self.log_dir}/cockroach.log"), (self.container, f"{self.log_dir}/cockroach-sql-exec.log")]

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            rows = self.q("SELECT metadata->>'query' AS q, sum((statistics->'statistics'->>'cnt')::INT) AS cnt, "
                          "round(avg((statistics->'statistics'->'runLat'->>'mean')::FLOAT8) * 1000, 3) AS mean_ms "
                          "FROM crdb_internal.statement_statistics WHERE metadata->>'db' = 'lab' GROUP BY q ORDER BY cnt DESC LIMIT 5")
            out["statement_statistics_top5"] = [{"query": r[0][:90], "calls": r[1], "mean_ms": r[2]} for r in rows or []]
        except Exception as e:  # noqa: BLE001
            out["statement_statistics_error"] = str(e)[:150]
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return CRDBLogProbe(engine, cfg, log=log)
