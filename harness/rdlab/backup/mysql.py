"""MySQL / MariaDB / Percona XtraDB Cluster backup strategies.

MySQL 9 (official image: mysqlsh + mysqldump + clone plugin, no mysqlbinlog):
  mysqlsh_dump   MySQL Shell util.dumpTables/loadDump — parallel, zstd-compressed, the current recommended logical tool
  mysqldump      classic single-transaction dump, for comparison
  clone          CLONE LOCAL DATA DIRECTORY (physical hot copy through the clone plugin) started in a scratch container,
                 then point-in-time recovery by replicating from the source with START REPLICA UNTIL SQL_BEFORE_GTIDS
MariaDB 11:
  mariadb_dump   mariadb-dump --single-transaction (+ routines/triggers/events)
  mariadb_backup mariadb-backup full + incremental chain (--prepare / --incremental-dir), restored in a scratch container,
                 then PITR by replicating from the primary until a GTID (START SLAVE UNTIL master_gtid_pos)
  flashback      mariadb-binlog --flashback: undo the destructive DELETE in place from the ROW binlog
Percona XtraDB Cluster 8.4:
  mysqldump      logical
  xtrabackup     Percona XtraBackup 8.4 (bundled for SST) full + incremental, prepared and started with wsrep off
"""
from __future__ import annotations

import time

from .. import dockerctl
from ..config import StackConfig, Target
from ..engines import Engine
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult

XTRABACKUP = "/usr/bin/pxc_extra/pxb-8.4/bin/xtrabackup"


def _st(ctx: Ctx) -> dict:
    return ctx.__dict__.setdefault("_mystate", {})


def _root(ctx: Ctx) -> tuple[str, str]:
    p = ctx.primary
    return p.extra.get("admin_user", "root"), p.extra.get("admin_password", ctx.bconf.get("root_password", "rootpass"))


def _admin_target(ctx: Ctx, **kw) -> Target:
    u, pw = _root(ctx)
    return ctx.alt_target(user=u, password=pw, database="", **kw)


def _cli(ctx: Ctx, client: str = "mysql") -> str:
    u, pw = _root(ctx)
    return f"{client} -uroot -p{pw} --protocol=socket" if client in ("mysql", "mariadb") else client


def _scratch(ctx: Ctx, datadir: str, *, args: list[str], name_suffix: str = "restore", entrypoint: str | None = None,
             user: str | None = None, env: dict | None = None) -> tuple[str, Target]:
    project = ctx.cfg.project
    image = ctx.bconf.get("image") or ctx.cfg.image
    port = int(ctx.bconf.get("restore_port", 13309))
    name = f"{project}-{name_suffix}"
    vol = ctx.bconf.get("volume", "backups")
    dockerctl.scratch_run(name, image, project=project, volumes=[f"{dockerctl.volume_name(project, vol)}:{ctx.backup_dir}"],
                          ports=[f"{port}:3306"], env={"MYSQL_ROOT_PASSWORD": _root(ctx)[1], "MARIADB_ROOT_PASSWORD": _root(ctx)[1], **(env or {})},
                          command=[f"--datadir={datadir}", *args], entrypoint=entrypoint, user=user)
    u, pw = _root(ctx)
    return name, ctx.alt_target(port=port, host="127.0.0.1", user=u, password=pw, name=name_suffix)


def _wait_ready(ctx: Ctx, name: str, target: Target, *, timeout: float = 300.0) -> float:
    t0 = time.perf_counter()
    last = None
    while time.perf_counter() - t0 < timeout:
        if not dockerctl.container_running(name):
            raise RuntimeError(f"scratch container exited: {dockerctl.container_logs(name, 30)[-1500:]}")
        try:
            c = ctx.engine.connect(target, timeout=3)
            try:
                c.execute("SELECT 1", rendered=True)
                return time.perf_counter() - t0
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001
            last = str(e)[:120]
        time.sleep(0.5)
    raise TimeoutError(f"scratch instance not ready after {timeout}s: {last}\n{dockerctl.container_logs(name, 30)[-1500:]}")


def _dump_restore(ctx: Ctx, dump_cmd: str, dump_file: str, restore_cmd: str, *, label: str) -> None:
    r = ctx.res
    db = ctx.primary.database
    st = ctx.sh(label, dump_cmd)
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(dump_file)
    rdb = f"{db}_restore"
    ctx.sql_step("create restore db", [f"DROP DATABASE IF EXISTS {rdb}", f"CREATE DATABASE {rdb}"], target=_admin_target(ctx))
    st = ctx.sh("restore", restore_cmd)
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    ctx.sql_step("drop restore db", [f"DROP DATABASE {rdb}"], target=_admin_target(ctx))


# --------------------------------------------------------------------------------------------- MySQL
def s_mysqlsh_dump(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    u, pw = _root(ctx)
    db = ctx.primary.database
    d = f"{ctx.backup_dir}/shell"
    ctx.sh("prepare", f"rm -rf {d}")
    st = ctx.sh("mysqlsh dump-tables", f"mysqlsh --uri {u}:{pw}@localhost:3306 -- util dump-tables {db} --all --outputUrl={d} --threads=4 --compression=zstd")
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(d)
    rdb = f"{db}_restore"
    ctx.sql_step("create restore db", [f"DROP DATABASE IF EXISTS {rdb}", f"CREATE DATABASE {rdb}"], target=_admin_target(ctx))
    st = ctx.sh("mysqlsh load-dump", f"mysqlsh --uri {u}:{pw}@localhost:3306 -- util load-dump {d} --schema={rdb} --threads=4 --resetProgress")
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(ctx.alt_target(database=rdb))
    ctx.sql_step("drop restore db", [f"DROP DATABASE {rdb}"], target=_admin_target(ctx))
    r.notes = "MySQL Shell dump utility: 4 threads, zstd chunks, loaded with LOAD DATA LOCAL INFILE into a second schema"
    return r


def s_mysqldump(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    u, pw = _root(ctx)
    db = ctx.primary.database
    f = f"{ctx.backup_dir}/{db}.sql"
    gtid = "--set-gtid-purged=OFF" if _st(ctx).get("flavor") != "pxc" else ""
    _dump_restore(ctx, f"mysqldump -u{u} -p{pw} --single-transaction --routines --triggers --events {gtid} {db} > {f}", f,
                  f"mysql -u{u} -p{pw} {db}_restore < {f}", label="mysqldump")
    r.notes = "single-transaction consistent snapshot, plain SQL restored serially with the mysql client"
    return r


def s_clone(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    u, pw = _root(ctx)
    adm = _admin_target(ctx)
    clone_dir = f"{ctx.backup_dir}/clone"
    ctx.sh("prepare", f"rm -rf {clone_dir}")
    ctx.sql_step("install clone plugin", ["INSTALL PLUGIN clone SONAME 'mysql_clone.so'"], target=adm) if not _plugin_active(ctx, adm, "clone") else None
    t0 = time.perf_counter()
    ctx.sql_step("CLONE LOCAL DATA DIRECTORY", [f"CLONE LOCAL DATA DIRECTORY = '{clone_dir}'"], target=adm)
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = ctx.size(clone_dir)
    rows = ctx.sql_step("clone status", ["SELECT state, error_no, gtid_executed FROM performance_schema.clone_status"], target=adm, fetch_last=True)
    r.extra["clone_status"] = str(rows[0])[:200] if rows else None
    # data written after the clone: probe rows, then the GTID before the "disaster"
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
        gtid_before = c.execute("SELECT @@global.gtid_executed", rendered=True)[0][0]
        c.execute("DELETE FROM backup_probe", rendered=True, fetch=False)
        gtid_after = c.execute("SELECT @@global.gtid_executed", rendered=True)[0][0]
    finally:
        c.close()
    bad = _next_gtid(gtid_before, gtid_after)
    r.extra["gtid_before_delete"] = gtid_before
    r.extra["bad_transaction_gtid"] = bad
    name, tgt = _scratch(ctx, clone_dir, args=["--server-id=99", "--skip-replica-start", "--gtid-mode=ON", "--enforce-gtid-consistency=ON", "--local-infile=ON"])
    try:
        t0 = time.perf_counter()
        _wait_ready(ctx, name, tgt)
        r.restore_seconds = round(time.perf_counter() - t0, 2)
        r.verify = ctx.verify(ctx.alt_target(port=tgt.port, host="127.0.0.1", user=u, password=pw))
        # PITR: replay the source's binlog up to (excluding) the bad transaction
        src = ctx.bconf.get("source_host", "source")
        repl_u, repl_pw = ctx.bconf.get("repl_user", "repl"), ctx.bconf.get("repl_password", "replpass")
        ctx.sql_step("START REPLICA UNTIL SQL_BEFORE_GTIDS", [
            f"CHANGE REPLICATION SOURCE TO SOURCE_HOST='{src}', SOURCE_PORT=3306, SOURCE_USER='{repl_u}', SOURCE_PASSWORD='{repl_pw}', SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1",
            f"START REPLICA UNTIL SQL_BEFORE_GTIDS='{bad}'"], target=tgt)
        t1 = time.perf_counter()
        deadline = t1 + 120
        status = None
        while time.perf_counter() < deadline:
            c = ctx.engine.connect(tgt)
            try:
                st = ctx.engine.replication_status(c).get("replica_status") or {}
            finally:
                c.close()
            status = st
            if st.get("Replica_SQL_Running") == "No" and not st.get("Last_SQL_Error"):
                break
            time.sleep(0.5)
        n = ctx.probe_count(ctx.alt_target(port=tgt.port, host="127.0.0.1", user=u, password=pw))
        stopped_clean = bool(status) and status.get("Replica_SQL_Running") == "No" and not status.get("Last_SQL_Error")
        r.pitr = {"match": n == PROBE_ROWS and stopped_clean, "method": f"START REPLICA UNTIL SQL_BEFORE_GTIDS='{bad}'", "replay_seconds": round(time.perf_counter() - t1, 2),
                  "stopped_before_target": stopped_clean, "probe_rows_recovered": n, "probe_rows_expected": PROBE_ROWS,
                  "replica_status": {k: str(v)[:80] for k, v in (status or {}).items()}}
        ctx.log(f"    pitr via replication until GTID: {n} probe rows ({'MATCH' if n == PROBE_ROWS else 'MISMATCH'})")
    finally:
        dockerctl.rm_container(name)
    r.notes = "clone plugin physical hot copy (no external tool), scratch instance replays the source binlog with GTID auto-positioning and stops before the bad transaction"
    return r


def _plugin_active(ctx: Ctx, adm: Target, name: str) -> bool:
    try:
        rows = ctx.engine.connect(adm).execute(f"SELECT plugin_status FROM information_schema.plugins WHERE plugin_name='{name}'", rendered=True)
        return bool(rows) and rows[0][0] == "ACTIVE"
    except Exception:  # noqa: BLE001
        return False


def _next_gtid(before: str, after: str) -> str:
    """The GTID(s) executed between two gtid_executed snapshots (the bad transaction)."""
    def parse(s: str) -> dict[str, int]:
        out = {}
        for part in s.replace("\n", "").split(","):
            part = part.strip()
            if not part:
                continue
            uuid, _, ranges = part.partition(":")
            last = ranges.split(":")[-1]
            hi = int(last.split("-")[-1])
            out[uuid] = hi
        return out
    b, a = parse(before), parse(after)
    for uuid, hi in a.items():
        if b.get(uuid, 0) < hi:
            return f"{uuid}:{b.get(uuid, 0) + 1}"
    raise RuntimeError(f"no new GTID between {before} and {after}")


# --------------------------------------------------------------------------------------------- MariaDB
def s_mariadb_dump(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    u, pw = _root(ctx)
    db = ctx.primary.database
    f = f"{ctx.backup_dir}/{db}.sql"
    _dump_restore(ctx, f"mariadb-dump -u{u} -p{pw} --single-transaction --routines --triggers --events --gtid {db} > {f}", f,
                  f"mariadb -u{u} -p{pw} {db}_restore < {f}", label="mariadb-dump")
    r.notes = "single-transaction snapshot with the GTID position recorded in the dump header"
    return r


def s_mariadb_backup(ctx: Ctx) -> StrategyResult:
    """full -> (probe rows 1..500) -> incremental -> (probe rows 501..1000, GTID noted) -> DELETE -> restore chain in a
    scratch container (expect 500 rows) -> replicate from the primary until the noted GTID (expect 1000, DELETE not applied)."""
    r = ctx.res
    u, pw = _root(ctx)
    full, inc = f"{ctx.backup_dir}/full", f"{ctx.backup_dir}/inc1"
    ctx.sh("prepare", f"rm -rf {full} {inc}")
    st = ctx.sh("mariadb-backup --backup (full)", f"mariadb-backup --backup --user={u} --password={pw} --target-dir={full}", tail=3)
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(full)
    ctx.sh("mariadb-backup --prepare (full)", f"mariadb-backup --prepare --target-dir={full}", tail=2)
    half = PROBE_ROWS // 2
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c, rows=half)                     # change set 1: captured by the incremental
    finally:
        c.close()
    st = ctx.sh("mariadb-backup --backup (incremental)", f"mariadb-backup --backup --user={u} --password={pw} --target-dir={inc} --incremental-basedir={full}", tail=3)
    r.extra["incremental"] = {"seconds": round(st.seconds, 2), "bytes": ctx.size(inc)}
    ctx.sh("mariadb-backup --prepare (apply incremental)", f"mariadb-backup --prepare --target-dir={full} --incremental-dir={inc}", tail=2)
    binfo = (dockerctl.read_file(ctx.container, f"{full}/mariadb_backup_binlog_info") or dockerctl.read_file(ctx.container, f"{full}/xtrabackup_binlog_info")).strip()
    r.extra["backup_binlog_info"] = binfo
    gtid_backup = binfo.split()[-1] if binfo else ""
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_insert(c, half + 1, PROBE_ROWS - half)   # change set 2: only in the binlog
        gtid_before = c.execute("SELECT @@gtid_binlog_pos", rendered=True)[0][0]
        c.execute("DELETE FROM backup_probe", rendered=True, fetch=False)   # the disaster
    finally:
        c.close()
    r.extra["gtid_backup"] = gtid_backup
    r.extra["gtid_before_delete"] = gtid_before
    name, tgt = _scratch(ctx, full, args=["--server-id=77", "--skip-slave-start", "--log-bin=binlog", "--gtid-strict-mode=OFF"])
    try:
        t0 = time.perf_counter()
        _wait_ready(ctx, name, tgt)
        r.restore_seconds = round(time.perf_counter() - t0, 2)
        rt = ctx.alt_target(port=tgt.port, host="127.0.0.1", user=u, password=pw)
        r.verify = ctx.verify(rt)
        n0 = ctx.probe_count(rt)
        r.extra["probe_rows_after_chain_restore"] = {"expected": half, "actual": n0}
        src = ctx.bconf.get("source_host", "primary")
        repl_u, repl_pw = ctx.bconf.get("repl_user", "repl"), ctx.bconf.get("repl_password", "replpass")
        ctx.sql_step("START SLAVE UNTIL master_gtid_pos", [
            f"SET GLOBAL gtid_slave_pos = '{gtid_backup}'",
            f"CHANGE MASTER TO master_host='{src}', master_port=3306, master_user='{repl_u}', master_password='{repl_pw}', master_use_gtid=slave_pos",
            f"START SLAVE UNTIL master_gtid_pos='{gtid_before}'"], target=tgt)
        t1 = time.perf_counter()
        status: dict = {}
        reached = False
        while time.perf_counter() - t1 < 120:
            c = ctx.engine.connect(tgt)
            try:
                status = ctx.engine.replication_status(c).get("replica_status") or {}
                pos = c.execute("SELECT @@gtid_slave_pos", rendered=True)[0][0]
            finally:
                c.close()
            if status.get("Last_SQL_Error"):
                break
            if status.get("Slave_SQL_Running") == "No" and pos == gtid_before:
                reached = True
                break
            time.sleep(0.3)
        n = ctx.probe_count(rt)
        r.pitr = {"match": reached and n == PROBE_ROWS and n0 == half, "method": f"START SLAVE UNTIL master_gtid_pos='{gtid_before}' from {gtid_backup}",
                  "replay_seconds": round(time.perf_counter() - t1, 2), "reached_target_gtid": reached, "gtid_slave_pos": pos,
                  "probe_rows_recovered": n, "probe_rows_expected": PROBE_ROWS, "slave_status": {k: str(v)[:80] for k, v in status.items()}}
        ctx.log(f"    pitr via replication until GTID: {n} probe rows, reached={reached} ({'MATCH' if r.pitr['match'] else 'MISMATCH'})")
    finally:
        dockerctl.rm_container(name)
    r.notes = "physical hot backup + incremental chain (--prepare, then --prepare --incremental-dir), started in a scratch container; PITR by GTID replication from the primary up to the GTID before the DELETE"
    return r


def s_flashback(ctx: Ctx) -> StrategyResult:
    """Undo the DELETE in place: mariadb-binlog --flashback turns the ROW events into their inverse."""
    r = ctx.res
    u, pw = _root(ctx)
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
        f1, p1 = c.execute("SHOW MASTER STATUS", rendered=True)[0][:2]
        c.execute("DELETE FROM backup_probe WHERE id > 500", rendered=True, fetch=False)
        f2, p2 = c.execute("SHOW MASTER STATUS", rendered=True)[0][:2]
        n_after = ctx.probe_count(conn=c)
    finally:
        c.close()
    if f1 != f2:
        r.status = "n/a"
        r.notes = "binlog rotated between the positions"
        return r
    undo = f"{ctx.backup_dir}/undo.sql"
    st = ctx.sh("mariadb-binlog --flashback", f"mariadb-binlog --flashback --start-position={p1} --stop-position={p2} /var/lib/mysql/{f1} > {undo}")
    r.backup_seconds = round(st.seconds, 3)
    r.backup_bytes = ctx.size(undo)
    t0 = time.perf_counter()
    ctx.sh("apply undo", f"mariadb -u{u} -p{pw} {ctx.primary.database} < {undo}")
    r.restore_seconds = round(time.perf_counter() - t0, 3)
    n = ctx.probe_count()
    r.pitr = {"match": n == PROBE_ROWS, "probe_rows_after_delete": n_after, "probe_rows_after_flashback": n, "probe_rows_expected": PROBE_ROWS,
              "binlog": f"{f1}:{p1}-{p2}"}
    r.verify = {"match": n == PROBE_ROWS, "tables": 1, "rows": n, "mismatch": [], "note": "probe table only (in-place undo, no restore)"}
    r.notes = "ROW-format binlog events of the bad transaction inverted with --flashback and replayed on the primary (no restore needed)"
    return r


# --------------------------------------------------------------------------------------------- PXC
def s_xtrabackup(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    u, pw = _root(ctx)
    full, inc = f"{ctx.backup_dir}/full", f"{ctx.backup_dir}/inc1"
    sock = ctx.bconf.get("socket", "/tmp/mysql.sock")
    ctx.sh("prepare", f"rm -rf {full} {inc}")
    st = ctx.sh("xtrabackup --backup (full)", f"{XTRABACKUP} --backup --user={u} --password={pw} --socket={sock} --target-dir={full}", tail=2)
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(full)
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
    finally:
        c.close()
    st = ctx.sh("xtrabackup --backup (incremental)", f"{XTRABACKUP} --backup --user={u} --password={pw} --socket={sock} --target-dir={inc} --incremental-basedir={full}", tail=2)
    r.extra["incremental"] = {"seconds": round(st.seconds, 2), "bytes": ctx.size(inc)}
    ctx.sh("xtrabackup --prepare --apply-log-only", f"{XTRABACKUP} --prepare --apply-log-only --target-dir={full}", tail=2)
    ctx.sh("xtrabackup --prepare (incremental)", f"{XTRABACKUP} --prepare --target-dir={full} --incremental-dir={inc}", tail=2)
    # docker exec runs as the image's mysql user (uid 1001) so the prepared directory already has the right owner
    name, tgt = _scratch(ctx, full, entrypoint="mysqld", user="1001",
                         args=["--wsrep-provider=none", "--pxc-encrypt-cluster-traffic=OFF", "--server-id=88", "--socket=/tmp/mysqld.sock",
                               "--pid-file=/tmp/mysqld.pid", "--log-error=/tmp/mysqld.err", "--skip-log-bin", "--skip-mysqlx"])
    try:
        t0 = time.perf_counter()
        _wait_ready(ctx, name, tgt)
        r.restore_seconds = round(time.perf_counter() - t0, 2)
        r.verify = ctx.verify(ctx.alt_target(port=tgt.port, host="127.0.0.1", user=u, password=pw))
        n = ctx.probe_count(ctx.alt_target(port=tgt.port, host="127.0.0.1", user=u, password=pw))
        r.pitr = {"match": n == PROBE_ROWS, "probe_rows_expected": PROBE_ROWS, "probe_rows_restored": n,
                  "note": "rows inserted after the full backup come from the incremental"}
    finally:
        dockerctl.rm_container(name)
    r.notes = "XtraBackup 8.4 full + incremental (--apply-log-only on the base, final prepare with --incremental-dir), started standalone (wsrep off) in a scratch container"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    flavor = (cfg.raw.get("backup", {}) or {}).get("flavor") or ("mariadb" if cfg.dialect == "mariadb" else ("pxc" if cfg.key == "pxc" else "mysql"))
    if flavor == "mariadb":
        return [
            Strategy("mariadb_dump", "logical", "mariadb-dump --single-transaction", "logical dump restored into a second schema", s_mariadb_dump),
            Strategy("mariadb_backup", "physical", "mariadb-backup full + incremental, --prepare", "physical hot backup chain restored in a scratch container, PITR by GTID replication", s_mariadb_backup),
            Strategy("flashback", "flashback", "mariadb-binlog --flashback", "in-place undo of a bad DELETE from the ROW binlog", s_flashback),
        ]
    if flavor == "pxc":
        return [
            Strategy("mysqldump", "logical", "mysqldump --single-transaction", "logical dump restored into a second schema", _with_flavor(s_mysqldump, "pxc")),
            Strategy("xtrabackup", "physical", "xtrabackup 8.4 full + incremental", "physical hot backup chain restored standalone in a scratch container", s_xtrabackup),
        ]
    return [
        Strategy("mysqlsh_dump", "logical", "mysqlsh util dump-tables / load-dump", "MySQL Shell parallel zstd dump loaded into a second schema", s_mysqlsh_dump),
        Strategy("mysqldump", "logical", "mysqldump --single-transaction", "classic logical dump restored into a second schema", s_mysqldump),
        Strategy("clone", "physical", "CLONE LOCAL DATA DIRECTORY + START REPLICA UNTIL SQL_BEFORE_GTIDS", "clone-plugin physical copy started in a scratch container, PITR by GTID replication", s_clone),
    ]


def _with_flavor(fn, flavor):
    def run(ctx: Ctx):
        _st(ctx)["flavor"] = flavor
        return fn(ctx)
    return run
