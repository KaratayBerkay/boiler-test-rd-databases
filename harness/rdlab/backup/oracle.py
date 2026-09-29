"""Oracle Database Free backup strategies.

  datapump   expdp SCHEMAS=lab (directory object on /backups) -> impdp REMAP_SCHEMA=lab:lab_restore, verified as lab_restore
  rman       RMAN is not shipped in the gvenzl *slim* image (only expdp/impdp/sqlplus): reported as n/a with the commands
             that the full image would run (BACKUP DATABASE PLUS ARCHIVELOG needs ARCHIVELOG mode)
  flashback  Flashback Query (AS OF TIMESTAMP), FLASHBACK TABLE ... TO TIMESTAMP (row movement) and FLASHBACK TABLE ...
             TO BEFORE DROP (recycle bin): undo-based point-in-time recovery without any backup
"""
from __future__ import annotations

import time

from .. import dockerctl
from ..config import StackConfig
from ..engines import Engine
from .common import PROBE_ROWS, Ctx, Strategy, StrategyResult


def _sysdba(ctx: Ctx, name: str, sql: str, *, pdb: bool = True) -> str:
    """Run SQL as SYSDBA (OS authentication inside the container), optionally inside the PDB."""
    pre = f"ALTER SESSION SET CONTAINER = {ctx.bconf.get('pdb', 'FREEPDB1')};\n" if pdb else ""
    script = f"""cat <<'SQL' | sqlplus -s / as sysdba
SET HEADING OFF FEEDBACK OFF PAGESIZE 0 LINESIZE 300 TRIMSPOOL ON
WHENEVER SQLERROR EXIT SQL.SQLCODE
{pre}{sql}
EXIT
SQL"""
    st = ctx.sh(name, script, shell="bash")
    return st.out


def s_datapump(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    user, pw, pdb = ctx.primary.user, ctx.primary.password, ctx.bconf.get("pdb", "FREEPDB1")
    conn = f"{ctx.bconf.get('admin_user', 'system')}/{ctx.bconf.get('admin_password', 'rootpass')}@localhost/{pdb}"
    ctx.sh("prepare", f"rm -f {ctx.backup_dir}/lab.dmp {ctx.backup_dir}/exp.log {ctx.backup_dir}/imp.log")
    _sysdba(ctx, "directory object", f"CREATE OR REPLACE DIRECTORY rdlab_bk AS '{ctx.backup_dir}';\nGRANT READ, WRITE ON DIRECTORY rdlab_bk TO {user};")
    st = ctx.sh("expdp SCHEMAS=lab", f"expdp {conn} SCHEMAS={user} DIRECTORY=rdlab_bk DUMPFILE=lab.dmp LOGFILE=exp.log REUSE_DUMPFILES=YES", tail=4, timeout=1800)
    r.backup_seconds = round(st.seconds, 2)
    r.backup_bytes = ctx.size(f"{ctx.backup_dir}/lab.dmp")
    rdb = f"{user}_restore"
    _sysdba(ctx, "create restore user", f"""BEGIN EXECUTE IMMEDIATE 'DROP USER {rdb} CASCADE'; EXCEPTION WHEN OTHERS THEN NULL; END;
/
CREATE USER {rdb} IDENTIFIED BY {pw} QUOTA UNLIMITED ON USERS;
GRANT CONNECT, RESOURCE, CREATE VIEW TO {rdb};""")
    st = ctx.sh("impdp REMAP_SCHEMA", f"impdp {conn} SCHEMAS={user} REMAP_SCHEMA={user}:{rdb} DIRECTORY=rdlab_bk DUMPFILE=lab.dmp LOGFILE=imp.log TABLE_EXISTS_ACTION=REPLACE", tail=4, timeout=1800, check=False)
    if st.rc not in (0, 5):          # 5 = completed with warnings (e.g. ORA-31684 already exists)
        raise RuntimeError(f"impdp failed rc={st.rc}: {st.out[-500:]}")
    r.restore_seconds = round(st.seconds, 2)
    r.verify = ctx.verify(ctx.alt_target(user=rdb, password=pw))
    _sysdba(ctx, "drop restore user", f"DROP USER {rdb} CASCADE;")
    r.notes = "Data Pump schema export through a directory object on the shared volume, imported into another schema (REMAP_SCHEMA)"
    return r


def s_rman(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    rc, out, _ = dockerctl.exec_in(ctx.container, ["bash", "-lc", "command -v rman && rman TARGET / <<< 'SHOW ALL;' | head -5"])
    if rc != 0 or not out.strip():
        r.status = "n/a"
        r.notes = ("RMAN binary is not part of gvenzl/oracle-free:*-slim*; with the full image: `ALTER DATABASE ARCHIVELOG` (mount), "
                   "`RMAN> CONFIGURE CONTROLFILE AUTOBACKUP ON; BACKUP AS COMPRESSED BACKUPSET DATABASE PLUS ARCHIVELOG;` and "
                   "`RECOVER DATABASE UNTIL TIME` for PITR")
        r.extra["log_mode"] = _sysdba(ctx, "log mode", "SELECT log_mode FROM v$database;", pdb=False).strip()
        return r
    r.status = "n/a"
    r.notes = "RMAN present but the lab does not restore a whole CDB in place; see the skill for the RMAN procedure"
    return r


def s_flashback(ctx: Ctx) -> StrategyResult:
    """SCN-based (timestamps map to SCNs with ~3 s granularity, which trips ORA-01466 right after a DDL)."""
    r = ctx.res
    _sysdba(ctx, "grant dbms_flashback", f"GRANT EXECUTE ON DBMS_FLASHBACK TO {ctx.primary.user};\nGRANT FLASHBACK ANY TABLE TO {ctx.primary.user};")
    c = ctx.engine.connect(ctx.primary)
    try:
        ctx.probe_create(c)
        c.execute("ALTER TABLE backup_probe ENABLE ROW MOVEMENT", rendered=True, fetch=False)
        time.sleep(6.0)     # ORA-01466 otherwise: the DDL time is compared with the flashback point at ~3 s SCN/time granularity
        scn = c.execute("SELECT DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER FROM dual", rendered=True)[0][0]
        time.sleep(1.0)
        c.execute("DELETE FROM backup_probe", rendered=True, fetch=False)
        n_after = ctx.probe_count(conn=c)
        stale = c.execute(f"SELECT COUNT(*) FROM backup_probe AS OF SCN {scn}", rendered=True)[0][0]
        ctx.log(f"    Flashback Query AS OF SCN {scn}: {stale} rows (current {n_after})")
        t0 = time.perf_counter()
        c.execute(f"FLASHBACK TABLE backup_probe TO SCN {scn}", rendered=True, fetch=False)
        fb_s = round(time.perf_counter() - t0, 3)
        n_fb = ctx.probe_count(conn=c)
        c.execute("DROP TABLE backup_probe", rendered=True, fetch=False)
        t0 = time.perf_counter()
        c.execute("FLASHBACK TABLE backup_probe TO BEFORE DROP", rendered=True, fetch=False)
        drop_s = round(time.perf_counter() - t0, 3)
        n_drop = ctx.probe_count(conn=c)
    finally:
        c.close()
    undo = _sysdba(ctx, "undo_retention", "SELECT value FROM v$parameter WHERE name = 'undo_retention';", pdb=False).strip()
    r.backup_seconds = 0.0
    r.backup_bytes = 0
    r.restore_seconds = fb_s
    r.pitr = {"match": n_fb == PROBE_ROWS and n_drop == PROBE_ROWS, "target": f"SCN {scn}", "probe_rows_after_delete": n_after, "stale_read_rows": int(stale),
              "probe_rows_after_flashback_table": n_fb, "probe_rows_after_flashback_before_drop": n_drop, "flashback_table_s": fb_s, "flashback_drop_s": drop_s,
              "undo_retention_s": undo}
    r.verify = ctx.verify(label="all tables (untouched)")
    r.notes = "undo-based: Flashback Query reads the old version, FLASHBACK TABLE TO SCN rewrites the current one, the recycle bin brings a dropped table back (within undo_retention)"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [
        Strategy("datapump", "logical", "expdp / impdp REMAP_SCHEMA", "Data Pump schema export imported into a second schema", s_datapump),
        Strategy("rman", "physical", "RMAN BACKUP DATABASE PLUS ARCHIVELOG", "physical backup with RMAN (not in the slim image)", s_rman),
        Strategy("flashback", "flashback", "AS OF TIMESTAMP / FLASHBACK TABLE / TO BEFORE DROP", "undo-based point-in-time recovery of a table", s_flashback),
    ]
