"""ClickHouse backup strategies (native BACKUP / RESTORE statements, ClickHouse 22.8+).

  backup_disk      BACKUP DATABASE lab TO Disk('backups', 'full') on one replica, RESTORE ... AS lab_restore (the replicated
                   tables use the {uuid} Keeper path, so the restored copy gets its own path)
  backup_cluster   BACKUP DATABASE lab ON CLUSTER lab_cluster: every shard backed up once, replicas share the work; the drill
                   drops one table ON CLUSTER and restores it in place with RESTORE TABLE ... ON CLUSTER
  incremental      BACKUP ... SETTINGS base_backup = Disk('backups', 'full'): only new parts are written
No PITR: MergeTree parts are immutable, so the recovery unit is "the parts that existed at backup time".
"""
from __future__ import annotations

import time

from ..config import StackConfig
from ..engines import Engine
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult


def _disk(ctx: Ctx) -> str:
    return ctx.bconf.get("disk", "backups")


def _status(ctx: Ctx, name: str) -> dict:
    rows = ctx.engine.connect(ctx.primary).execute(
        f"SELECT status, num_files, total_size, num_entries, uncompressed_size, compressed_size, error, toString(start_time), toString(end_time) "
        f"FROM system.backups WHERE name LIKE '%{name}%' ORDER BY start_time DESC LIMIT 1", rendered=True)
    if not rows:
        return {}
    r = rows[0]
    return {"status": r[0], "num_files": r[1], "total_size": r[2], "num_entries": r[3], "uncompressed_size": r[4], "compressed_size": r[5],
            "error": r[6], "start": r[7], "end": r[8]}


def _restore_verify(ctx: Ctx, backup_name: str, rdb: str, *, extra_probe: bool = False) -> None:
    r = ctx.res
    ctx.sql_step("drop old restore db", [f"DROP DATABASE IF EXISTS {rdb} SYNC"])
    t0 = time.perf_counter()
    ctx.sql_step(f"RESTORE DATABASE AS {rdb}", [f"RESTORE DATABASE {ctx.primary.database} AS {rdb} FROM Disk('{_disk(ctx)}', '{backup_name}')"])
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    if extra_probe:
        n = ctx.probe_count(ctx.alt_target(database=rdb))
        r.pitr = {"match": n == PROBE_ROWS, "probe_rows_expected": PROBE_ROWS, "probe_rows_restored": n, "note": "table created after the base backup restored from the incremental"}
    ctx.sql_step("drop restore db", [f"DROP DATABASE {rdb} SYNC"])


def s_backup_disk(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    name = "full"
    ctx.sh("prepare", f"rm -rf {ctx.backup_dir}/{name} {ctx.backup_dir}/inc1 {ctx.backup_dir}/cluster")
    t0 = time.perf_counter()
    ctx.sql_step("BACKUP DATABASE TO Disk", [f"BACKUP DATABASE {ctx.primary.database} TO Disk('{_disk(ctx)}', '{name}')"], fetch_last=True)
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    st = _status(ctx, name)
    r.extra["system_backups"] = st
    r.backup_bytes = ctx.size(f"{ctx.backup_dir}/{name}")          # bytes on disk (system.backups.total_size counts base-backup files too)
    _restore_verify(ctx, name, f"{ctx.primary.database}_restore")
    r.notes = "single-replica backup of every table's parts + metadata into the `backups` disk (/backups); restored under a new database name on the same server"
    return r


def s_backup_cluster(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    cluster = ctx.cfg.features.get("cluster", "lab_cluster")
    name = "cluster"
    t0 = time.perf_counter()
    ctx.sql_step("BACKUP DATABASE ON CLUSTER", [f"BACKUP DATABASE {ctx.primary.database} ON CLUSTER {cluster} TO Disk('{_disk(ctx)}', '{name}')"], fetch_last=True)
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    st = _status(ctx, name)
    r.extra["system_backups"] = st
    r.backup_bytes = ctx.size(f"{ctx.backup_dir}/{name}")          # bytes on disk (system.backups.total_size counts base-backup files too)
    db = ctx.primary.database
    ctx.sql_step("DROP TABLE inventory ON CLUSTER", [f"DROP TABLE {db}.inventory ON CLUSTER {cluster} SYNC"])
    t0 = time.perf_counter()
    ctx.sql_step("RESTORE TABLE inventory ON CLUSTER", [f"RESTORE TABLE {db}.inventory ON CLUSTER {cluster} FROM Disk('{_disk(ctx)}', '{name}')"])
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    r.verify = ctx.verify(label="after in-place RESTORE TABLE")
    rep = ctx.cfg.replicas[0] if ctx.cfg.replicas else None
    if rep:
        v2 = ctx.verify(rep, tables=["inventory"], label="inventory on replica")
        r.extra["replica_verify"] = v2
    r.notes = "cluster-coordinated backup (each shard once, the replicas split the parts); drill: DROP TABLE ... ON CLUSTER then RESTORE TABLE ... ON CLUSTER in place"
    return r


def s_incremental(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
    finally:
        c.close()
    name = "inc1"
    t0 = time.perf_counter()
    ctx.sql_step("BACKUP ... base_backup", [f"BACKUP DATABASE {ctx.primary.database} TO Disk('{_disk(ctx)}', '{name}') SETTINGS base_backup = Disk('{_disk(ctx)}', 'full')"], fetch_last=True)
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    st = _status(ctx, name)
    r.extra["system_backups"] = st
    r.backup_bytes = ctx.size(f"{ctx.backup_dir}/{name}")          # bytes on disk (system.backups.total_size counts base-backup files too)
    _restore_verify(ctx, name, f"{ctx.primary.database}_restore3", extra_probe=True)
    r.notes = "only parts not present in the base backup are written; RESTORE from the incremental resolves the base automatically"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("backup_disk", "physical", "BACKUP DATABASE TO Disk('backups', ...)", "native backup of parts + metadata, restored as a new database", s_backup_disk),
        Strategy("backup_cluster", "physical", "BACKUP DATABASE ON CLUSTER", "cluster-coordinated backup (shards once, replicas share)", s_backup_cluster),
        Strategy("incremental", "incremental", "BACKUP ... SETTINGS base_backup", "incremental on top of the full backup", s_incremental),
    ]
