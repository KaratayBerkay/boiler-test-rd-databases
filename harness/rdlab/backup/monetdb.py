"""MonetDB backup strategies.

  msqldump      logical SQL dump (schema + COPY INTO data) restored into a second database created with `monetdb create`
  hot_snapshot  CALL sys.hot_snapshot('/backups/lab.tar'): consistent tar of the database directory taken by the server,
                restored by extracting it into the dbfarm under a new name
"""
from __future__ import annotations

import time

from ..config import StackConfig
from ..engines import Engine
from .common import Ctx, Strategy, StrategyResult

DOT = "printf 'user=monetdb\\npassword=%s\\n' > /tmp/.monetdb; export DOTMONETDBFILE=/tmp/.monetdb; "


def _env(ctx: Ctx) -> str:
    return DOT % ctx.bconf.get("admin_password", ctx.primary.password)


def _new_db(ctx: Ctx, name: str) -> None:
    """Create a database in the dbfarm; a fresh database starts with monetdb/monetdb, so align the admin password."""
    pw = ctx.bconf.get("admin_password", ctx.primary.password)
    ctx.sh(f"monetdb create {name}", f"monetdb stop {name} >/dev/null 2>&1; monetdb destroy -f {name} >/dev/null 2>&1; monetdb create {name} && monetdb release {name} && "
           f"printf 'user=monetdb\\npassword=monetdb\\n' > /tmp/.monetdb-new && DOTMONETDBFILE=/tmp/.monetdb-new mclient -d {name} -s \"ALTER USER SET PASSWORD '{pw}' USING OLD PASSWORD 'monetdb'\"")


def s_msqldump(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    f = f"{ctx.backup_dir}/{db}.sql"
    st = ctx.sh("msqldump", f"{_env(ctx)} msqldump -d {db} > {f}")
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(f)
    rdb = f"{db}_restore"
    _new_db(ctx, rdb)
    st = ctx.sh("mclient < dump", f"{_env(ctx)} mclient -d {rdb} < {f} > /dev/null")
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    ctx.sh("destroy restore db", f"monetdb stop {rdb} >/dev/null 2>&1; monetdb destroy -f {rdb}", check=False)
    r.notes = "SQL dump with COPY INTO blocks; the new database is created in the same dbfarm with the monetdb control tool"
    return r


def s_hot_snapshot(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    tarball = f"{ctx.backup_dir}/{db}-hot.tar"
    farm = ctx.bconf.get("dbfarm", "/var/monetdb5/dbfarm")
    ctx.sh("prepare", f"rm -f {tarball}")
    t0 = time.perf_counter()
    ctx.sql_step("CALL sys.hot_snapshot", [f"CALL sys.hot_snapshot('{tarball}')"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = ctx.size(tarball)
    rdb = f"{db}_snap"
    ctx.sh("remove old snapshot db", f"monetdb stop {rdb} >/dev/null 2>&1; monetdb destroy -f {rdb} >/dev/null 2>&1; rm -rf {farm}/{rdb}; true")
    t0 = time.perf_counter()
    ctx.sh("extract into dbfarm", f"tar -C {farm} -xf {tarball} --transform 's,^{db}/,{rdb}/,' --transform 's,^{db}$,{rdb},' && chown -R monetdb:monetdb {farm}/{rdb} 2>/dev/null; monetdb release {rdb} >/dev/null 2>&1; true")
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    r.restore_seconds = round(r.restore_seconds + r.verify.get("seconds", 0) * 0, 2)
    ctx.sh("destroy snapshot db", f"monetdb stop {rdb} >/dev/null 2>&1; monetdb destroy -f {rdb}", check=False)
    r.notes = "server-side consistent tar of the database directory (no client round trips); restore = untar under a new name in the dbfarm"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("msqldump", "logical", "msqldump | mclient", "SQL dump restored into a second database", s_msqldump),
        Strategy("hot_snapshot", "physical", "CALL sys.hot_snapshot(tar)", "server-side consistent tar restored under a new name", s_hot_snapshot),
    ]
