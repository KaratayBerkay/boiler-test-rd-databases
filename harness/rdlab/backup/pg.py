"""PostgreSQL (+ Citus) backup strategies.

  pg_dump        logical, directory format, parallel dump + parallel restore into a second database
  pg_basebackup  physical full copy, restored in a scratch container (crash recovery from streamed WAL)
  incremental    PostgreSQL 17+ block-level incremental (summarize_wal) merged with pg_combinebackup
  pitr           full backup + archived WAL replayed to a named restore point taken before a destructive DELETE

The primary is started with archive_mode=on, archive_command into the shared /backups volume and summarize_wal=on
(see stacks/postgres/compose.yaml). Restores run in a throw-away container on the stack network.
"""
from __future__ import annotations

import time

from .. import dockerctl
from ..config import StackConfig, Target
from ..engines import Engine
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult

PGDATA = "/var/lib/postgresql/18/docker"


def _st(ctx: Ctx) -> dict:
    return ctx.__dict__.setdefault("_pgstate", {})


def _pg_user(ctx: Ctx) -> str:
    return ctx.bconf.get("os_user", "postgres")


def _db_user(ctx: Ctx) -> str:
    return ctx.primary.user


def _scratch(ctx: Ctx, prepare: str, *, recovery: str = "", name_suffix: str = "restore") -> tuple[str, Target]:
    """Start a scratch PostgreSQL container whose data directory is produced by `prepare` (runs as postgres)."""
    project = ctx.cfg.project
    image = ctx.bconf.get("image") or ctx.cfg.image
    port = int(ctx.bconf.get("restore_port", 15439))
    name = f"{project}-{name_suffix}"
    vol = ctx.bconf.get("volume", "pg-backups")
    script = f"""set -e
mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA" && chmod 700 "$PGDATA"
gosu postgres bash -ec '{prepare}'
rm -f "$PGDATA/postmaster.pid"
{recovery}
chown -R postgres:postgres "$PGDATA"
exec gosu postgres postgres -c archive_mode=off -c hot_standby=on -c logging_collector=off -c shared_preload_libraries={ctx.bconf.get('preload', 'pg_stat_statements')} -c max_connections={ctx.bconf.get("max_connections", 300)} -c listen_addresses='*'
"""
    dockerctl.scratch_run(name, image, project=project, volumes=[f"{dockerctl.volume_name(project, vol)}:/backups"],
                          ports=[f"{port}:5432"], env={"PGDATA": PGDATA, "POSTGRES_PASSWORD": "x"},
                          command=["bash", "-ec", script])
    return name, ctx.alt_target(port=port, host="127.0.0.1", name=name_suffix)


def _wait_ready(ctx: Ctx, name: str, target, *, timeout: float = 300.0, need_primary: bool = True) -> float:
    t0 = time.perf_counter()
    deadline = t0 + timeout
    last = None
    while time.perf_counter() < deadline:
        if not dockerctl.container_running(name):
            raise RuntimeError(f"scratch container exited: {dockerctl.container_logs(name, 40)[-1500:]}")
        try:
            c = ctx.engine.connect(target, timeout=3)
            try:
                rec = c.execute("SELECT pg_is_in_recovery()", rendered=True)[0][0]
                if not (need_primary and rec):
                    return time.perf_counter() - t0
                last = "still in recovery"
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001
            last = str(e)[:120]
        time.sleep(0.5)
    raise TimeoutError(f"scratch instance not ready after {timeout}s: {last}\n{dockerctl.container_logs(name, 40)[-1500:]}")


def _archived_wal(ctx: Ctx) -> dict:
    rows = ctx.engine.connect(ctx.primary).execute("SELECT archived_count, failed_count, last_archived_wal, last_failed_wal FROM pg_stat_archiver", rendered=True)
    r = rows[0]
    return {"archived_count": r[0], "failed_count": r[1], "last_archived_wal": r[2], "last_failed_wal": r[3]}


def _wait_archived(ctx: Ctx, timeout: float = 60.0) -> dict:
    """Switch WAL and wait until the switched segment is archived."""
    c = ctx.engine.connect(ctx.primary)
    try:
        seg = c.execute("SELECT pg_walfile_name(pg_switch_wal())", rendered=True)[0][0]
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            last = c.execute("SELECT last_archived_wal FROM pg_stat_archiver", rendered=True)[0][0]
            if last and last >= seg:
                return {"segment": seg, "archived_after_s": round(time.perf_counter() - t0, 2)}
            time.sleep(0.3)
        return {"segment": seg, "archived_after_s": None, "warning": "segment not archived in time"}
    finally:
        c.close()


# --------------------------------------------------------------------------------------------- strategies
def s_pg_dump(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    u, dbu, db = _pg_user(ctx), _db_user(ctx), ctx.primary.database
    jobs = int(ctx.bconf.get("jobs", 4))
    d = f"{ctx.backup_dir}/dump"
    ctx.sh("prepare", f"rm -rf {d} {ctx.backup_dir}/globals.sql", user=u)
    st = ctx.sh("pg_dump -Fd", f"pg_dump -U {dbu} -d {db} -Fd -j {jobs} --compress=zstd -f {d}", user=u)
    ctx.sh("pg_dumpall --globals-only", f"pg_dumpall -U {dbu} --globals-only -f {ctx.backup_dir}/globals.sql", user=u)
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(d, user=u)
    rdb = f"{db}_restore"
    ctx.sql_step("create restore db", [f"DROP DATABASE IF EXISTS {rdb}", f"CREATE DATABASE {rdb}"])
    st = ctx.sh("pg_restore -j", f"pg_restore -U {dbu} -d {rdb} -j {jobs} --no-owner {d}", user=u, check=False)
    if st.rc != 0 and "already exists" not in st.out and "errors ignored" not in st.out:
        raise RuntimeError(f"pg_restore failed: {st.out[-600:]}")
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    r.extra["restore_warnings"] = st.out[-300:] if st.rc != 0 else ""
    ctx.sql_step("drop restore db", [f"DROP DATABASE {rdb}"])
    r.notes = f"directory format, {jobs} parallel jobs, zstd; globals dumped separately with pg_dumpall"
    return r


def s_basebackup(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    u, dbu = _pg_user(ctx), _db_user(ctx)
    full = f"{ctx.backup_dir}/full"
    ctx.sh("prepare", f"rm -rf {full} {ctx.backup_dir}/incr1 && mkdir -p {ctx.backup_dir}/wal", user=u)
    st = ctx.sh("pg_basebackup", f"pg_basebackup -U {dbu} -D {full} -Fp -Xs --checkpoint=fast --manifest-checksums=CRC32C", user=u)
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(full, user=u)
    v = ctx.sh("pg_verifybackup", f"pg_verifybackup {full}", user=u, check=False)
    r.extra["pg_verifybackup"] = v.out.strip()[-200:]
    name, tgt = _scratch(ctx, f'cp -a {full}/. "$PGDATA"')
    try:
        t0 = time.perf_counter()
        ready = _wait_ready(ctx, name, tgt)
        r.restore_seconds = round(time.perf_counter() - t0, 2)
        r.extra["ready_after_s"] = round(ready, 2)
        r.verify = ctx.verify(tgt)
    finally:
        dockerctl.rm_container(name)
    _st(ctx)["full"] = full
    r.notes = "plain-format full copy with streamed WAL (-Xs), verified with pg_verifybackup; restored by copying into a scratch container"
    return r


def s_incremental(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    u, dbu = _pg_user(ctx), _db_user(ctx)
    full = _st(ctx).get("full")
    if not full:
        r.status = "n/a"
        r.notes = "needs the full pg_basebackup first"
        return r
    # changes after the full backup: the probe table + a restore point the PITR strategy will use
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
        lsn = c.execute("SELECT pg_create_restore_point('rdlab_pitr')", rendered=True)[0][0]
        _st(ctx)["restore_point_lsn"] = str(lsn)
        _st(ctx)["restore_point_time"] = c.execute("SELECT now()", rendered=True)[0][0]
    finally:
        c.close()
    ctx.res.extra["wal_switch"] = _wait_archived(ctx)
    incr = f"{ctx.backup_dir}/incr1"
    st = ctx.sh("pg_basebackup --incremental", f"pg_basebackup -U {dbu} -D {incr} -Fp -Xs --checkpoint=fast --incremental={full}/backup_manifest", user=u)
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(incr, user=u)
    r.extra["full_backup_bytes"] = ctx.size(full, user=u)
    r.extra["incremental_pct_of_full"] = round(100 * r.backup_bytes / r.extra["full_backup_bytes"], 2) if r.backup_bytes and r.extra["full_backup_bytes"] else None
    name, tgt = _scratch(ctx, f'pg_combinebackup {full} {incr} -o "$PGDATA"')
    try:
        t0 = time.perf_counter()
        _wait_ready(ctx, name, tgt)
        r.restore_seconds = round(time.perf_counter() - t0, 2)
        r.verify = ctx.verify(tgt)
        n = ctx.probe_count(tgt)
        r.pitr = {"match": n == PROBE_ROWS, "probe_rows_expected": PROBE_ROWS, "probe_rows_restored": n,
                  "note": "rows inserted after the full backup are present in the combined (full+incremental) restore"}
    finally:
        dockerctl.rm_container(name)
    r.notes = "block-level incremental (summarize_wal=on, PG17+), restored with pg_combinebackup full incr -o PGDATA"
    return r


def s_pitr(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    full = _st(ctx).get("full")
    if not full or "restore_point_lsn" not in _st(ctx):
        r.status = "n/a"
        r.notes = "needs the full backup and the restore point created by the incremental strategy"
        return r
    # the "disaster": everything in the probe table is deleted after the restore point
    c = ctx.engine.connect(ctx.primary)
    try:
        c.execute("DELETE FROM backup_probe", rendered=True, fetch=False)
        after = ctx.probe_count(conn=c)
    finally:
        c.close()
    r.extra["wal_switch"] = _wait_archived(ctx)
    r.extra["archiver"] = _archived_wal(ctx)
    r.backup_bytes = ctx.size(f"{ctx.backup_dir}/wal", user=_pg_user(ctx))
    r.backup_seconds = 0.0
    recovery = """cat >> "$PGDATA/postgresql.auto.conf" <<EOF
restore_command = 'cp /backups/wal/%f %p'
recovery_target_name = 'rdlab_pitr'
recovery_target_action = 'promote'
recovery_target_inclusive = true
EOF
touch "$PGDATA/recovery.signal"
"""
    name, tgt = _scratch(ctx, f'cp -a {full}/. "$PGDATA"', recovery=recovery, name_suffix="pitr")
    try:
        t0 = time.perf_counter()
        _wait_ready(ctx, name, tgt)
        r.restore_seconds = round(time.perf_counter() - t0, 2)
        r.verify = ctx.verify(tgt)
        n = ctx.probe_count(tgt)
        r.pitr = {"match": n == PROBE_ROWS, "target": "recovery_target_name='rdlab_pitr'", "probe_rows_at_restore_point": PROBE_ROWS,
                  "probe_rows_on_primary_after_delete": after, "probe_rows_recovered": n}
        r.extra["scratch_log_tail"] = dockerctl.container_logs(name, 12)[-800:]
    finally:
        dockerctl.rm_container(name)
    r.notes = "WAL archive (archive_command -> /backups/wal) replayed on top of the full backup up to the named restore point; the DELETE that followed is not replayed"
    return r


# --------------------------------------------------------------------------------------------- citus
def s_citus_dump(ctx: Ctx) -> StrategyResult:
    """pg_dump through the coordinator: data of distributed tables is pulled from the workers by COPY."""
    r = ctx.res
    u, dbu, db = _pg_user(ctx), _db_user(ctx), ctx.primary.database
    jobs = int(ctx.bconf.get("jobs", 2))
    d = f"{ctx.backup_dir}/dump"
    ctx.sh("prepare", f"rm -rf {d}", user=u)
    st = ctx.sh("pg_dump -Fd (coordinator)", f"pg_dump -U {dbu} -d {db} -Fd -j {jobs} --compress=zstd -f {d}", user=u)
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(d, user=u)
    rdb = f"{db}_restore"
    ctx.sql_step("create restore db", [f"DROP DATABASE IF EXISTS {rdb}", f"CREATE DATABASE {rdb}"])
    st = ctx.sh("pg_restore -j", f"pg_restore -U {dbu} -d {rdb} -j {jobs} --no-owner {d}", user=u, check=False)
    r.restore_seconds = round(st.seconds, 2)
    r.extra["restore_warnings"] = st.out[-400:] if st.rc != 0 else ""
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    ctx.sql_step("drop restore db", [f"DROP DATABASE {rdb}"])
    r.notes = ("logical backup of the whole cluster via the coordinator; the restore yields plain local tables "
               "(re-run create_distributed_table/create_reference_table to shard them again; Citus metadata is not in the dump)")
    return r


def s_citus_restore_point(ctx: Ctx) -> StrategyResult:
    """citus_create_restore_point(): one consistent named restore point on the coordinator and every worker,
    the anchor for a cluster-wide PITR (each node restored with recovery_target_name)."""
    r = ctx.res
    c = ctx.engine.connect(ctx.primary)
    try:
        t0 = time.perf_counter()
        lsn = c.execute("SELECT citus_create_restore_point('rdlab_citus_pitr')", rendered=True)[0][0]
        r.backup_seconds = round(time.perf_counter() - t0, 3)
        nodes = c.execute("SELECT nodename, nodeport, noderole, isactive FROM pg_dist_node ORDER BY nodeid", rendered=True)
        r.extra["coordinator_lsn"] = str(lsn)
        r.extra["nodes"] = [list(map(str, n)) for n in nodes]
    finally:
        c.close()
    # physical backup size of the coordinator and one worker (every node would be backed up the same way)
    u, dbu = _pg_user(ctx), _db_user(ctx)
    sizes = {}
    for tname in ["primary"] + [ctx.bconf.get("sample_worker", "w1")]:
        t = ctx.cfg.by_name(tname)
        if not t or not t.container:
            continue
        ctx.sh(f"prepare {tname}", f"rm -rf {ctx.backup_dir}/{tname}", container=t.container, user=u)
        st = ctx.sh(f"pg_basebackup {tname}", f"pg_basebackup -U {dbu} -D {ctx.backup_dir}/{tname} -Fp -Xs --checkpoint=fast", container=t.container, user=u)
        sizes[tname] = {"seconds": round(st.seconds, 2), "bytes": ctx.size(f"{ctx.backup_dir}/{tname}", container=t.container, user=u),
                        "archiver": _archived_wal_on(ctx, t)}
    r.extra["node_basebackups"] = sizes
    r.backup_bytes = sum(v["bytes"] or 0 for v in sizes.values())
    r.verify = {"match": None, "note": "no restore drill: a consistent cluster restore needs every node restored to the same restore point"}
    r.notes = "consistent restore point across all nodes + per-node pg_basebackup with WAL archiving on each node (coordinator and w1 measured)"
    return r


def _archived_wal_on(ctx: Ctx, target) -> dict:
    try:
        c = ctx.engine.connect(target)
        try:
            row = c.execute("SELECT archived_count, failed_count, last_archived_wal FROM pg_stat_archiver", rendered=True)[0]
            return {"archived_count": row[0], "failed_count": row[1], "last_archived_wal": row[2]}
        finally:
            c.close()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    if cfg.features.get("citus"):
        from .citus import strategies as citus_strategies   # pg_dump via the coordinator + cluster-wide PITR drill
        return citus_strategies(engine, cfg)
    return [
        Strategy("pg_dump", "logical", "pg_dump -Fd -j4 --compress=zstd | pg_restore -j4", "parallel directory-format dump restored into a second database", s_pg_dump),
        Strategy("pg_basebackup", "physical", "pg_basebackup -Fp -Xs", "full physical copy restored in a scratch container", s_basebackup),
        Strategy("incremental", "incremental", "pg_basebackup --incremental + pg_combinebackup", "PG17+ block-level incremental on top of the full backup", s_incremental),
        Strategy("pitr", "pitr", "archive_command + restore_command + recovery_target_name", "WAL-archive point-in-time recovery to a restore point before a destructive DELETE", s_pitr),
    ]
