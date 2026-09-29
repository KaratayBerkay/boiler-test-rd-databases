"""Citus (coordinator + worker primaries) backup strategies.

  pg_dump        logical dump of the whole cluster through the coordinator (distributed tables are streamed from the
                 workers by COPY), restored into a second database on the coordinator as plain tables (backup/pg.py)
  cluster_pitr   cluster-consistent point-in-time recovery, restored end to end:
                   1. pg_basebackup of the coordinator and of every worker primary into /backups/<node>
                      (every node archives its WAL into /backups/wal/<node>/, see stacks/citus/compose.yaml)
                   2. a distributed probe table gets 1000 rows, then citus_create_restore_point('rdlab_citus_pitr')
                      writes the same named restore point on every node while 2PC commits are blocked
                   3. the disaster: DELETE FROM backup_probe through the coordinator (rows gone on every worker)
                   4. all nodes are restored in throw-away containers on an *isolated* docker network whose aliases are
                      the node names (coordinator, w1..w5): the restored coordinator's pg_dist_node therefore points at
                      the restored workers and never at the live ones, with no metadata edit
                   5. verified through the restored coordinator: fingerprint of every table + 1000 probe rows back,
                      per-shard counts on the restored workers, citus_check_connection_to_node for every worker
"""
from __future__ import annotations

import time
from typing import Any

from .. import dockerctl
from ..config import StackConfig, Target
from ..engines import Engine
from .common import PROBE_ROWS, PROBE_TABLE, Ctx, Strategy, StrategyResult
from .pg import PGDATA, _pg_user, s_citus_dump

RESTORE_POINT = "rdlab_citus_pitr"


def _nodes(ctx: Ctx) -> list[tuple[str, Target]]:
    """(citus node name, harness target) of every primary node, coordinator first — from pg_dist_node."""
    c = ctx.engine.connect(ctx.primary)
    try:
        rows = c.execute("SELECT nodename, groupid FROM pg_dist_node WHERE noderole = 'primary' AND isactive ORDER BY groupid", rendered=True)
    finally:
        c.close()
    out = []
    for name, group in rows:
        t = ctx.cfg.by_name(name) or (ctx.primary if group == 0 else None)
        if t is None or not t.container:
            raise RuntimeError(f"pg_dist_node lists {name} but lab.yaml has no target/container for it")
        out.append((name, t))
    return out


def _wait_archived_on(ctx: Ctx, target: Target, *, timeout: float = 60.0) -> dict[str, Any]:
    """Force the segment holding everything written so far to be archived on one node."""
    c = ctx.engine.connect(target)
    try:
        # a tiny record first, so pg_switch_wal() is never a no-op and `seg` is the segment that holds it
        c.execute("SELECT pg_create_restore_point('rdlab_wal_flush')", rendered=True)
        seg = c.execute("SELECT pg_walfile_name(pg_current_wal_insert_lsn())", rendered=True)[0][0]
        c.execute("SELECT pg_switch_wal()", rendered=True)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            last, archived, failed = c.execute("SELECT last_archived_wal, archived_count, failed_count FROM pg_stat_archiver", rendered=True)[0]
            if last and last >= seg:
                return {"segment": seg, "archived_after_s": round(time.perf_counter() - t0, 2), "archived_count": archived, "failed_count": failed}
            time.sleep(0.3)
        return {"segment": seg, "archived_after_s": None, "warning": "segment not archived in time"}
    finally:
        c.close()


def _scratch_node(ctx: Ctx, alias: str, *, port: int | None = None) -> str:
    """Start the restored copy of one node: base backup copied into PGDATA, recovery to the named restore point from
    the node's own WAL archive, then promote. Joins the isolated restore network under the node's name."""
    project = ctx.cfg.project
    image = ctx.bconf.get("image") or ctx.cfg.image
    net = ctx.bconf.get("restore_network", f"{project}-restore")
    vol = ctx.bconf.get("volume", "citus-backups")
    name = f"{project}-restore-{alias}"
    script = f"""set -e
mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA" && chmod 700 "$PGDATA"
gosu postgres bash -ec 'cp -a {ctx.backup_dir}/{alias}/. "$PGDATA"'
rm -f "$PGDATA/postmaster.pid"
cat >> "$PGDATA/postgresql.auto.conf" <<EOF
restore_command = 'cp {ctx.backup_dir}/wal/{alias}/%f %p'
recovery_target_name = '{RESTORE_POINT}'
recovery_target_action = 'promote'
recovery_target_inclusive = true
EOF
touch "$PGDATA/recovery.signal"
chown -R postgres:postgres "$PGDATA"
exec gosu postgres postgres -c archive_mode=off -c hot_standby=on -c logging_collector=off -c shared_preload_libraries=citus,pg_stat_statements -c max_connections=300 -c listen_addresses='*'
"""
    dockerctl.rm_container(name)
    cmd = ["docker", "run", "-d", "--name", name, "--network", net, "--network-alias", alias, "--label", "rdlab.scratch=1",
           "-v", f"{dockerctl.volume_name(project, vol)}:{ctx.backup_dir}", "-e", f"PGDATA={PGDATA}", "-e", "POSTGRES_PASSWORD=x"]
    if port:
        cmd += ["-p", f"{port}:5432"]
    cmd += [image, "bash", "-ec", script]
    dockerctl._run(cmd, timeout=120)
    return name


def _wait_promoted(ctx: Ctx, name: str, user: str, db: str, *, timeout: float = 300.0) -> float:
    """Wait until the restored node finished recovery (reached the restore point and promoted)."""
    t0 = time.perf_counter()
    last = ""
    while time.perf_counter() - t0 < timeout:
        if not dockerctl.container_running(name):
            raise RuntimeError(f"{name} exited: {dockerctl.container_logs(name, 40)[-1500:]}")
        rc, out, err = dockerctl.exec_in(name, ["psql", "-U", user, "-d", db, "-tAc", "SELECT pg_is_in_recovery()"], user="postgres", timeout=20)
        if rc == 0 and out.strip() == "f":
            return time.perf_counter() - t0
        last = (out + err).strip()[-160:]
        time.sleep(0.5)
    raise TimeoutError(f"{name} not promoted after {timeout}s: {last}\n{dockerctl.container_logs(name, 40)[-1500:]}")


def _network(ctx: Ctx, create: bool) -> str:
    net = ctx.bconf.get("restore_network", f"{ctx.cfg.project}-restore")
    dockerctl._run(["docker", "network", "rm", net], check=False, timeout=60)
    if create:
        dockerctl._run(["docker", "network", "create", "--label", "rdlab.scratch=1", net], timeout=60)
    return net


def s_cluster_pitr(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    u, dbu, db = _pg_user(ctx), ctx.primary.user, ctx.primary.database
    bdir = ctx.backup_dir
    nodes = _nodes(ctx)
    r.extra["nodes"] = [n for n, _ in nodes]
    # 1. physical base backup of every primary node
    bb: dict[str, Any] = {}
    for alias, t in nodes:
        ctx.sh(f"prepare {alias}", f"rm -rf {bdir}/{alias} && mkdir -p {bdir}/wal/{alias}", container=t.container, user=u)
        st = ctx.sh(f"pg_basebackup {alias}", f"pg_basebackup -U {dbu} -D {bdir}/{alias} -Fp -Xs --checkpoint=fast --manifest-checksums=CRC32C",
                    container=t.container, user=u)
        bb[alias] = {"seconds": round(st.seconds, 2), "bytes": ctx.size(f"{bdir}/{alias}", container=t.container, user=u)}
    r.extra["basebackups"] = bb
    r.backup_seconds = round(sum(v["seconds"] for v in bb.values()), 2)
    r.backup_bytes = sum(v["bytes"] or 0 for v in bb.values())
    # 2. changes after the backups: a distributed probe table, then the cluster-wide restore point
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c, rows=0)
        c.execute(f"SELECT create_distributed_table('{PROBE_TABLE.name}', 'id')", rendered=True)
        ctx.probe_insert(c, 1, PROBE_ROWS)
        shards = c.execute(f"SELECT cs.nodename, count(*) FROM citus_shards cs JOIN pg_dist_node n ON n.nodename = cs.nodename AND n.nodeport = cs.nodeport "
                           f"WHERE cs.table_name = '{PROBE_TABLE.name}'::regclass AND n.noderole = 'primary' GROUP BY 1 ORDER BY 1", rendered=True)
        r.extra["probe_shards_per_worker"] = {row[0]: row[1] for row in shards}
        r.extra["live_shard_placements"] = c.execute("SELECT count(*) FROM citus_shards", rendered=True)[0][0]
        t0 = time.perf_counter()
        lsn = c.execute(f"SELECT citus_create_restore_point('{RESTORE_POINT}')", rendered=True)[0][0]
        r.extra["restore_point"] = {"name": RESTORE_POINT, "coordinator_lsn": str(lsn), "seconds": round(time.perf_counter() - t0, 3)}
        ctx.log(f"    citus_create_restore_point         {RESTORE_POINT} @ {lsn} ({r.extra['restore_point']['seconds']}s)")
        # 3. the disaster
        c.execute(f"DELETE FROM {PROBE_TABLE.name}", rendered=True, fetch=False)
        after = ctx.probe_count(conn=c)
    finally:
        c.close()
    # 4. make sure every node's archive holds the restore point
    arch: dict[str, Any] = {}
    for alias, t in nodes:
        arch[alias] = _wait_archived_on(ctx, t)
    r.extra["wal_archive"] = arch
    r.extra["wal_archive_bytes"] = ctx.size(f"{bdir}/wal", user=u)
    # 5. restore the whole cluster on an isolated network
    port = int(ctx.bconf.get("restore_port", 15571))
    net = _network(ctx, create=True)
    names: list[str] = []
    try:
        t0 = time.perf_counter()
        for alias, _ in nodes:
            names.append(_scratch_node(ctx, alias, port=port if alias == nodes[0][0] else None))
        ready: dict[str, float] = {}
        for alias, name in zip((n for n, _ in nodes), names):
            ready[alias] = round(_wait_promoted(ctx, name, dbu, db), 2)
            ctx.log(f"    restored {alias:12s} promoted after {ready[alias]}s")
        r.restore_seconds = round(time.perf_counter() - t0, 2)
        r.extra["promoted_after_s"] = ready
        evidence: dict[str, list[str]] = {}
        for alias, name in zip((n for n, _ in nodes), names):
            lines = dockerctl.container_logs(name, 400).splitlines()
            evidence[alias] = [l.split("LOG:  ", 1)[-1][:160] for l in lines
                               if any(k in l for k in ("starting point-in-time recovery", "recovery stopping at restore point", "selected new timeline", "archive recovery complete"))]
        r.extra["restored_recovery_log"] = evidence
        stopped_at_point = all(any("recovery stopping at restore point" in l and RESTORE_POINT in l for l in v) for v in evidence.values())
        tgt = ctx.alt_target(port=port, host="127.0.0.1", name="restored-coordinator")
        # 6. verification through the restored coordinator
        rc = ctx.engine.connect(tgt)
        try:
            r.extra["restored_pg_dist_node"] = [list(map(str, row)) for row in rc.execute(
                "SELECT nodename, nodeport, noderole, isactive FROM pg_dist_node ORDER BY groupid, noderole", rendered=True)]
            r.extra["restored_worker_connectivity"] = {row[0]: row[1] for row in rc.execute(
                "SELECT nodename, citus_check_connection_to_node(nodename, nodeport) FROM pg_dist_node WHERE noderole = 'primary' AND groupid > 0 ORDER BY 1", rendered=True)}
            r.extra["restored_shard_count"] = rc.execute("SELECT count(*) FROM citus_shards", rendered=True)[0][0]
            per_shard = rc.execute(f"SELECT cs.nodename, sum(s.result::bigint) FROM run_command_on_shards('{PROBE_TABLE.name}', 'SELECT count(*) FROM %s') s "
                                   f"JOIN citus_shards cs ON cs.shardid = s.shardid JOIN pg_dist_node n ON n.nodename = cs.nodename AND n.nodeport = cs.nodeport "
                                   f"WHERE n.noderole = 'primary' GROUP BY 1 ORDER BY 1", rendered=True)
            r.extra["restored_probe_rows_per_worker"] = {row[0]: int(row[1]) for row in per_shard}
        finally:
            rc.close()
        r.verify = ctx.verify(tgt, label="restored cluster")
        n = ctx.probe_count(tgt)
        r.pitr = {"match": n == PROBE_ROWS and r.verify.get("match") is True, "target": f"recovery_target_name='{RESTORE_POINT}' on every node",
                  "probe_rows_at_restore_point": PROBE_ROWS, "probe_rows_on_live_cluster_after_delete": after, "probe_rows_recovered": n,
                  "workers_reachable": all(r.extra["restored_worker_connectivity"].values()),
                  "recovery_stopped_at_restore_point_on_all_nodes": stopped_at_point}
        r.pitr["match"] = r.pitr["match"] and stopped_at_point
        ctx.log(f"    pitr through restored coordinator: {n} probe rows ({'MATCH' if n == PROBE_ROWS else 'MISMATCH'}), "
                f"workers reachable={r.pitr['workers_reachable']}, recovery stopped at the restore point on all nodes={stopped_at_point}")
        r.extra["scratch_log_tail"] = dockerctl.container_logs(names[0], 10)[-600:]
    finally:
        for name in names:
            dockerctl.rm_container(name)
        _network(ctx, create=False)
    r.notes = (f"pg_basebackup of {len(nodes)} primaries + per-node WAL archives; citus_create_restore_point gives one consistent recovery "
               f"target; the {len(nodes)} nodes restored to it on an isolated network reusing the node names, so pg_dist_node needs no edit")
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("pg_dump", "logical", "pg_dump -Fd -j | pg_restore -j", "logical dump through the coordinator, restored into a second database", s_citus_dump),
        Strategy("cluster_pitr", "pitr", "pg_basebackup per node + citus_create_restore_point + recovery_target_name",
                 "cluster-consistent point-in-time recovery of coordinator and all workers, verified through the restored coordinator", s_cluster_pitr),
    ]
