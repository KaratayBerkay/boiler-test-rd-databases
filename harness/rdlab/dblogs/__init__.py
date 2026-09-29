"""Per-engine logging probes. `probe_for(engine, cfg)` returns the LogProbe for a stack (dispatch by dialect)."""
from __future__ import annotations

import importlib

from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe, collect_logs, marker, wait_found

_MODULES = {
    "postgres": "pg", "citus": "pg", "yugabyte": "yugabyte", "cockroach": "cockroach",
    "mysql": "mysql", "mariadb": "mysql", "tidb": "tidb",
    "tsql": "mssql", "oracle": "oracle", "db2": "db2",
    "clickhouse": "clickhouse", "crate": "crate", "questdb": "questdb", "monetdb": "monetdb",
    "firebird": "firebird", "h2": "h2", "sqlite": "embedded", "duckdb": "embedded",
}


def probe_for(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe | None:
    mod = _MODULES.get(cfg.dialect)
    if not mod:
        return None
    m = importlib.import_module(f".{mod}", __name__)
    return m.probe(engine, cfg, log=log)


__all__ = ["LogProbe", "collect_logs", "marker", "probe_for", "wait_found"]
