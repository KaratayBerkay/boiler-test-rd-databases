"""CrateDB logging probe: sys.jobs_log (every statement with user, start/end, error) is the in-memory statement log and
audit trail; stats.jobs_log_persistent_filter writes matching jobs (here: slower than 100 ms) as JSON lines to the
CrateDB log (stdout -> docker json-file)."""
from __future__ import annotations

from typing import Any

from .. import dockerctl
from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe


def _ms(v) -> float:
    """sys.jobs_log started/ended come back as epoch millis (int) or datetime depending on the client."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return v.timestamp() * 1000.0


class CrateLogProbe(LogProbe):
    sink = "sys.jobs_log (+ stats.jobs_log_persistent_filter -> JSON lines in the CrateDB log / docker logs)"
    audit_mechanism = "sys.jobs_log (username, stmt, started, ended, error)"

    def settings(self) -> dict[str, Any]:
        rows = self.q("SELECT settings['stats'] FROM sys.cluster")
        st = rows[0][0] if rows else {}
        return {"stats": {k: st.get(k) for k in ("enabled", "jobs_log_size", "jobs_log_expiration", "jobs_log_filter", "jobs_log_persistent_filter", "operations_log_size")} if isinstance(st, dict) else str(st)[:300]}

    def slow_query(self, mk: str) -> str:
        # no sleep() in CrateDB: a wide self join + aggregate takes a few hundred ms
        return f"SELECT count(*) AS {mk} FROM events e1 JOIN events e2 ON e1.customer_id = e2.customer_id WHERE e1.id < 3000 AND e2.id < 3000"

    def find_slow(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT started, ended, username, stmt, error FROM sys.jobs_log WHERE stmt LIKE '%{mk}%' AND ended::bigint - started::bigint >= {self.slow_threshold_ms} ORDER BY ended DESC LIMIT 1")
        out: dict[str, Any] = {"found": False}
        if rows:
            r = rows[0]
            out = {"found": True, "sample": f"started={r[0]} ended={r[1]} user={r[2]} stmt={str(r[3])[:120]}", "duration_ms": _ms(r[1]) - _ms(r[0])}
        # the persistent filter writes the same job as a JSON line into the server log
        logs = dockerctl.container_logs(self.container, tail=400)
        for line in logs.splitlines()[::-1]:
            if mk in line:
                out["docker_log_line"] = line[:400]
                out["found"] = True
                break
        return out

    def full_logging(self, on: bool) -> str | None:
        v = "true" if on else f"ended::bigint - started::bigint > {self.slow_threshold_ms}"
        self.q(f"SET GLOBAL TRANSIENT stats.jobs_log_persistent_filter = '{v}'", fetch=False)
        return "SET GLOBAL TRANSIENT stats.jobs_log_persistent_filter = 'true' (every job written to the log)"

    def audit_setup(self) -> str | None:
        return "sys.jobs_log is always on (stats.enabled=true)"

    def audit_find(self, mk: str) -> dict[str, Any]:
        rows = self.q(f"SELECT started, username, stmt FROM sys.jobs_log WHERE stmt LIKE '%audit_{mk}%' ORDER BY started LIMIT 1")
        return {"found": bool(rows), "sample": f"started={rows[0][0]} user={rows[0][1]} stmt={rows[0][2]}" if rows else ""}

    def log_bytes(self) -> int:
        lc = dockerctl.log_config(self.container)
        return int(lc.get("host_log_bytes_total") or lc.get("host_log_bytes") or lc.get("docker_logs_bytes") or 0)

    def log_files(self) -> list[tuple[str, str]]:
        return []

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            out["jobs_log_rows"] = int(self.q("SELECT count(*) FROM sys.jobs_log")[0][0])
            rows = self.q("SELECT classification['type'], count(*), round(avg(ended::bigint - started::bigint), 2) FROM sys.jobs_log GROUP BY 1 ORDER BY 2 DESC LIMIT 6")
            out["jobs_by_type"] = [{"type": r[0], "count": r[1], "avg_ms": r[2]} for r in rows or []]
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:150]
        try:
            logs = dockerctl.container_logs(self.container, tail=5000)
            (self.cfg.stack_dir.parents[1] / "results" / self.cfg.key / "logs").mkdir(parents=True, exist_ok=True)
            p = self.cfg.stack_dir.parents[1] / "results" / self.cfg.key / "logs" / "crate-1__docker-logs.log"
            p.write_text(logs)
            out["docker_logs_saved"] = str(p.relative_to(self.cfg.stack_dir.parents[1]))
        except Exception as e:  # noqa: BLE001
            out["docker_logs_error"] = str(e)[:150]
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return CrateLogProbe(engine, cfg, log=log)
