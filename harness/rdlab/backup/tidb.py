"""TiDB backup strategies (BR is embedded in tidb-server; the SQL BACKUP/RESTORE statements drive it).

  br_full        BACKUP DATABASE lab TO 'local:///backups/full' (every TiKV writes its SSTs into the shared volume),
                 then DROP TABLE inventory + RESTORE TABLE lab.inventory FROM the backup (BR cannot restore under a new name)
  br_incremental BACKUP ... LAST_BACKUP = <BackupTS of the full backup>, RESTORE TABLE of a table created after the full backup
  flashback      FLASHBACK TABLE after DROP TABLE (MVCC history within tidb_gc_life_time)
  pitr_cluster   stale read AS OF TIMESTAMP + FLASHBACK CLUSTER TO TIMESTAMP: whole-cluster in-place point-in-time recovery
"""
from __future__ import annotations

import time

from ..config import StackConfig
from ..engines import Engine
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult


def _st(ctx: Ctx) -> dict:
    return ctx.__dict__.setdefault("_tidbstate", {})


def _size_all_nodes(ctx: Ctx, path: str) -> int:
    """Sum of `path` over the tikv/tidb containers listed in lab.yaml backup.containers (the volume is shared, so
    a single measurement is enough when the same volume is mounted everywhere)."""
    conts = ctx.bconf.get("size_containers") or [ctx.container]
    tot = 0
    for c in conts:
        tot += ctx.size(path, container=c) or 0
    return tot


def _backup(ctx: Ctx, name: str, dest: str, *, last_backup: str | None = None) -> dict:
    sql = f"BACKUP DATABASE {ctx.primary.database} TO '{dest}'"
    if last_backup:
        sql += f" LAST_BACKUP = {last_backup}"
    rows = ctx.sql_step(name, [sql], fetch_last=True)
    r = rows[0] if rows else ()
    # Destination | Size | BackupTS | Queue Time | Execution Time
    return {"destination": str(r[0]) if r else dest, "size_bytes": int(r[1]) if len(r) > 1 else None, "backup_ts": str(r[2]) if len(r) > 2 else None,
            "queue_time": str(r[3]) if len(r) > 3 else None, "execution_time": str(r[4]) if len(r) > 4 else None}


def s_br_full(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    dest = f"local://{ctx.backup_dir}/full"
    ctx.sh("prepare", f"rm -rf {ctx.backup_dir}/full {ctx.backup_dir}/inc1", container=ctx.bconf.get("shell_container", ctx.container))
    t0 = time.perf_counter()
    info = _backup(ctx, "BACKUP DATABASE (full)", dest)
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = info.get("size_bytes") or _size_all_nodes(ctx, f"{ctx.backup_dir}/full")
    r.extra["backup"] = info
    r.extra["dir_bytes_on_volume"] = _size_all_nodes(ctx, f"{ctx.backup_dir}/full")
    _st(ctx)["full_ts"] = info.get("backup_ts")
    # restore drill: drop one table and bring it back from the backup
    ctx.sql_step("DROP TABLE inventory", ["DROP TABLE inventory"])
    t0 = time.perf_counter()
    rows = ctx.sql_step("RESTORE TABLE inventory", [f"RESTORE TABLE {ctx.primary.database}.inventory FROM '{dest}'"], fetch_last=True)
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    r.extra["restore"] = str(rows[0])[:200] if rows else None
    r.verify = ctx.verify(label="after RESTORE TABLE")
    r.notes = "BR embedded in tidb-server; SSTs written by every TiKV into the shared volume; restore is in place (BR cannot rename), so the drill drops and restores one table"
    return r


def s_br_incremental(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    full_ts = _st(ctx).get("full_ts")
    if not full_ts:
        r.status = "n/a"
        r.notes = "needs the full backup"
        return r
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
    finally:
        c.close()
    dest = f"local://{ctx.backup_dir}/inc1"
    t0 = time.perf_counter()
    info = _backup(ctx, "BACKUP DATABASE (incremental)", dest, last_backup=full_ts)
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = info.get("size_bytes") or _size_all_nodes(ctx, f"{ctx.backup_dir}/inc1")
    r.extra["backup"] = info
    r.extra["full_backup_ts"] = full_ts
    ctx.sql_step("DROP TABLE backup_probe", ["DROP TABLE backup_probe"])
    t0 = time.perf_counter()
    ctx.sql_step("RESTORE TABLE backup_probe (from incremental)", [f"RESTORE TABLE {ctx.primary.database}.backup_probe FROM '{dest}'"], fetch_last=True)
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    n = ctx.probe_count()
    r.pitr = {"match": n == PROBE_ROWS, "probe_rows_expected": PROBE_ROWS, "probe_rows_restored": n,
              "note": "table created after the full backup restored from the incremental alone (DDL + rows since LAST_BACKUP)"}
    r.verify = ctx.verify(label="other tables untouched")
    r.notes = "BACKUP ... LAST_BACKUP = <BackupTS> ships only KV changes after the full backup"
    return r


def s_flashback_table(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
        gc = c.execute("SELECT @@global.tidb_gc_life_time", rendered=True)[0][0]
    finally:
        c.close()
    r.extra["tidb_gc_life_time"] = str(gc)
    ctx.sql_step("DROP TABLE backup_probe", ["DROP TABLE backup_probe"])
    t0 = time.perf_counter()
    ctx.sql_step("FLASHBACK TABLE backup_probe", ["FLASHBACK TABLE backup_probe"])
    r.restore_seconds = round(time.perf_counter() - t0, 3)
    r.backup_seconds = 0.0
    r.backup_bytes = 0
    n = ctx.probe_count()
    r.pitr = {"match": n == PROBE_ROWS, "probe_rows_expected": PROBE_ROWS, "probe_rows_recovered": n, "window": f"tidb_gc_life_time={gc}"}
    r.verify = {"match": n == PROBE_ROWS, "tables": 1, "rows": n, "mismatch": [], "note": "dropped table only"}
    r.notes = "no backup needed: the dropped table's data is still in TiKV MVCC history until GC (tidb_gc_life_time)"
    return r


def s_pitr_cluster(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
        time.sleep(1.2)
        ts = c.execute("SELECT NOW(6)", rendered=True)[0][0]
        ts_s = ts.strftime("%Y-%m-%d %H:%M:%S.%f") if hasattr(ts, "strftime") else str(ts)
        time.sleep(1.2)
        c.execute("DELETE FROM backup_probe", rendered=True, fetch=False)
        n_after = ctx.probe_count(conn=c)
        stale = c.execute(f"SELECT COUNT(*) FROM backup_probe AS OF TIMESTAMP '{ts_s}'", rendered=True)[0][0]
    finally:
        c.close()
    r.extra["stale_read"] = {"as_of": ts_s, "rows": int(stale), "current_rows": n_after}
    ctx.log(f"    AS OF TIMESTAMP stale read: {stale} rows (current {n_after})")
    t0 = time.perf_counter()
    try:
        ctx.sql_step("FLASHBACK CLUSTER TO TIMESTAMP", [f"FLASHBACK CLUSTER TO TIMESTAMP '{ts_s}'"])
    except Exception as e:  # noqa: BLE001
        # the statement disconnects sessions while it runs; retry the count after a short wait if the error is a lost connection
        if "Lost connection" not in str(e) and "closed" not in str(e).lower() and "Broken pipe" not in str(e):
            raise
    time.sleep(2.0)
    n = None
    for _ in range(30):
        try:
            n = ctx.probe_count()
            break
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    r.backup_seconds = 0.0
    r.backup_bytes = 0
    r.pitr = {"match": n == PROBE_ROWS, "target": f"FLASHBACK CLUSTER TO TIMESTAMP '{ts_s}'", "probe_rows_after_delete": n_after,
              "probe_rows_after_flashback": n, "probe_rows_expected": PROBE_ROWS, "stale_read_rows": int(stale)}
    r.verify = ctx.verify(label="all tables after cluster flashback")
    r.notes = "whole-cluster in-place PITR from MVCC history (blocks reads/writes while running; only within tidb_gc_life_time)"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("br_full", "physical", "BACKUP DATABASE ... TO 'local://'", "BR full backup to the shared volume, RESTORE TABLE drill", s_br_full),
        Strategy("br_incremental", "incremental", "BACKUP ... LAST_BACKUP = ts", "incremental backup since the full BackupTS, restore of a new table from it", s_br_incremental),
        Strategy("flashback", "flashback", "FLASHBACK TABLE", "recover a dropped table from MVCC history", s_flashback_table),
        Strategy("pitr_cluster", "pitr", "AS OF TIMESTAMP + FLASHBACK CLUSTER TO TIMESTAMP", "stale read of the pre-disaster state and in-place cluster rollback", s_pitr_cluster),
    ]
