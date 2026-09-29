"""CockroachDB backup strategies (native BACKUP/RESTORE, free in v24.3+ for self-hosted "Enterprise Free"/Core).

  full          BACKUP DATABASE lab INTO 'nodelocal://1/lab' WITH revision_history; RESTORE ... WITH new_db_name
  incremental   BACKUP DATABASE lab INTO LATEST IN 'nodelocal://1/lab' (only changes since the previous backup)
  pitr          RESTORE ... FROM LATEST IN ... AS OF SYSTEM TIME '<before the DELETE>' (needs revision_history)
nodelocal://1 is node 1's external I/O directory (<store>/extern), reachable by the other nodes through node 1.
"""
from __future__ import annotations

import time

from ..config import StackConfig
from ..engines import Engine
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult


def _st(ctx: Ctx) -> dict:
    return ctx.__dict__.setdefault("_crdbstate", {})


def _uri(ctx: Ctx) -> str:
    return ctx.bconf.get("uri", "nodelocal://1/lab")


def _extern_dir(ctx: Ctx) -> str:
    return ctx.bconf.get("extern_dir", "/cockroach/cockroach-data/extern/lab")


def _show_backup(ctx: Ctx) -> dict:
    c = ctx.engine.connect(ctx.primary)
    try:
        rows = c.execute(f"SELECT database_name, object_name, backup_type, rows, size_bytes FROM [SHOW BACKUP LATEST IN '{_uri(ctx)}'] WHERE object_type = 'table'", rendered=True)
        jobs = c.execute("SELECT job_type, status, running_status, description, created::STRING, finished::STRING FROM [SHOW JOBS] "
                         "WHERE job_type IN ('BACKUP', 'RESTORE') ORDER BY created DESC LIMIT 3", rendered=True)
    finally:
        c.close()
    return {"tables": [{"table": r[1], "type": r[2], "rows": r[3], "size_bytes": r[4]} for r in rows],
            "size_bytes": sum(int(r[4] or 0) for r in rows), "jobs": [list(map(str, j)) for j in jobs]}


def _restore_as(ctx: Ctx, rdb: str, *, as_of: str | None = None) -> float:
    aost = f" AS OF SYSTEM TIME '{as_of}'" if as_of else ""
    ctx.sql_step(f"drop {rdb} if exists", [f"DROP DATABASE IF EXISTS {rdb} CASCADE"])
    t0 = time.perf_counter()
    ctx.sql_step(f"RESTORE DATABASE AS {rdb}", [f"RESTORE DATABASE {ctx.primary.database} FROM LATEST IN '{_uri(ctx)}'{aost} WITH new_db_name = '{rdb}'"])
    return round(time.perf_counter() - t0, 2)


def s_full(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    ctx.sh("prepare", f"rm -rf {_extern_dir(ctx)}")
    t0 = time.perf_counter()
    ctx.sql_step("BACKUP DATABASE (full, revision_history)", [f"BACKUP DATABASE {db} INTO '{_uri(ctx)}' WITH revision_history"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.extra["show_backup"] = _show_backup(ctx)
    r.backup_bytes = ctx.size(_extern_dir(ctx)) or r.extra["show_backup"]["size_bytes"]
    rdb = f"{db}_restore"
    r.restore_seconds = _restore_as(ctx, rdb)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    ctx.sql_step("drop restore db", [f"DROP DATABASE {rdb} CASCADE"])
    _st(ctx)["full"] = True
    r.notes = "distributed backup job (every node exports its ranges as SSTs) with MVCC revision history; restored under a new database name"
    return r


def s_incremental(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    if not _st(ctx).get("full"):
        r.status = "n/a"
        r.notes = "needs the full backup"
        return r
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
        _st(ctx)["ts_before_delete"] = str(c.execute("SELECT cluster_logical_timestamp()", rendered=True)[0][0])
    finally:
        c.close()
    before = ctx.size(_extern_dir(ctx)) or 0
    t0 = time.perf_counter()
    ctx.sql_step("BACKUP DATABASE INTO LATEST (incremental)", [f"BACKUP DATABASE {db} INTO LATEST IN '{_uri(ctx)}' WITH revision_history"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = max(0, (ctx.size(_extern_dir(ctx)) or 0) - before)
    r.extra["show_backup"] = _show_backup(ctx)
    rdb = f"{db}_restore2"
    r.restore_seconds = _restore_as(ctx, rdb)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    n = ctx.probe_count(ctx.alt_target(database=rdb))
    r.pitr = {"match": n == PROBE_ROWS, "probe_rows_expected": PROBE_ROWS, "probe_rows_restored": n, "note": "table created after the full backup comes from the incremental layer"}
    ctx.sql_step("drop restore db", [f"DROP DATABASE {rdb} CASCADE"])
    r.notes = "incremental layer appended to the same collection (INTO LATEST IN); RESTORE FROM LATEST applies full + incrementals"
    return r


def s_pitr(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    ts = _st(ctx).get("ts_before_delete")
    if not ts:
        r.status = "n/a"
        r.notes = "needs the incremental strategy (timestamp before the disaster)"
        return r
    c = ctx.engine.connect(ctx.primary)
    try:
        time.sleep(0.5)
        c.execute("DELETE FROM backup_probe", rendered=True, fetch=False)
        n_after = ctx.probe_count(conn=c)
        stale = c.execute(f"SELECT count(*) FROM backup_probe AS OF SYSTEM TIME '{ts}'", rendered=True)[0][0]
    finally:
        c.close()
    r.extra["stale_read"] = {"as_of": ts, "rows": int(stale), "current_rows": n_after}
    ctx.log(f"    AS OF SYSTEM TIME stale read: {stale} rows (current {n_after})")
    before = ctx.size(_extern_dir(ctx)) or 0
    t0 = time.perf_counter()
    ctx.sql_step("BACKUP INTO LATEST (captures the DELETE)", [f"BACKUP DATABASE {db} INTO LATEST IN '{_uri(ctx)}' WITH revision_history"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = max(0, (ctx.size(_extern_dir(ctx)) or 0) - before)
    rdb = f"{db}_pitr"
    r.restore_seconds = _restore_as(ctx, rdb, as_of=ts)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    n = ctx.probe_count(ctx.alt_target(database=rdb))
    r.pitr = {"match": n == PROBE_ROWS, "target": f"AS OF SYSTEM TIME '{ts}'", "probe_rows_after_delete": n_after, "probe_rows_recovered": n,
              "probe_rows_expected": PROBE_ROWS, "stale_read_rows": int(stale)}
    ctx.sql_step("drop pitr db", [f"DROP DATABASE {rdb} CASCADE"])
    r.notes = "revision_history keeps every MVCC version in the backup chain; RESTORE ... AS OF SYSTEM TIME picks the state before the DELETE"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("full", "physical", "BACKUP DATABASE INTO 'nodelocal://1/lab' WITH revision_history", "distributed full backup restored under a new name", s_full),
        Strategy("incremental", "incremental", "BACKUP DATABASE INTO LATEST IN", "incremental layer on the same collection", s_incremental),
        Strategy("pitr", "pitr", "RESTORE ... AS OF SYSTEM TIME", "point-in-time restore from the revision history", s_pitr),
    ]
