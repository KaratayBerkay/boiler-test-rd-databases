"""MySQL / MariaDB / Percona XtraDB Cluster logging probes.

MySQL 9:  slow query log (log_slow_extra) -> /var/log/mysql/slow.log, error log as text + JSON (component_log_sink_json),
          performance_schema statement digests; audit = general log switched on for the test (Community has no audit plugin)
MariaDB:  slow log with log_slow_verbosity=query_plan,explain, server_audit plugin (CONNECT + QUERY_DDL) -> audit.log
PXC 8.4:  slow log with Percona log_slow_verbosity=full, audit_log plugin in JSON (DDL commands only) -> audit.json
"""
from __future__ import annotations

import json
from typing import Any

from ..config import StackConfig, Target
from ..engines import Engine
from .common import LogProbe

VARS = ["slow_query_log", "slow_query_log_file", "long_query_time", "log_slow_extra", "log_slow_verbosity", "log_queries_not_using_indexes",
        "min_examined_row_limit", "log_error", "log_error_verbosity", "log_error_services", "log_timestamps", "general_log", "general_log_file",
        "log_output", "log_bin", "binlog_format", "performance_schema", "server_audit_logging", "server_audit_events", "server_audit_file_path",
        "audit_log_format", "audit_log_file", "audit_log_policy", "audit_log_include_commands", "log_warnings", "innodb_print_all_deadlocks"]


class MyLogProbe(LogProbe):
    sink = "slow query log file (long_query_time)"
    audit_mechanism = "general log (no audit plugin in MySQL Community)"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.log_dir = self.lconf.get("dir", "/var/log/mysql")
        self.flavor = self.lconf.get("flavor") or ("mariadb" if cfg.dialect == "mariadb" else ("pxc" if cfg.key == "pxc" else "mysql"))
        p = self.primary
        self.admin = Target(name="admin", role="admin", host=p.host, port=p.port, user=p.extra.get("admin_user", "root"),
                            password=p.extra.get("admin_password", "rootpass"), database=p.database, container=p.container)
        if self.flavor == "mariadb":
            self.audit_mechanism = "server_audit plugin (CONNECT, QUERY_DDL) -> audit.log"
        elif self.flavor == "pxc":
            self.audit_mechanism = "Percona audit_log plugin, JSON, DDL commands only -> audit.json"

    def settings(self) -> dict[str, Any]:
        out = {}
        c = self.engine.connect(self.admin)
        try:
            rows = c.execute("SHOW GLOBAL VARIABLES WHERE Variable_name IN (" + ",".join(f"'{v}'" for v in VARS) + ")", rendered=True)
            out.update({r[0]: r[1] for r in rows})
        finally:
            c.close()
        return out

    def slow_query(self, mk: str) -> str:
        return f"SELECT SLEEP({self.slow_sleep_s}) AS {mk}"

    def find_slow(self, mk: str) -> dict[str, Any]:
        f = f"{self.log_dir}/slow.log"
        line = self.grep_files(mk, [f])
        if not line:
            return {"found": False}
        # the header lines precede the statement; fetch a small window around the match for the sample
        rc_out = self.read(f, tail_bytes=200_000)
        i = rc_out.rfind(mk)
        window = rc_out[max(0, i - 700):i + 120] if i >= 0 else line
        head = window[window.rfind("# Time:"):] if "# Time:" in window else window
        return {"found": True, "sample": head.strip()[:700]}

    def full_logging(self, on: bool) -> str | None:
        c = self.engine.connect(self.admin)
        try:
            c.execute(f"SET GLOBAL long_query_time = {0 if on else self.slow_threshold_ms / 1000}", rendered=True, fetch=False)
            c.execute("SET GLOBAL slow_query_log = ON", rendered=True, fetch=False)
        finally:
            c.close()
        return "SET GLOBAL long_query_time = 0 (every statement written to the slow log, new connections)"

    def audit_setup(self) -> str | None:
        if self.flavor == "mysql":
            c = self.engine.connect(self.admin)
            try:
                c.execute("SET GLOBAL general_log = ON", rendered=True, fetch=False)
            finally:
                c.close()
            return "SET GLOBAL general_log = ON (temporary, for the test)"
        return "configured at startup (compose command / lab.cnf)"

    def audit_find(self, mk: str) -> dict[str, Any]:
        if self.flavor == "mariadb":
            f, needle = f"{self.log_dir}/audit.log", f"audit_{mk}"
        elif self.flavor == "pxc":
            f, needle = f"{self.log_dir}/audit.json", f"audit_{mk}"
        else:
            f, needle = f"{self.log_dir}/general.log", f"audit_{mk}"
        line = self.grep_files(needle, [f])
        if not line:
            return {"found": False}
        if self.flavor == "pxc":
            try:
                rec = json.loads(line.rstrip(","))
                return {"found": True, "sample": json.dumps(rec)[:500]}
            except json.JSONDecodeError:
                pass
        return {"found": True, "sample": line[:400]}

    def audit_teardown(self) -> None:
        if self.flavor == "mysql":
            c = self.engine.connect(self.admin)
            try:
                c.execute("SET GLOBAL general_log = OFF", rendered=True, fetch=False)
            finally:
                c.close()

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, f"{self.log_dir}/slow.log"), (self.container, f"{self.log_dir}/error.log"),
                (self.container, f"{self.log_dir}/error.log.00.json"), (self.container, f"{self.log_dir}/audit.log"),
                (self.container, f"{self.log_dir}/audit.json"), (self.container, f"{self.log_dir}/general.log")]

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        c = self.engine.connect(self.admin)
        try:
            try:
                rows = c.execute("SELECT COUNT_STAR, ROUND(SUM_TIMER_WAIT/1e9,1), ROUND(AVG_TIMER_WAIT/1e9,3), LEFT(DIGEST_TEXT, 90) "
                                 "FROM performance_schema.events_statements_summary_by_digest WHERE SCHEMA_NAME = 'lab' "
                                 "ORDER BY SUM_TIMER_WAIT DESC LIMIT 5", rendered=True)
                out["performance_schema_top5"] = [{"calls": r[0], "total_ms": float(r[1]), "avg_ms": float(r[2]), "digest": r[3]} for r in rows]
            except Exception as e:  # noqa: BLE001
                out["performance_schema_error"] = str(e)[:120]
            try:
                out["error_log_last"] = [str(r) for r in c.execute("SELECT logged, prio, subsystem, LEFT(data, 120) FROM performance_schema.error_log ORDER BY logged DESC LIMIT 5", rendered=True)]
            except Exception:  # noqa: BLE001
                pass
        finally:
            c.close()
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return MyLogProbe(engine, cfg, log=log)
