"""Driver-level engine adapters. `make_engine(stack)` picks the adapter from lab.yaml `driver`."""
from __future__ import annotations

from ..config import StackConfig
from .base import Engine, Conn, EngineError


def make_engine(cfg: StackConfig) -> Engine:
    d = cfg.driver
    if d == "psycopg":
        from .pg import PsycopgEngine
        return PsycopgEngine(cfg)
    if d == "pymysql":
        from .mysql import PyMySQLEngine
        return PyMySQLEngine(cfg)
    if d == "sqlite3":
        from .sqlite import SQLiteEngine
        return SQLiteEngine(cfg)
    if d == "duckdb":
        from .duckdb import DuckDBEngine
        return DuckDBEngine(cfg)
    if d == "clickhouse-connect":
        from .clickhouse import ClickHouseEngine
        return ClickHouseEngine(cfg)
    if d == "pymssql":
        from .mssql import MSSQLEngine
        return MSSQLEngine(cfg)
    if d == "oracledb":
        from .oracle import OracleEngine
        return OracleEngine(cfg)
    if d == "ibm_db":
        from .db2 import Db2Engine
        return Db2Engine(cfg)
    if d == "firebird-driver":
        from .firebird import FirebirdEngine
        return FirebirdEngine(cfg)
    if d == "pymonetdb":
        from .monetdb import MonetDBEngine
        return MonetDBEngine(cfg)
    if d == "crate":
        from .crate import CrateEngine
        return CrateEngine(cfg)
    if d == "questdb":
        from .questdb import QuestDBEngine
        return QuestDBEngine(cfg)
    if d == "jdbc":
        from .jdbc import JDBCEngine
        return JDBCEngine(cfg)
    raise EngineError(f"unknown driver '{d}' for stack {cfg.key}")


__all__ = ["make_engine", "Engine", "Conn", "EngineError"]
