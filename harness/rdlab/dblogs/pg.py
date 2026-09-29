"""PostgreSQL / Citus logging probe.

Server config (compose): logging_collector=on, log_destination=stderr,jsonlog -> /var/lib/postgresql/log/*.json,
log_min_duration_statement=100ms, log_lock_waits, log_temp_files=0, log_checkpoints, log_connections (PG18 list form),
auto_explain (json plans for statements > 500 ms), pg_stat_statements.
Audit: pgaudit is not part of the pgvector image, so DDL auditing uses log_statement=ddl (every DDL logged with user,
database and application_name in the JSON record); the skill documents the pgaudit setup.
"""
from __future__ import annotations

import json
from typing import Any

from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe

SETTINGS = ["logging_collector", "log_destination", "log_directory", "log_filename", "log_rotation_size", "log_rotation_age",
            "log_min_duration_statement", "log_min_duration_sample", "log_statement_sample_rate", "log_statement", "log_lock_waits",
            "log_temp_files", "log_checkpoints", "log_connections", "log_disconnections", "log_autovacuum_min_duration",
            "log_line_prefix", "log_error_verbosity", "shared_preload_libraries", "auto_explain.log_min_duration",
            "auto_explain.log_format", "track_io_timing", "archive_mode", "summarize_wal"]


class PgLogProbe(LogProbe):
    sink = "jsonlog file (logging_collector) via log_min_duration_statement"
    audit_mechanism = "log_statement=ddl (pgaudit not bundled in the image)"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.log_dir = self.lconf.get("dir", "/var/lib/postgresql/log")
        self.os_user = self.lconf.get("os_user", "postgres")

    def settings(self) -> dict[str, Any]:
        out = {}
        c = self.engine.connect(self.primary)
        try:
            for s in SETTINGS:
                try:
                    out[s] = c.execute(f"SHOW {s}", rendered=True)[0][0]
                except Exception:  # noqa: BLE001
                    pass
            try:
                out["log_files"] = [r[0] for r in c.execute("SELECT name FROM pg_ls_logdir() ORDER BY modification DESC LIMIT 6", rendered=True)]
                out["current_logfile_json"] = c.execute("SELECT pg_current_logfile('jsonlog')", rendered=True)[0][0]
            except Exception as e:  # noqa: BLE001
                out["log_files_error"] = str(e)[:120]
        finally:
            c.close()
        return out

    def slow_query(self, mk: str) -> str:
        return f"SELECT pg_sleep({self.slow_sleep_s}) AS {mk}"

    def _json_lines(self) -> str:
        return self.read(f"{self.log_dir}/*.json", tail_bytes=6_000_000)

    def find_slow(self, mk: str) -> dict[str, Any]:
        line = self.grep_files(mk, [f"{self.log_dir}/*.json"], user=self.os_user)
        if not line:
            return {"found": False}
        try:
            rec = json.loads(line)
            return {"found": True, "sample": json.dumps({k: rec.get(k) for k in ("timestamp", "user", "dbname", "application_name", "message", "error_severity") if k in rec}),
                    "duration_ms": _duration_from_message(rec.get("message", ""))}
        except json.JSONDecodeError:
            return {"found": True, "sample": line[:500]}

    def full_logging(self, on: bool) -> str | None:
        val = "0" if on else "100ms"
        self.q_many([f"ALTER SYSTEM SET log_min_duration_statement = '{val}'", "SELECT pg_reload_conf()"])
        return "ALTER SYSTEM SET log_min_duration_statement = 0 + pg_reload_conf() (every statement logged with its duration)"

    def audit_setup(self) -> str | None:
        self.q_many(["ALTER SYSTEM SET log_statement = 'ddl'", "SELECT pg_reload_conf()"])
        return "log_statement=ddl"

    def audit_find(self, mk: str) -> dict[str, Any]:
        line = self.grep_files(f"CREATE TABLE audit_{mk}", [f"{self.log_dir}/*.json"], user=self.os_user)
        if not line:
            return {"found": False}
        try:
            rec = json.loads(line)
            return {"found": True, "sample": json.dumps({k: rec.get(k) for k in ("timestamp", "user", "dbname", "application_name", "message") if k in rec})}
        except json.JSONDecodeError:
            return {"found": True, "sample": line[:400]}

    def audit_teardown(self) -> None:
        self.q_many(["ALTER SYSTEM RESET log_statement", "SELECT pg_reload_conf()"])

    def log_files(self) -> list[tuple[str, str]]:
        out = [(self.container, f"{self.log_dir}/*.json"), (self.container, f"{self.log_dir}/*.log")]
        for t in self.cfg.targets:
            if t.role == "replica" and t.container and t.container != self.container:
                out.append((t.container, f"{self.log_dir}/*.json"))
                break
        return out

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        c = self.engine.connect(self.primary)
        try:
            try:
                rows = c.execute("SELECT calls, round(total_exec_time::numeric, 1), round(mean_exec_time::numeric, 2), left(query, 90) "
                                 "FROM pg_stat_statements WHERE query NOT ILIKE '%pg_stat_statements%' ORDER BY total_exec_time DESC LIMIT 5", rendered=True)
                out["pg_stat_statements_top5"] = [{"calls": r[0], "total_ms": float(r[1]), "mean_ms": float(r[2]), "query": r[3]} for r in rows]
            except Exception as e:  # noqa: BLE001
                out["pg_stat_statements_error"] = str(e)[:120]
            try:
                out["log_dir_bytes"] = int(c.execute("SELECT COALESCE(SUM(size), 0) FROM pg_ls_logdir()", rendered=True)[0][0])
            except Exception:  # noqa: BLE001
                pass
        finally:
            c.close()
        return out


def _duration_from_message(msg: str) -> float | None:
    # "duration: 300.412 ms  statement: SELECT ..."
    if msg.startswith("duration: "):
        try:
            return float(msg.split()[1])
        except (IndexError, ValueError):
            return None
    return None


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return PgLogProbe(engine, cfg, log=log)
