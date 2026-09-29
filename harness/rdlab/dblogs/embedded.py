"""Embedded engines: the application is the logger.

SQLite:  statements are timed in the driver wrapper (engines/sqlite.py) and written as JSON lines when they exceed
         RDLAB_SQLITE_SLOW_MS -> results/sqlite/logs/slow.jsonl (0 = every statement, used for the overhead test).
DuckDB:  the built-in logger (DuckDB 1.2+): CALL enable_logging('QueryLog') records every statement, readable with
         duckdb_logs() or written to CSV files (storage='file'); profiling (enable_profiling='json') adds per-query latency.
"""
from __future__ import annotations

import json
import os
from typing import Any

from ..config import StackConfig
from ..engines import Engine
from ..util import RESULTS, ROOT
from .common import LogProbe

HEAVY = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < {n}) SELECT COUNT(*) AS {mk} FROM c"


class SQLiteLogProbe(LogProbe):
    sink = "application-side JSONL slow log (driver wrapper, RDLAB_SQLITE_SLOW_MS)"
    audit_mechanism = "n/a (no server; use the same wrapper or SQLite's sqlite3_trace_v2 hook)"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.path = RESULTS / cfg.key / "logs" / "slow.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.environ["RDLAB_SQLITE_SLOW_MS"] = str(self.slow_threshold_ms)
        os.environ["RDLAB_SQLITE_SLOW_LOG"] = str(self.path)

    def settings(self) -> dict[str, Any]:
        out = {"RDLAB_SQLITE_SLOW_MS": os.environ.get("RDLAB_SQLITE_SLOW_MS"), "log": str(self.path.relative_to(ROOT))}
        c = self.engine.connect(self.primary)
        try:
            for pr in ("journal_mode", "synchronous", "wal_autocheckpoint", "cache_size"):
                out[pr] = c.execute(f"PRAGMA {pr}", rendered=True)[0][0]
        finally:
            c.close()
        return out

    def slow_query(self, mk: str) -> str:
        return HEAVY.format(n=2_000_000, mk=mk)

    def find_slow(self, mk: str) -> dict[str, Any]:
        if not self.path.exists():
            return {"found": False}
        for line in self.path.read_text().splitlines()[::-1]:
            if mk in line:
                rec = json.loads(line)
                return {"found": True, "sample": line[:400], "duration_ms": rec.get("duration_ms")}
        return {"found": False}

    def full_logging(self, on: bool) -> str | None:
        os.environ["RDLAB_SQLITE_SLOW_MS"] = "0" if on else str(self.slow_threshold_ms)
        return "RDLAB_SQLITE_SLOW_MS=0 (every statement appended to slow.jsonl by every worker process)"

    def log_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def log_files(self) -> list[tuple[str, str]]:
        return []


class DuckDBLogProbe(LogProbe):
    sink = "duckdb_logs() QueryLog (every statement; in-memory or CSV file storage)"
    audit_mechanism = "QueryLog records every statement (DDL included)"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.dir = RESULTS / cfg.key / "logs" / "duckdb_logs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._on = False

    def _enable(self, file: bool = False) -> None:
        c = self.engine.connect(self.primary)
        try:
            if file:
                c.execute(f"CALL enable_logging('QueryLog', storage='file', storage_path='{self.dir}')", rendered=True, fetch=False)
            else:
                c.execute("CALL enable_logging('QueryLog')", rendered=True, fetch=False)
        finally:
            c.close()
        self._on = True

    def _disable(self) -> None:
        c = self.engine.connect(self.primary)
        try:
            c.execute("CALL disable_logging()", rendered=True, fetch=False)
        finally:
            c.close()
        self._on = False

    def settings(self) -> dict[str, Any]:
        self._enable()
        out = {}
        c = self.engine.connect(self.primary)
        try:
            for s in ("enable_logging", "logging_level", "logging_storage", "enable_profiling", "profiling_output", "threads", "memory_limit"):
                try:
                    out[s] = c.execute(f"SELECT current_setting('{s}')", rendered=True)[0][0]
                except Exception:  # noqa: BLE001
                    pass
            out["duckdb_version"] = c.execute("SELECT version()", rendered=True)[0][0]
        finally:
            c.close()
        return out

    def slow_query(self, mk: str) -> str:
        return f"SELECT COUNT(*) AS {mk} FROM range(300000000)"

    def find_slow(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT timestamp::VARCHAR, type, log_level, message FROM duckdb_logs() WHERE message LIKE '%{mk}%' LIMIT 1")
        if rows:
            return {"found": True, "sample": " | ".join(str(x) for x in rows[0])[:400], "note": "QueryLog has no duration threshold: every statement is logged"}
        return {"found": False}

    def full_logging(self, on: bool) -> str | None:
        if on:
            self._enable()
            return "CALL enable_logging('QueryLog') vs CALL disable_logging() (workload threads share the database handle)"
        self._disable()
        return None

    def workload_session_sql(self, on: bool) -> str | None:
        return None

    def audit_setup(self) -> str | None:
        self._enable()
        return "QueryLog"

    def audit_find(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT timestamp::VARCHAR, message FROM duckdb_logs() WHERE message LIKE '%audit_{mk}%' LIMIT 1")
        return {"found": bool(rows), "sample": " | ".join(str(x) for x in rows[0])[:300] if rows else ""}

    def log_bytes(self) -> int:
        try:
            return int(self.q("SELECT COALESCE(SUM(LENGTH(message)), 0) FROM duckdb_logs()")[0][0])
        except Exception:  # noqa: BLE001
            return 0

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            out["log_entries"] = int(self.q("SELECT COUNT(*) FROM duckdb_logs()")[0][0])
            # dump the in-memory log to CSV files (what storage='file' produces) so it is kept with the results
            self.q(f"COPY (SELECT * FROM duckdb_logs()) TO '{self.dir / 'duckdb_log_entries.csv'}' (HEADER)", fetch=False)
            out["exported"] = str((self.dir / "duckdb_log_entries.csv").relative_to(ROOT))
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:150]
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return SQLiteLogProbe(engine, cfg, log=log) if cfg.dialect == "sqlite" else DuckDBLogProbe(engine, cfg, log=log)
