"""H2 backup strategies (server mode, PostgreSQL wire).

  backup_zip   BACKUP TO 'file.zip' (online, consistent zip of the database file), restored with the Restore tool under a new name
  script       SCRIPT TO 'file.sql' (DDL + data) replayed with RUNSCRIPT FROM into a new database
"""
from __future__ import annotations

import time

from ..config import StackConfig
from ..engines import Engine
from .common import Ctx, Strategy, StrategyResult


def s_backup_zip(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    z = f"{ctx.backup_dir}/lab.zip"
    ctx.sh("prepare", f"rm -f {z} /data/lab_restore.mv.db /data/lab_restore.trace.db")
    t0 = time.perf_counter()
    ctx.sql_step("BACKUP TO", [f"BACKUP TO '{z}'"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = ctx.size(z)
    st = ctx.sh("org.h2.tools.Restore", f"java -cp /opt/h2.jar org.h2.tools.Restore -file {z} -dir /data -db lab_restore")
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(ctx.alt_target(database="lab_restore"))
    r.notes = "online zip of the .mv.db file taken by the server; the Restore tool unpacks it under a new database name"
    return r


def s_script(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    f = f"{ctx.backup_dir}/lab.sql"
    ctx.sh("prepare", f"rm -f {f} /data/lab_script.mv.db /data/lab_script.trace.db")
    t0 = time.perf_counter()
    ctx.sql_step("SCRIPT TO", [f"SCRIPT TO '{f}'"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = ctx.size(f)
    t0 = time.perf_counter()
    ctx.sql_step("RUNSCRIPT FROM (new db)", [f"RUNSCRIPT FROM '{f}'"], target=ctx.alt_target(database="lab_script"))
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    r.verify = ctx.verify(ctx.alt_target(database="lab_script"))
    r.notes = "SQL script (DDL + INSERTs) replayed into a fresh database through the same connection type"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("backup_zip", "physical", "BACKUP TO 'lab.zip' + org.h2.tools.Restore", "online zip of the database file restored under a new name", s_backup_zip),
        Strategy("script", "logical", "SCRIPT TO / RUNSCRIPT FROM", "SQL script replayed into a new database", s_script),
    ]
