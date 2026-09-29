"""Embedded engines (no container): SQLite and DuckDB.

SQLite:  VACUUM INTO 'copy.db' (online, consistent, compacts) and the online backup API (sqlite3.Connection.backup,
         page-level copy that tolerates concurrent writers); both verified by opening the copy.
         Continuous replication / PITR for SQLite is a separate tool (Litestream, LiteFS) - documented in the skill.
DuckDB:  EXPORT DATABASE ... (FORMAT parquet) + IMPORT DATABASE (portable, versioned), and
         ATTACH + COPY FROM DATABASE (binary copy into a second .duckdb file, DuckDB 1.1+).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path

from ..config import StackConfig
from ..engines import Engine
from ..util import ROOT
from .common import Ctx, Strategy, StrategyResult


def _tree_bytes(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _bdir(ctx: Ctx) -> Path:
    d = ROOT / ctx.bconf.get("dir", f"data/{ctx.cfg.key}/backups")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


# --------------------------------------------------------------------------------------------- SQLite
def s_vacuum_into(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    d = _bdir(ctx)
    dest = d / "lab-vacuum.db"
    for p in (dest, dest.with_suffix(".db-wal"), dest.with_suffix(".db-shm")):
        p.unlink(missing_ok=True)
    t0 = time.perf_counter()
    ctx.sql_step("VACUUM INTO", [f"VACUUM INTO '{dest}'"])
    r.backup_seconds = round(time.perf_counter() - t0, 3)
    r.backup_bytes = dest.stat().st_size
    t0 = time.perf_counter()
    tgt = ctx.alt_target(database=_rel(dest))
    r.verify = ctx.verify(tgt)
    r.restore_seconds = round(time.perf_counter() - t0, 3)
    src = ctx.engine.db_path(ctx.primary)
    r.extra["source_bytes"] = os.path.getsize(src) + (os.path.getsize(src + "-wal") if os.path.exists(src + "-wal") else 0)
    r.extra["integrity_check"] = sqlite3.connect(dest).execute("PRAGMA integrity_check").fetchone()[0]
    r.notes = "single SQL statement, consistent snapshot, output is compacted; restore = open the file"
    return r


def s_backup_api(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    d = _bdir(ctx)
    dest = d / "lab-backupapi.db"
    for p in (dest, dest.with_suffix(".db-wal"), dest.with_suffix(".db-shm")):
        p.unlink(missing_ok=True)
    src = sqlite3.connect(ctx.engine.db_path(ctx.primary))
    dst = sqlite3.connect(dest)
    t0 = time.perf_counter()
    try:
        src.backup(dst, pages=4096)          # copies in 4096-page steps, letting writers in between
    finally:
        dst.close()
        src.close()
    r.backup_seconds = round(time.perf_counter() - t0, 3)
    r.backup_bytes = dest.stat().st_size
    t0 = time.perf_counter()
    r.verify = ctx.verify(ctx.alt_target(database=_rel(dest)))
    r.restore_seconds = round(time.perf_counter() - t0, 3)
    r.notes = "sqlite3_backup API (page copy in steps, safe with concurrent writers); the mechanism Litestream/LiteFS build on"
    return r


# --------------------------------------------------------------------------------------------- DuckDB
def s_export_database(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    d = _bdir(ctx)
    exp = d / "export-parquet"
    shutil.rmtree(exp, ignore_errors=True)
    t0 = time.perf_counter()
    ctx.sql_step("EXPORT DATABASE (parquet)", [f"EXPORT DATABASE '{exp}' (FORMAT parquet, COMPRESSION zstd)"])
    r.backup_seconds = round(time.perf_counter() - t0, 3)
    r.backup_bytes = _tree_bytes(exp)
    dest = d / "lab-import.duckdb"
    dest.unlink(missing_ok=True)
    Path(str(dest) + ".wal").unlink(missing_ok=True)
    t0 = time.perf_counter()
    ctx.sql_step("IMPORT DATABASE", [f"ATTACH '{dest}' AS imp", "USE imp", f"IMPORT DATABASE '{exp}'", "USE lab", "DETACH imp"])
    r.restore_seconds = round(time.perf_counter() - t0, 3)
    r.verify = ctx.verify(ctx.alt_target(database=_rel(dest)))
    r.extra["files"] = sorted(p.name for p in exp.iterdir())[:12]
    r.notes = "schema.sql + load.sql + one zstd parquet file per table: portable across DuckDB versions; IMPORT DATABASE replays it"
    return r


def s_copy_from_database(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    d = _bdir(ctx)
    dest = d / "lab-copy.duckdb"
    dest.unlink(missing_ok=True)
    Path(str(dest) + ".wal").unlink(missing_ok=True)
    from ..schema import LOAD_ORDER
    t0 = time.perf_counter()
    # COPY FROM DATABASE copies tables in catalog (alphabetical) order and trips over foreign keys (order_items before
    # orders), so copy the schema with it and the rows table by table in dependency order.
    ctx.sql_step("COPY FROM DATABASE (SCHEMA) + INSERT per table",
                 [f"ATTACH '{dest}' AS cp", "COPY FROM DATABASE lab TO cp (SCHEMA)"] + [f"INSERT INTO cp.{t} SELECT * FROM lab.{t}" for t in LOAD_ORDER] + ["DETACH cp"])
    r.backup_seconds = round(time.perf_counter() - t0, 3)
    r.backup_bytes = dest.stat().st_size
    t0 = time.perf_counter()
    r.verify = ctx.verify(ctx.alt_target(database=_rel(dest)))
    r.restore_seconds = round(time.perf_counter() - t0, 3)
    r.extra["source_bytes"] = os.path.getsize(ctx.engine.db_path(ctx.primary))
    r.notes = "schema copied with COPY FROM DATABASE (SCHEMA), rows inserted per table in FK order (plain COPY FROM DATABASE violates foreign keys because it copies alphabetically)"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    if cfg.dialect == "sqlite":
        return [
            Strategy("vacuum_into", "logical", "VACUUM INTO 'file'", "consistent compacted copy with one SQL statement", s_vacuum_into, requires_container=False),
            Strategy("backup_api", "physical", "sqlite3_backup API", "online page-level copy", s_backup_api, requires_container=False),
        ]
    return [
        Strategy("export_database", "logical", "EXPORT DATABASE (FORMAT parquet) / IMPORT DATABASE", "portable parquet export replayed into a new file", s_export_database, requires_container=False),
        Strategy("copy_from_database", "physical", "ATTACH + COPY FROM DATABASE", "binary copy into a second .duckdb file", s_copy_from_database, requires_container=False),
    ]
