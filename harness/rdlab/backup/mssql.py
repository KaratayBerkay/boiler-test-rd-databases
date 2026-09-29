"""SQL Server backup strategies (native T-SQL BACKUP/RESTORE, the database is in FULL recovery inside an Availability Group).

  full          BACKUP DATABASE ... WITH COMPRESSION, CHECKSUM; RESTORE VERIFYONLY; RESTORE DATABASE ... WITH MOVE as lab_restore
  differential  BACKUP DATABASE ... WITH DIFFERENTIAL; restore full (NORECOVERY) + differential
  log_pitr      BACKUP LOG; restore full + differential + log WITH STOPAT = '<before the DELETE>'
"""
from __future__ import annotations

import time

from ..config import StackConfig
from ..engines import Engine
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult


def _st(ctx: Ctx) -> dict:
    return ctx.__dict__.setdefault("_msstate", {})


def _datadir(ctx: Ctx) -> str:
    return ctx.bconf.get("data_dir", "/var/opt/mssql/data")


def _filelist(ctx: Ctx, bak: str) -> list[tuple[str, str]]:
    rows = ctx.sql_step("RESTORE FILELISTONLY", [f"RESTORE FILELISTONLY FROM DISK = N'{bak}'"], fetch_last=True)
    return [(r[0], r[2]) for r in rows]      # (LogicalName, Type)


def _move_clause(ctx: Ctx, bak: str, rdb: str) -> str:
    parts = []
    for logical, typ in _filelist(ctx, bak):
        ext = "ldf" if typ == "L" else "mdf"
        parts.append(f"MOVE N'{logical}' TO N'{_datadir(ctx)}/{rdb}_{logical}.{ext}'")
    return ", ".join(parts)


def _drop(ctx: Ctx, rdb: str) -> None:
    ctx.sql_step(f"drop {rdb}", [f"IF DB_ID(N'{rdb}') IS NOT NULL BEGIN ALTER DATABASE [{rdb}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{rdb}]; END"])


def s_full(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    bak = f"{ctx.backup_dir}/{db}-full.bak"
    ctx.sh("prepare", f"rm -f {ctx.backup_dir}/{db}-*.bak {ctx.backup_dir}/{db}-*.trn")
    t0 = time.perf_counter()
    ctx.sql_step("BACKUP DATABASE (full)", [f"BACKUP DATABASE [{db}] TO DISK = N'{bak}' WITH INIT, COMPRESSION, CHECKSUM, STATS = 25"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = ctx.size(bak)
    ctx.sql_step("RESTORE VERIFYONLY", [f"RESTORE VERIFYONLY FROM DISK = N'{bak}' WITH CHECKSUM"])
    rows = ctx.sql_step("backup history", [f"SELECT TOP 1 backup_size, compressed_backup_size, DATEDIFF(ms, backup_start_date, backup_finish_date) FROM msdb.dbo.backupset WHERE database_name = N'{db}' AND type = 'D' ORDER BY backup_finish_date DESC"], fetch_last=True)
    if rows:
        r.extra["msdb_backupset"] = {"backup_size": int(rows[0][0]), "compressed_backup_size": int(rows[0][1]), "ms": rows[0][2]}
    rdb = f"{db}_restore"
    _drop(ctx, rdb)
    t0 = time.perf_counter()
    ctx.sql_step("RESTORE DATABASE (as lab_restore)", [f"RESTORE DATABASE [{rdb}] FROM DISK = N'{bak}' WITH {_move_clause(ctx, bak, rdb)}, RECOVERY, STATS = 25"])
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    _drop(ctx, rdb)
    _st(ctx)["full"] = bak
    r.notes = "compressed, checksummed full backup verified with RESTORE VERIFYONLY and restored under a new name (WITH MOVE); history in msdb.dbo.backupset"
    return r


def s_differential(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    full = _st(ctx).get("full")
    if not full:
        r.status = "n/a"
        r.notes = "needs the full backup"
        return r
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
    finally:
        c.close()
    diff = f"{ctx.backup_dir}/{db}-diff.bak"
    t0 = time.perf_counter()
    ctx.sql_step("BACKUP DATABASE WITH DIFFERENTIAL", [f"BACKUP DATABASE [{db}] TO DISK = N'{diff}' WITH DIFFERENTIAL, INIT, COMPRESSION, CHECKSUM"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = ctx.size(diff)
    rdb = f"{db}_restore2"
    _drop(ctx, rdb)
    t0 = time.perf_counter()
    ctx.sql_step("RESTORE full NORECOVERY + differential", [
        f"RESTORE DATABASE [{rdb}] FROM DISK = N'{full}' WITH {_move_clause(ctx, full, rdb)}, NORECOVERY",
        f"RESTORE DATABASE [{rdb}] FROM DISK = N'{diff}' WITH RECOVERY"])
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    n = ctx.probe_count(ctx.alt_target(database=rdb))
    r.pitr = {"match": n == PROBE_ROWS, "probe_rows_expected": PROBE_ROWS, "probe_rows_restored": n, "note": "table created after the full backup comes from the differential"}
    _drop(ctx, rdb)
    _st(ctx)["diff"] = diff
    r.notes = "extents changed since the last full backup (differential bitmap); restore = full WITH NORECOVERY then the differential"
    return r


def s_log_pitr(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    full, diff = _st(ctx).get("full"), _st(ctx).get("diff")
    if not (full and diff):
        r.status = "n/a"
        r.notes = "needs the full and differential backups"
        return r
    c = ctx.engine.connect(ctx.primary)
    try:
        time.sleep(1.2)
        ts = c.execute("SELECT CONVERT(varchar(23), GETDATE(), 121)", rendered=True)[0][0]
        time.sleep(1.2)
        c.execute("DELETE FROM backup_probe", rendered=True, fetch=False)
        n_after = ctx.probe_count(conn=c)
    finally:
        c.close()
    trn = f"{ctx.backup_dir}/{db}-log1.trn"
    t0 = time.perf_counter()
    ctx.sql_step("BACKUP LOG", [f"BACKUP LOG [{db}] TO DISK = N'{trn}' WITH INIT, COMPRESSION, CHECKSUM"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = ctx.size(trn)
    rdb = f"{db}_pitr"
    _drop(ctx, rdb)
    t0 = time.perf_counter()
    ctx.sql_step("RESTORE full + diff + LOG WITH STOPAT", [
        f"RESTORE DATABASE [{rdb}] FROM DISK = N'{full}' WITH {_move_clause(ctx, full, rdb)}, NORECOVERY",
        f"RESTORE DATABASE [{rdb}] FROM DISK = N'{diff}' WITH NORECOVERY",
        f"RESTORE LOG [{rdb}] FROM DISK = N'{trn}' WITH STOPAT = N'{ts}', RECOVERY"])
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    n = ctx.probe_count(ctx.alt_target(database=rdb))
    r.pitr = {"match": n == PROBE_ROWS, "target": f"STOPAT = '{ts}'", "probe_rows_after_delete": n_after, "probe_rows_recovered": n, "probe_rows_expected": PROBE_ROWS}
    _drop(ctx, rdb)
    r.notes = "FULL recovery model: the log backup carries every transaction; RESTORE LOG ... WITH STOPAT replays up to the timestamp before the DELETE"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("full", "physical", "BACKUP DATABASE WITH COMPRESSION, CHECKSUM + RESTORE VERIFYONLY", "compressed full backup restored under a new name", s_full),
        Strategy("differential", "incremental", "BACKUP DATABASE WITH DIFFERENTIAL", "differential restored on top of the full backup", s_differential),
        Strategy("log_pitr", "pitr", "BACKUP LOG + RESTORE LOG WITH STOPAT", "point-in-time restore from the transaction log", s_log_pitr),
    ]
