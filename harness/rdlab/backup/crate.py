"""CrateDB backup strategies: repository snapshots (Lucene segment copies, incremental by construction).

  snapshot      CREATE REPOSITORY (fs on the shared /backups volume) + CREATE SNAPSHOT ... ALL, restored with the
                schema_rename_replacement option into a second schema and verified
  incremental   a second snapshot after new writes copies only the new segments; restore drill of the new table
"""
from __future__ import annotations

import time

from ..config import StackConfig
from ..engines import Engine
from ..schema import LOAD_ORDER
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult


def _repo(ctx: Ctx) -> str:
    return ctx.bconf.get("repository", "lab_backups")


def _ensure_repo(ctx: Ctx) -> None:
    c = ctx.engine.connect(ctx.primary)
    try:
        rows = c.execute(f"SELECT name FROM sys.repositories WHERE name = '{_repo(ctx)}'", rendered=True)
        if not rows:
            c.execute(f"CREATE REPOSITORY {_repo(ctx)} TYPE fs WITH (location = '{ctx.backup_dir}/crate', compress = true)", rendered=True, fetch=False)
    finally:
        c.close()


def _restore_schema(ctx: Ctx, snap: str, schema: str, tables: list[str]) -> None:
    c = ctx.engine.connect(ctx.primary)
    try:
        for t in tables:
            try:
                c.execute(f"DROP TABLE IF EXISTS {schema}.{t}", rendered=True, fetch=False)
            except Exception:  # noqa: BLE001
                pass
    finally:
        c.close()
    tl = ", ".join(f"doc.{t}" for t in tables)
    ctx.sql_step(f"RESTORE SNAPSHOT -> schema {schema}", [
        f"RESTORE SNAPSHOT {_repo(ctx)}.{snap} TABLE {tl} WITH (wait_for_completion = true, schema_rename_pattern = 'doc', schema_rename_replacement = '{schema}')"])
    c = ctx.engine.connect(ctx.primary)
    try:
        for t in tables:
            c.execute(f"REFRESH TABLE {schema}.{t}", rendered=True, fetch=False)
    finally:
        c.close()


def s_snapshot(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    ctx.sh("prepare", f"rm -rf {ctx.backup_dir}/crate")
    _ensure_repo(ctx)
    ctx.sql_step("drop old snapshots", [f"DROP SNAPSHOT {_repo(ctx)}.snap1"]) if _snapshot_exists(ctx, "snap1") else None
    t0 = time.perf_counter()
    ctx.sql_step("CREATE SNAPSHOT snap1 ALL", [f"CREATE SNAPSHOT {_repo(ctx)}.snap1 ALL WITH (wait_for_completion = true)"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = ctx.size(f"{ctx.backup_dir}/crate")
    r.extra["snapshot"] = _snapshot_info(ctx, "snap1")
    t0 = time.perf_counter()
    _restore_schema(ctx, "snap1", "restored", LOAD_ORDER)
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    r.verify = ctx.verify(qualify=lambda t: f"restored.{t}")
    c = ctx.engine.connect(ctx.primary)
    try:
        for t in LOAD_ORDER:
            c.execute(f"DROP TABLE IF EXISTS restored.{t}", rendered=True, fetch=False)
    finally:
        c.close()
    r.notes = "fs repository on the shared volume; snapshots copy Lucene segments (compressed); RESTORE ... schema_rename_replacement restores into another schema"
    return r


def _snapshot_exists(ctx: Ctx, name: str) -> bool:
    c = ctx.engine.connect(ctx.primary)
    try:
        return bool(c.execute(f"SELECT name FROM sys.snapshots WHERE repository = '{_repo(ctx)}' AND name = '{name}'", rendered=True))
    finally:
        c.close()


def _snapshot_info(ctx: Ctx, name: str) -> dict:
    c = ctx.engine.connect(ctx.primary)
    try:
        rows = c.execute(f"SELECT state, started, finished, concrete_indices FROM sys.snapshots WHERE repository = '{_repo(ctx)}' AND name = '{name}'", rendered=True)
        return {"state": rows[0][0], "started": str(rows[0][1]), "finished": str(rows[0][2]), "tables": len(rows[0][3] or [])} if rows else {}
    finally:
        c.close()


def s_incremental(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
        c.execute("REFRESH TABLE backup_probe", rendered=True, fetch=False)
    finally:
        c.close()
    before = ctx.size(f"{ctx.backup_dir}/crate") or 0
    ctx.sql_step("drop old snap2", [f"DROP SNAPSHOT {_repo(ctx)}.snap2"]) if _snapshot_exists(ctx, "snap2") else None
    t0 = time.perf_counter()
    ctx.sql_step("CREATE SNAPSHOT snap2 ALL", [f"CREATE SNAPSHOT {_repo(ctx)}.snap2 ALL WITH (wait_for_completion = true)"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = max(0, (ctx.size(f"{ctx.backup_dir}/crate") or 0) - before)
    r.extra["repository_bytes_total"] = ctx.size(f"{ctx.backup_dir}/crate")
    r.extra["snapshot"] = _snapshot_info(ctx, "snap2")
    # the disaster: table dropped, restored from snap2 in place
    ctx.sql_step("DROP TABLE backup_probe", ["DROP TABLE backup_probe"])
    t0 = time.perf_counter()
    ctx.sql_step("RESTORE SNAPSHOT snap2 TABLE backup_probe", [f"RESTORE SNAPSHOT {_repo(ctx)}.snap2 TABLE doc.backup_probe WITH (wait_for_completion = true)"])
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    n = ctx.probe_count()
    r.pitr = {"match": n == PROBE_ROWS, "probe_rows_expected": PROBE_ROWS, "probe_rows_restored": n, "note": "dropped table restored from the second snapshot"}
    r.verify = ctx.verify(label="other tables untouched")
    r.notes = "a second snapshot into the same repository only adds new segment files (incremental by construction)"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("snapshot", "snapshot", "CREATE REPOSITORY fs + CREATE SNAPSHOT ALL / RESTORE SNAPSHOT", "repository snapshot restored into another schema", s_snapshot),
        Strategy("incremental", "incremental", "second CREATE SNAPSHOT (new segments only)", "incremental snapshot, dropped-table restore drill", s_incremental),
    ]
