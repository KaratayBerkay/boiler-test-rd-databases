"""YugabyteDB backup strategies.

  ysql_dump   logical dump (pg_dump fork that understands YB DDL) restored into a second database
  snapshot    distributed in-cluster snapshot (yb-admin create_database_snapshot / restore_snapshot): tablet-level
              copy-on-write snapshot on every node, restored in place after a destructive DELETE
  pitr        snapshot schedule (create_snapshot_schedule) + restore_snapshot_schedule <timestamp>: in-place
              point-in-time recovery from the retained history
"""
from __future__ import annotations

import json
import re
import time

from ..config import StackConfig
from ..engines import Engine
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult


def _masters(ctx: Ctx) -> str:
    return ctx.bconf.get("masters", "yb1:7100,yb2:7100,yb3:7100")


def _admin(ctx: Ctx, name: str, args: str, *, timeout: int = 600, check: bool = True):
    return ctx.sh(name, f"cd /home/yugabyte && bin/yb-admin -master_addresses {_masters(ctx)} {args}", timeout=timeout, check=check)


def _admin_quiet(ctx: Ctx, args: str, *, timeout: int = 120) -> str:
    """yb-admin without recording a step (polling)."""
    from .. import dockerctl
    rc, out, err = dockerctl.exec_in(ctx.container, ["sh", "-c", f"cd /home/yugabyte && bin/yb-admin -master_addresses {_masters(ctx)} {args}"], timeout=timeout)
    return out + err


def _state_of(out: str, ident: str) -> str | None:
    """State of the entry `ident` in yb-admin output (JSON or the tab-separated table form)."""
    try:
        doc = json.loads(out)
        for lst in doc.values():
            if isinstance(lst, list):
                for e in lst:
                    if isinstance(e, dict) and e.get("id") == ident:
                        return e.get("state")
    except json.JSONDecodeError:
        pass
    for line in out.splitlines():
        if ident in line:
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return None


def _wait_state(ctx: Ctx, list_cmd: str, ident: str, wanted: str, *, timeout: float = 300.0) -> float:
    t0 = time.perf_counter()
    last = None
    while time.perf_counter() - t0 < timeout:
        last = _state_of(_admin_quiet(ctx, list_cmd), ident)
        if last == wanted:
            ctx.log(f"    {ident[:8]} {wanted} after {time.perf_counter() - t0:.1f}s")
            return time.perf_counter() - t0
        time.sleep(1.0)
    raise TimeoutError(f"{ident} did not reach {wanted} in {timeout}s (last state {last})")


def s_ysql_dump(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    host = ctx.bconf.get("host", "yb1")
    db, user = ctx.primary.database, ctx.primary.user
    f = f"{ctx.backup_dir}/{db}.sql"
    st = ctx.sh("ysql_dump", f"cd /home/yugabyte && postgres/bin/ysql_dump -h {host} -U {user} -d {db} -f {f}")
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(f)
    rdb = f"{db}_restore"
    ctx.sql_step("create restore db", [f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{rdb}' AND pid <> pg_backend_pid()",
                                       f"DROP DATABASE IF EXISTS {rdb}", f"CREATE DATABASE {rdb}"])
    st = ctx.sh("ysqlsh -f (restore)", f"cd /home/yugabyte && bin/ysqlsh -h {host} -U {user} -d {rdb} -q -v ON_ERROR_STOP=0 -f {f}", tail=5)
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    ctx.sql_step("drop restore db", [f"DROP DATABASE {rdb}"])
    r.notes = "plain SQL dump (ysql_dump keeps YB-specific DDL such as SPLIT INTO); restored serially with ysqlsh"
    return r


def s_snapshot(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
    finally:
        c.close()
    t0 = time.perf_counter()
    st = _admin(ctx, "create_database_snapshot", f"create_database_snapshot ysql.{db}")
    m = re.search(r"snapshot creation: ([0-9a-f-]+)", st.out)
    if not m:
        raise RuntimeError(f"no snapshot id in: {st.out[-300:]}")
    sid = m.group(1)
    _wait_state(ctx, "list_snapshots", sid, "COMPLETE")
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.extra["snapshot_id"] = sid
    st = _admin(ctx, "export_snapshot", f"export_snapshot {sid} {ctx.backup_dir}/{db}.snapshot", check=False)
    r.extra["export_snapshot"] = st.out.strip()[-200:]
    r.backup_bytes = ctx.size(f"{ctx.backup_dir}/{db}.snapshot")
    r.extra["note_size"] = "the snapshot itself is copy-on-write tablet files on every node; export_snapshot writes only the metadata"
    # the disaster
    c = ctx.engine.connect(ctx.primary)
    try:
        c.execute("DELETE FROM backup_probe", rendered=True, fetch=False)
        n_after = ctx.probe_count(conn=c)
    finally:
        c.close()
    t0 = time.perf_counter()
    st = _admin(ctx, "restore_snapshot", f"restore_snapshot {sid}")
    m = re.search(r"Restoration id: ([0-9a-f-]+)", st.out)
    if m:
        _wait_state(ctx, "list_snapshot_restorations", m.group(1), "RESTORED")
    else:
        time.sleep(5)
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    n = ctx.probe_count()
    r.pitr = {"match": n == PROBE_ROWS, "probe_rows_after_delete": n_after, "probe_rows_after_restore": n, "probe_rows_expected": PROBE_ROWS}
    r.verify = ctx.verify(label="all tables after restore_snapshot")
    r.notes = "cluster-wide consistent snapshot (hybrid-time), restored in place; off-cluster copy = export_snapshot metadata + tablet snapshot files"
    _admin(ctx, "delete_snapshot", f"delete_snapshot {sid}", check=False)
    return r


def s_pitr(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    # a schedule left behind by an earlier run blocks a new one for the same namespace
    try:
        for sc in json.loads(_admin_quiet(ctx, "list_snapshot_schedules")).get("schedules", []):
            if sc.get("options", {}).get("filter") == f"ysql.{db}":
                _admin(ctx, "delete stale schedule", f"delete_snapshot_schedule {sc['id']}", check=False)
    except json.JSONDecodeError:
        pass
    st = _admin(ctx, "create_snapshot_schedule", f"create_snapshot_schedule 1 10 ysql.{db}")
    try:
        sched = json.loads(st.out.strip()).get("schedule_id")
    except json.JSONDecodeError:
        m = re.search(r'"schedule_id":\s*"([0-9a-f-]+)"', st.out)
        sched = m.group(1) if m else None
    if not sched:
        raise RuntimeError(f"no schedule id: {st.out[-200:]}")
    r.extra["schedule_id"] = sched
    # wait for the first scheduled snapshot (taken right after the schedule is created)
    t0 = time.perf_counter()
    first = None
    while time.perf_counter() - t0 < 180:
        out = _admin_quiet(ctx, f"list_snapshot_schedules {sched}")
        try:
            snaps = json.loads(out)["schedules"][0].get("snapshots", [])
        except (json.JSONDecodeError, KeyError, IndexError):
            snaps = []
        if snaps:
            first = snaps[0]
            break
        time.sleep(2)
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.extra["first_scheduled_snapshot"] = first
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
        time.sleep(1.5)
        ts = c.execute("SELECT to_char(now(), 'YYYY-MM-DD HH24:MI:SS.US')", rendered=True)[0][0]
        time.sleep(1.5)
        c.execute("DELETE FROM backup_probe", rendered=True, fetch=False)
        n_after = ctx.probe_count(conn=c)
    finally:
        c.close()
    t0 = time.perf_counter()
    st = _admin(ctx, "restore_snapshot_schedule", f'restore_snapshot_schedule {sched} "{ts}"')
    m = re.search(r'"restoration_id":\s*"([0-9a-f-]+)"', st.out)
    if m:
        _wait_state(ctx, "list_snapshot_restorations", m.group(1), "RESTORED")
    else:
        time.sleep(5)
    r.restore_seconds = round(time.perf_counter() - t0, 2)
    n = None
    for _ in range(30):
        try:
            n = ctx.probe_count()
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    r.pitr = {"match": n == PROBE_ROWS, "target": ts, "probe_rows_after_delete": n_after, "probe_rows_recovered": n, "probe_rows_expected": PROBE_ROWS}
    r.verify = ctx.verify(label="all tables after PITR")
    r.backup_bytes = 0
    _admin(ctx, "delete_snapshot_schedule", f"delete_snapshot_schedule {sched}", check=False)
    r.notes = "snapshot schedule (1 min interval, 10 min retention) keeps history; restore_snapshot_schedule rolls the database back to the timestamp in place"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("ysql_dump", "logical", "ysql_dump | ysqlsh", "logical dump restored into a second database", s_ysql_dump),
        Strategy("snapshot", "snapshot", "yb-admin create_database_snapshot / restore_snapshot", "distributed snapshot restored in place", s_snapshot),
        Strategy("pitr", "pitr", "yb-admin create_snapshot_schedule / restore_snapshot_schedule", "point-in-time restore from the snapshot schedule history", s_pitr),
    ]
