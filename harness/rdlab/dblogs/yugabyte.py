"""YugabyteDB logging probe: the YSQL layer is PostgreSQL, so log_min_duration_statement etc. are set through
--tserver_flags=ysql_pg_conf_csv={...} and the log is <base_dir>/data/yb-data/tserver/logs/postgresql-*.log; the tserver
and master glog files sit next to it. Audit: ysql_log_statement=ddl (pgaudit is bundled: CREATE EXTENSION pgaudit) ."""
from __future__ import annotations

from typing import Any

from .. import dockerctl
from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe
from .pg import SETTINGS


class YBLogProbe(LogProbe):
    sink = "postgresql-*.log of the tserver (log_min_duration_statement via ysql_pg_conf_csv)"
    audit_mechanism = "ysql_log_statement=ddl (+ pgaudit extension available)"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.base = self.lconf.get("base_dir", "/home/yugabyte/yb_data")
        self.log_glob = f"{self.base}/data/yb-data/tserver/logs/postgresql-*.log"

    def _pg_logs(self) -> list[str]:
        return dockerctl.list_files(self.container, self.log_glob)

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
                out["pgaudit_available"] = bool(c.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'pgaudit'", rendered=True))
            except Exception:  # noqa: BLE001
                pass
        finally:
            c.close()
        out["log_files"] = [f.rsplit("/", 1)[-1] for f in self._pg_logs()][-5:]
        return out

    def slow_query(self, mk: str) -> str:
        return f"SELECT pg_sleep({self.slow_sleep_s}) AS {mk}"

    def find_slow(self, mk: str) -> dict[str, Any]:
        line = self.grep_files(mk, [self.log_glob])
        if not line:
            return {"found": False}
        dur = None
        if "duration: " in line:
            try:
                dur = float(line.split("duration: ")[1].split()[0])
            except (IndexError, ValueError):
                pass
        return {"found": True, "sample": line[:500], "duration_ms": dur}

    def full_logging(self, on: bool) -> str | None:
        # ALTER SYSTEM is not supported by YSQL; a database-level GUC applies to every new connection
        v = "0" if on else f"{self.slow_threshold_ms}ms"
        self.q(f"ALTER DATABASE {self.primary.database} SET log_min_duration_statement = '{v}'", fetch=False)
        return "ALTER DATABASE lab SET log_min_duration_statement = 0 (new connections log every statement)"

    def audit_setup(self) -> str | None:
        self.q(f"ALTER DATABASE {self.primary.database} SET log_statement = 'ddl'", fetch=False)
        return "log_statement=ddl at database level"

    def audit_find(self, mk: str) -> dict[str, Any]:
        line = self.grep_files(f"CREATE TABLE audit_{mk}", [self.log_glob])
        return {"found": bool(line), "sample": (line or "")[:400]}

    def audit_teardown(self) -> None:
        self.q(f"ALTER DATABASE {self.primary.database} RESET log_statement", fetch=False)

    def log_dirs(self) -> list[tuple[str, str]]:
        return [(self.container, f"{self.base}/data/yb-data/tserver/logs")]

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, self.log_glob), (self.container, f"{self.base}/data/yb-data/tserver/logs/yb-tserver.INFO"),
                (self.container, f"{self.base}/data/yb-data/master/logs/yb-master.INFO")]

    def extra_report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            rows = self.q("SELECT calls, round(total_exec_time::numeric, 1), round(mean_exec_time::numeric, 2), left(query, 90) FROM pg_stat_statements "
                          "WHERE query NOT ILIKE '%pg_stat_statements%' ORDER BY total_exec_time DESC LIMIT 5")
            out["pg_stat_statements_top5"] = [{"calls": r[0], "total_ms": float(r[1]), "mean_ms": float(r[2]), "query": r[3]} for r in rows or []]
        except Exception as e:  # noqa: BLE001
            out["pg_stat_statements_error"] = str(e)[:120]
        return out


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return YBLogProbe(engine, cfg, log=log)
