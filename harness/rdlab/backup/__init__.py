"""Per-engine backup strategies. `strategies_for(engine, cfg)` returns the list for a stack (dispatch by dialect)."""
from __future__ import annotations

import importlib

from ..config import StackConfig
from ..engines import Engine
from .common import Ctx, Strategy, StrategyResult, run_strategy

_MODULES = {
    "postgres": "pg", "citus": "citus", "yugabyte": "yugabyte", "cockroach": "cockroach",
    "mysql": "mysql", "mariadb": "mysql", "tidb": "tidb",
    "tsql": "mssql", "oracle": "oracle", "db2": "db2",
    "clickhouse": "clickhouse", "crate": "crate", "questdb": "questdb", "monetdb": "monetdb",
    "firebird": "firebird", "h2": "h2", "sqlite": "embedded", "duckdb": "embedded",
}


def strategies_for(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    mod = _MODULES.get(cfg.dialect)
    if not mod:
        return []
    m = importlib.import_module(f".{mod}", __name__)
    return m.strategies(engine, cfg)


__all__ = ["Ctx", "Strategy", "StrategyResult", "run_strategy", "strategies_for"]
