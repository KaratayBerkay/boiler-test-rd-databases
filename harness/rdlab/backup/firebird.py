"""Firebird backup strategies.

  gbak      logical/transportable backup (gbak -b) restored with gbak -c into a second database file
  nbackup   physical page-level backup: level 0 + level 1 increment, restored with nbackup -R (chain) into a new file
"""
from __future__ import annotations


from ..config import StackConfig
from ..engines import Engine
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult

BIN = "/opt/firebird/bin"


def _auth(ctx: Ctx) -> str:
    return f"-user {ctx.primary.user} -password {ctx.primary.password}"


def _os_user(ctx: Ctx) -> str:
    return ctx.bconf.get("os_user", "firebird")


def s_gbak(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    fbk = f"{ctx.backup_dir}/lab.fbk"
    rdb = db.replace("lab.fdb", "lab_restore.fdb")
    ctx.sh("prepare", f"rm -f {fbk} {rdb}", user=_os_user(ctx))
    st = ctx.sh("gbak -b", f"{BIN}/gbak -b -g {_auth(ctx)} localhost:{db} {fbk}", user=_os_user(ctx))
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(fbk, user=_os_user(ctx))
    st = ctx.sh("gbak -c (restore)", f"{BIN}/gbak -c {_auth(ctx)} {fbk} localhost:{rdb}", user=_os_user(ctx))
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    r.notes = "transportable format (any platform / version), garbage collected on the way; the restore rebuilds the database and its indexes"
    return r


def s_nbackup(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    l0, l1 = f"{ctx.backup_dir}/lab-level0.nbk", f"{ctx.backup_dir}/lab-level1.nbk"
    rdb = db.replace("lab.fdb", "lab_nbk.fdb")
    ctx.sh("prepare", f"rm -f {l0} {l1} {rdb}", user=_os_user(ctx))
    st = ctx.sh("nbackup -B 0", f"{BIN}/nbackup -B 0 {db} {l0} {_auth(ctx)}", user=_os_user(ctx))
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(l0, user=_os_user(ctx))
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
    finally:
        c.close()
    st = ctx.sh("nbackup -B 1 (incremental)", f"{BIN}/nbackup -B 1 {db} {l1} {_auth(ctx)}", user=_os_user(ctx))
    r.extra["incremental"] = {"seconds": round(st.seconds, 2), "bytes": ctx.size(l1, user=_os_user(ctx))}
    st = ctx.sh("nbackup -R (level 0 + 1)", f"{BIN}/nbackup -R {rdb} {l0} {l1}", user=_os_user(ctx))
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    n = ctx.probe_count(ctx.alt_target(database=rdb))
    r.pitr = {"match": n == PROBE_ROWS, "probe_rows_expected": PROBE_ROWS, "probe_rows_restored": n, "note": "rows inserted after level 0 come from the level-1 increment"}
    r.notes = "page-level physical backup with incremental levels (only pages changed since the previous level); restore merges the chain"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("gbak", "logical", "gbak -b / gbak -c", "transportable backup restored into a second database file", s_gbak),
        Strategy("nbackup", "incremental", "nbackup -B 0/1 + nbackup -R", "physical page-level level-0 + level-1 chain restored into a new file", s_nbackup),
    ]
