"""IBM Db2 backup strategies (db2 CLP inside the privileged container, run as the instance owner).

  offline_full   BACKUP DATABASE lab TO /backups COMPRESS (no connections), RESTORE ... INTO labr, verified as LABR
  online_incr    LOGARCHMETH1 DISK + TRACKMOD ON (needs one offline backup to leave "backup pending"), then
                 BACKUP ... ONLINE ... INCLUDE LOGS and BACKUP ... ONLINE INCREMENTAL; manual chain restore INTO labi
                 + ROLLFORWARD TO END OF BACKUP (rows inserted between full and incremental come from the increment)
  pitr           archived logs after the incremental (a DELETE), RESTORE chain INTO labp + ROLLFORWARD ... TO <timestamp>
                 USING LOCAL TIME AND COMPLETE OVERFLOW LOG PATH (archive directory) -> rows before the DELETE
"""
from __future__ import annotations

import re
import time

from ..config import StackConfig
from ..engines import Engine
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult


def _st(ctx: Ctx) -> dict:
    return ctx.__dict__.setdefault("_db2state", {})


def _inst(ctx: Ctx) -> str:
    return ctx.bconf.get("instance", "db2inst1")


def _db2(ctx: Ctx, name: str, cmds: str, *, timeout: int = 3600, check: bool = True, tail: int | None = 6):
    """Run db2 CLP commands as the instance owner (each line one command, `db2 -v` echoes them)."""
    # CLP return codes: 0 ok, 1 no rows, 2 warning (e.g. SQL1495W on deactivate), 4 error, 8 system error
    script = "\n".join(f"db2 -v {c!r}; __rc=$?; [ $__rc -le 2 ] || exit $__rc" if not c.startswith("!") else c[1:] for c in cmds.strip().splitlines())
    return ctx.sh(name, f"su - {_inst(ctx)} -c {_q(script)}", timeout=timeout, check=check, tail=tail, shell="bash")


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _timestamp(out: str) -> str | None:
    m = re.search(r"timestamp for this backup image is\s*:\s*(\d{14})", out)
    return m.group(1) if m else None


def _restore_target(ctx: Ctx, name: str):
    return ctx.alt_target(database=name)


def _archive_dir(ctx: Ctx) -> str:
    """The directory LOGARCHMETH1 writes S*.LOG files into (…/<instance>/<DB>/NODE0000/LOGSTREAM0000/C0000000)."""
    from .. import dockerctl
    rc, out, _ = dockerctl.exec_in(ctx.container, ["bash", "-c", f"find {ctx.backup_dir}/logs -name 'S*.LOG' -printf '%h\\n' | sort -u | head -1"])
    return out.strip() or f"{ctx.backup_dir}/logs"


def s_offline_full(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    ctx.sh("prepare", f"rm -rf {ctx.backup_dir}/*.001 {ctx.backup_dir}/logs {ctx.backup_dir}/lt && mkdir -p {ctx.backup_dir}/logs {ctx.backup_dir}/lt && chmod 777 {ctx.backup_dir}/logs {ctx.backup_dir}/lt")
    # the compose healthcheck opens a CLP connection every 15 s: force + backup is retried until the backup wins the race
    st = _db2(ctx, "BACKUP DATABASE (offline)", f"""
!for i in 1 2 3 4 5 6; do db2 force applications all >/dev/null; sleep 1; db2 deactivate db {db} >/dev/null; if db2 -v 'backup db {db} to {ctx.backup_dir} compress without prompting'; then break; fi; sleep 2; done
""")
    ts = _timestamp(st.out)
    r.backup_seconds = round(st.seconds, 2)
    r.extra["timestamp"] = ts
    r.backup_bytes = ctx.size(f"{ctx.backup_dir}")
    _db2(ctx, "drop stale labr", "!db2 drop db labr >/dev/null 2>&1; true", check=False)
    st = _db2(ctx, "RESTORE DATABASE ... INTO labr", f"""
restore db {db} from {ctx.backup_dir} taken at {ts} into labr replace existing without prompting
!db2 rollforward db labr complete >/dev/null 2>&1; true
""")
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(_restore_target(ctx, "labr"))
    _db2(ctx, "drop labr", "drop db labr", check=False)
    _st(ctx)["offline_ts"] = ts
    r.notes = "offline (no connections) compressed full image; restored into a new database on the same instance (rollforward complete only needed once the source uses archive logging)"
    return r


def s_online_incremental(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    # switch to archive logging (leaves the database in backup-pending state until an offline backup is taken)
    st = _db2(ctx, "LOGARCHMETH1 + TRACKMOD, offline backup", f"""
update db cfg for {db} using LOGARCHMETH1 DISK:{ctx.backup_dir}/logs/ TRACKMOD ON
!for i in 1 2 3 4 5 6; do db2 force applications all >/dev/null; sleep 1; db2 deactivate db {db} >/dev/null; if db2 -v 'backup db {db} to {ctx.backup_dir} compress without prompting'; then break; fi; sleep 2; done
""")
    r.extra["archive_base_timestamp"] = _timestamp(st.out)
    st = _db2(ctx, "BACKUP DATABASE ONLINE INCLUDE LOGS", f"""
backup db {db} online to {ctx.backup_dir} compress include logs without prompting
""")
    full_ts = _timestamp(st.out)
    r.backup_seconds = round(st.seconds, 2)
    r.extra["online_full_timestamp"] = full_ts
    before = ctx.size(ctx.backup_dir) or 0
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
    finally:
        c.close()
    st = _db2(ctx, "BACKUP DATABASE ONLINE INCREMENTAL", f"""
backup db {db} online incremental to {ctx.backup_dir} compress include logs without prompting
""")
    inc_ts = _timestamp(st.out)
    r.extra["incremental"] = {"timestamp": inc_ts, "seconds": round(st.seconds, 2), "bytes": max(0, (ctx.size(ctx.backup_dir) or 0) - before)}
    r.backup_bytes = before
    ctx.sh("logtarget dir", f"rm -rf {ctx.backup_dir}/lt && mkdir -p {ctx.backup_dir}/lt && chmod 777 {ctx.backup_dir}/lt")
    _db2(ctx, "ARCHIVE LOG (so the archive holds every log the rollforward needs)", f"archive log for db {db}")
    logdir = _archive_dir(ctx)
    _db2(ctx, "drop stale labi", "!db2 drop db labi >/dev/null 2>&1; true", check=False)
    st = _db2(ctx, "RESTORE INCREMENTAL chain INTO labi + ROLLFORWARD", f"""
restore db {db} incremental from {ctx.backup_dir} taken at {inc_ts} into labi logtarget {ctx.backup_dir}/lt replace existing without prompting
restore db {db} incremental from {ctx.backup_dir} taken at {full_ts} into labi without prompting
restore db {db} incremental from {ctx.backup_dir} taken at {inc_ts} into labi without prompting
rollforward db labi to end of backup and complete overflow log path ({logdir})
""")
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(_restore_target(ctx, "labi"))
    n = ctx.probe_count(_restore_target(ctx, "labi"))
    r.pitr = {"match": n == PROBE_ROWS, "probe_rows_expected": PROBE_ROWS, "probe_rows_restored": n, "note": "table created after the online full comes from the incremental image"}
    _db2(ctx, "drop labi", "drop db labi", check=False)
    _st(ctx)["inc_ts"] = inc_ts
    _st(ctx)["full_ts"] = full_ts
    r.notes = "archive logging (LOGARCHMETH1 DISK) enables online backups; TRACKMOD ON makes incremental images possible; manual chain restore: target image, base full, target image"
    return r


def s_pitr(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    db = ctx.primary.database
    inc_ts, full_ts = _st(ctx).get("inc_ts"), _st(ctx).get("full_ts")
    if not inc_ts or not full_ts:
        r.status = "n/a"
        r.notes = "needs the online incremental chain"
        return r
    c = ctx.engine.connect(ctx.primary)
    try:
        time.sleep(1.5)
        ts = c.execute("VALUES CURRENT TIMESTAMP", rendered=True)[0][0]
        ts_s = ts.strftime("%Y-%m-%d-%H.%M.%S") if hasattr(ts, "strftime") else str(ts)
        time.sleep(1.5)
        c.execute("DELETE FROM backup_probe", rendered=True, fetch=False)
        n_after = ctx.probe_count(conn=c)
    finally:
        c.close()
    st = _db2(ctx, "ARCHIVE LOG", f"archive log for db {db}")
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(f"{ctx.backup_dir}/logs")
    logdir = _archive_dir(ctx)
    r.extra["archive_dir"] = logdir
    ctx.sh("logtarget dir", f"rm -rf {ctx.backup_dir}/lt && mkdir -p {ctx.backup_dir}/lt && chmod 777 {ctx.backup_dir}/lt")
    _db2(ctx, "drop stale labp", "!db2 drop db labp >/dev/null 2>&1; true", check=False)
    st = _db2(ctx, "RESTORE chain INTO labp + ROLLFORWARD TO timestamp", f"""
restore db {db} incremental from {ctx.backup_dir} taken at {inc_ts} into labp logtarget {ctx.backup_dir}/lt replace existing without prompting
restore db {db} incremental from {ctx.backup_dir} taken at {full_ts} into labp without prompting
restore db {db} incremental from {ctx.backup_dir} taken at {inc_ts} into labp without prompting
!cp -n {ctx.backup_dir}/lt/*.LOG {logdir}/ 2>/dev/null; true
rollforward db labp to {ts_s} using local time and complete overflow log path ({logdir})
""")
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(_restore_target(ctx, "labp"))
    n = ctx.probe_count(_restore_target(ctx, "labp"))
    r.pitr = {"match": n == PROBE_ROWS, "target": f"ROLLFORWARD TO {ts_s} USING LOCAL TIME", "probe_rows_after_delete": n_after, "probe_rows_recovered": n, "probe_rows_expected": PROBE_ROWS}
    _db2(ctx, "drop labp", "drop db labp", check=False)
    r.notes = "archived logs (ARCHIVE LOG forces the switch) replayed on the restored chain up to the timestamp before the DELETE"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("offline_full", "physical", "BACKUP DATABASE ... COMPRESS (offline) / RESTORE ... INTO", "offline full image restored into a new database", s_offline_full),
        Strategy("online_incremental", "incremental", "LOGARCHMETH1 + BACKUP ONLINE INCREMENTAL + RESTORE INCREMENTAL AUTOMATIC", "online full + incremental chain restored with rollforward", s_online_incremental),
        Strategy("pitr", "pitr", "ARCHIVE LOG + ROLLFORWARD ... TO <timestamp>", "point-in-time rollforward from archived logs", s_pitr),
    ]
