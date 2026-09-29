"""MonetDB logging probe: the built-in query log (sys.querylog_enable(threshold_ms) -> sys.querylog_catalog +
sys.querylog_calls with run time, rows, cpu, io per call) and the merovingian.log / mserver output."""
from __future__ import annotations

from typing import Any

from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe


class MonetLogProbe(LogProbe):
    sink = "sys.querylog_calls (CALL sys.querylog_enable(threshold_ms))"
    audit_mechanism = "sys.querylog_catalog (query text + owner) with threshold 0"

    def _enable(self, threshold_ms: int) -> None:
        self.q(f"CALL sys.querylog_enable({threshold_ms})", fetch=False)

    def settings(self) -> dict[str, Any]:
        self._enable(self.slow_threshold_ms)
        out: dict[str, Any] = {"querylog": f"sys.querylog_enable({self.slow_threshold_ms})"}
        try:
            rows = self.q("SELECT name, value FROM sys.env() WHERE name IN ('monet_version','gdk_dbpath','monet_release','sql_debug','max_clients')")
            out.update({r[0]: r[1] for r in rows or []})
        except Exception as e:  # noqa: BLE001
            out["env_error"] = str(e)[:120]
        return out

    def slow_query(self, mk: str) -> str:
        return f"SELECT count(*) AS {mk} FROM events e1, events e2 WHERE e1.customer_id = e2.customer_id AND e1.id <= 200000 AND e2.id <= 200000 AND e1.value_num > e2.value_num"

    def find_slow(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT c.\"start\", c.\"stop\", c.run, c.tuples, q.owner, q.query FROM sys.querylog_calls c JOIN sys.querylog_catalog q ON c.id = q.id "
                      f"WHERE q.query LIKE '%{mk}%' ORDER BY c.\"start\" DESC LIMIT 1")
        if rows:
            r = rows[0]
            return {"found": True, "sample": f"start={r[0]} stop={r[1]} run_us={r[2]} tuples={r[3]} owner={r[4]} query={str(r[5])[:100]}", "duration_ms": round((r[2] or 0) / 1000, 2)}
        return {"found": False}

    def full_logging(self, on: bool) -> str | None:
        self._enable(0 if on else self.slow_threshold_ms)
        return "CALL sys.querylog_enable(0) (every query logged into sys.querylog_calls)"

    def audit_setup(self) -> str | None:
        self._enable(0)
        return "sys.querylog_enable(0)"

    def audit_find(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT q.owner, q.defined, q.query FROM sys.querylog_catalog q WHERE q.query LIKE '%audit_{mk}%' LIMIT 1")
        return {"found": bool(rows), "sample": f"owner={rows[0][0]} defined={rows[0][1]} query={rows[0][2]}" if rows else ""}

    def audit_teardown(self) -> None:
        self._enable(self.slow_threshold_ms)

    def log_bytes(self) -> int:
        try:
            return int(self.q("SELECT count(*) FROM sys.querylog_calls")[0][0]) * 200
        except Exception:  # noqa: BLE001
            return 0

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, "/var/monetdb5/dbfarm/merovingian.log")]

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            out["querylog_calls"] = int(self.q("SELECT count(*) FROM sys.querylog_calls")[0][0])
            rows = self.q("SELECT q.query, count(*), round(avg(c.run)/1000.0, 2) FROM sys.querylog_calls c JOIN sys.querylog_catalog q ON c.id = q.id GROUP BY q.query ORDER BY 2 DESC LIMIT 5")
            out["top5"] = [{"query": str(r[0])[:90], "calls": r[1], "avg_ms": float(r[2])} for r in rows or []]
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:150]
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return MonetLogProbe(engine, cfg, log=log)
