---
name: db-backup-recovery
description: Back up, restore and point-in-time-recover 20 SQL engines running in Docker Compose, with every command verified by an automated drill (backup -> restore elsewhere -> fingerprint every table -> undo a destructive DELETE). PostgreSQL pg_dump -Fd/pg_basebackup/PG17+ incremental (pg_combinebackup)/WAL-archive PITR, MySQL Shell dump + clone plugin + START REPLICA UNTIL SQL_BEFORE_GTIDS, MariaDB mariadb-backup chains + mariadb-binlog --flashback, XtraBackup on Galera, TiDB BR SQL BACKUP/RESTORE + FLASHBACK CLUSTER, CockroachDB BACKUP INTO nodelocal + AS OF SYSTEM TIME, YugabyteDB snapshot schedules (PITR), SQL Server full/differential/log + STOPAT, Oracle Data Pump + Flashback, Db2 online incremental + ROLLFORWARD, ClickHouse BACKUP TO Disk (incremental), CrateDB snapshots, QuestDB CHECKPOINT, MonetDB hot_snapshot, Firebird gbak/nbackup, H2 BACKUP TO, SQLite VACUUM INTO / backup API, DuckDB EXPORT DATABASE, Citus citus_create_restore_point. Use when asked for "backup", "restore", "PITR", "point-in-time recovery", "disaster recovery", "incremental backup", "flashback" or "RPO/RTO" for any of these engines.
---

# Backup and recovery

## The drill (implemented in `harness/rdlab/phases/backup.py` + `harness/rdlab/backup/<engine>.py`)
Every strategy below was executed by the harness, not just documented:
1. **Fingerprint** the source: `COUNT(*)` + a checksum of the first 20 000 rows (ordered by primary key) of all 7 benchmark tables.
2. **Back up** into the stack's shared `/backups` volume (a one-shot `volumes-init` job creates it with mode 1777 so any container uid can write); record wall time and bytes on disk (`du -sb`).
3. **Restore somewhere else**: a second database/schema on the same server (`lab_restore`), or a throw-away container on the stack network that mounts the same volume (PostgreSQL, MySQL, MariaDB, PXC, QuestDB), or in place after a `DROP TABLE` when the engine cannot rename on restore (TiDB, CrateDB, ClickHouse ON CLUSTER).
4. **Verify**: fingerprint of the restored copy must equal the source (`verify.match`).
5. **Point-in-time / incremental probe**: a `backup_probe` table (1000 rows) is created *after* the full backup; a DELETE (the "disaster") happens after the recovery point; the restored copy must show the 1000 rows (`pitr.match`).
6. Clean up (drop the copy, remove the scratch container). Results: `results/<engine>/latest.json -> phases.backup.strategies.<name>` and the "Backup & recovery drills" table in `results/SUMMARY.md`.

Run it: `uv run --project harness rdlab run <stack> --phases load,backup` or `rdlab phase <stack> backup` on a running stack. One strategy by hand: `uv run --project harness python skills/db-backup-recovery/scripts/backup_drill.py <stack> [strategy]`.

## Choosing a strategy (what the drills showed)
```
need the data on another major version / engine / to restore ONE table?  -> logical (pg_dump -Fd, mysqlsh dumpTables, mariadb-dump,
                                                                              expdp, msqldump, gbak, EXPORT DATABASE ...)
need a fast full restore of a big cluster?                                 -> physical (pg_basebackup, clone plugin, mariadb-backup,
                                                                              xtrabackup, BACKUP DATABASE, db2 backup, ClickHouse BACKUP)
need "the state at 14:25:00, before the bad UPDATE"?                       -> physical + continuous log: WAL archive (PG), binlog + GTID
                                                                              (MySQL/MariaDB), BACKUP LOG + STOPAT (SQL Server), archived logs
                                                                              + ROLLFORWARD (Db2), revision_history (CockroachDB), snapshot
                                                                              schedule (YugabyteDB); MVCC/undo flashback for a short window
                                                                              (TiDB, Oracle, CockroachDB AS OF SYSTEM TIME)
backup window too small for a full copy every night?                       -> block/page level increments: PG17+ pg_basebackup --incremental,
                                                                              mariadb-backup/xtrabackup --incremental-basedir, SQL Server
                                                                              DIFFERENTIAL, Db2 INCREMENTAL, nbackup levels, ClickHouse
                                                                              base_backup, BR LAST_BACKUP, CockroachDB INTO LATEST IN
```
Rules that held on every engine: **the backup that was never restored is not a backup** (three of the twenty engines needed a fix before their documented restore path actually worked - see findings); keep the archive/binlog retention longer than the backup interval (MySQL `binlog-expire-logs-seconds`, PG `wal_summary_keep_time`, Db2 `LOGARCHMETH1`, YugabyteDB schedule retention); always back up cluster-wide *consistently* (Citus `citus_create_restore_point`, ClickHouse `ON CLUSTER`, CockroachDB/TiDB/YugabyteDB do it natively).

## Per-engine cheat sheet (all commands are the ones the harness runs; details and gotchas in `references/per-engine-commands.md`)
| engine | logical | physical / incremental | point in time | restore-elsewhere trick |
|---|---|---|---|---|
| PostgreSQL 18 | `pg_dump -Fd -j4 --compress=zstd`, `pg_restore -j4`, `pg_dumpall --globals-only` | `pg_basebackup -Fp -Xs --checkpoint=fast` + `pg_verifybackup`; `pg_basebackup --incremental=full/backup_manifest` + `pg_combinebackup full incr -o PGDATA` (needs `summarize_wal=on`) | `archive_mode=on`, `archive_command='test ! -f /backups/wal/%f && cp %p /backups/wal/%f'`; restore: `restore_command`, `recovery_target_name`, `touch recovery.signal` | scratch container must use `max_connections` >= primary's (hot_standby refuses otherwise) |
| Citus 13 | `pg_dump` via the coordinator (tables come back local; re-run `create_distributed_table`) | `pg_basebackup` on coordinator + every worker, WAL archive per node | `SELECT citus_create_restore_point('name')` = same restore point on all nodes; restore each node to it | restored nodes on an isolated network with the same hostnames so `pg_dist_node` needs no edit |
| MySQL 9.7 | `mysqlsh -- util dump-tables lab --all --threads=4 --compression=zstd` / `util load-dump --schema=lab_restore` (LOAD DATA LOCAL); `mysqldump --single-transaction --set-gtid-purged=OFF` | `INSTALL PLUGIN clone SONAME 'mysql_clone.so'; CLONE LOCAL DATA DIRECTORY = '/backups/clone'` -> start a scratch `mysqld --datadir=/backups/clone` | scratch replica: `CHANGE REPLICATION SOURCE TO ... SOURCE_AUTO_POSITION=1; START REPLICA UNTIL SQL_BEFORE_GTIDS='<uuid>:<n>'` (the official image has no `mysqlbinlog`) | keep `binlog-expire-logs-seconds` >= your RPO window |
| MariaDB 11.8 | `mariadb-dump --single-transaction --routines --triggers --events --gtid` | `mariadb-backup --backup --target-dir=full`, `--prepare`, incremental `--incremental-basedir=full`, apply with `--prepare --target-dir=full --incremental-dir=inc1` | `SET GLOBAL gtid_slave_pos='<mariadb_backup_binlog_info gtid>'; CHANGE MASTER TO ... master_use_gtid=slave_pos; START SLAVE UNTIL master_gtid_pos='<before the DELETE>'`; in-place undo: `mariadb-binlog --flashback --start-position --stop-position binlog.N \| mariadb lab` | binlog info file is `mariadb_backup_binlog_info` in 11.x |
| Percona XtraDB Cluster 8.4 | `mysqldump --single-transaction` | `/usr/bin/pxc_extra/pxb-8.4/bin/xtrabackup --backup --socket=/tmp/mysql.sock`, incremental `--incremental-basedir`, `--prepare --apply-log-only` on the base, final `--prepare --incremental-dir` | (binlog PITR as MySQL) | start the prepared dir with `mysqld --wsrep-provider=none` (uid 1001) |
| TiDB 8.5 | – | `BACKUP DATABASE lab TO 'local:///backups/full'` (BR inside tidb-server; every TiKV writes to the shared volume), `BACKUP ... LAST_BACKUP = <BackupTS>` | `SELECT ... AS OF TIMESTAMP`, `FLASHBACK TABLE t` (after DROP), `FLASHBACK CLUSTER TO TIMESTAMP '...'` (in place, blocks the cluster, within `tidb_gc_life_time`) | BR cannot rename: `DROP TABLE` + `RESTORE TABLE lab.t FROM ...` |
| CockroachDB 26.2 | – | `BACKUP DATABASE lab INTO 'nodelocal://1/lab' WITH revision_history`, incremental `INTO LATEST IN` (free in self-hosted since 24.3) | `RESTORE DATABASE lab FROM LATEST IN '...' AS OF SYSTEM TIME '<hlc>' WITH new_db_name = 'lab_pitr'` | `SHOW BACKUP LATEST IN`, `SHOW JOBS` |
| YugabyteDB 2026.1 | `ysql_dump` / `ysqlsh -f` | `yb-admin create_database_snapshot ysql.lab` (+ `export_snapshot` metadata), `restore_snapshot <id>` (in place) | `yb-admin create_snapshot_schedule 1 10 ysql.lab`, `restore_snapshot_schedule <id> "<timestamp>"` (in place) | yb-admin prints JSON: parse `restorations[].state == RESTORED` |
| SQL Server 2025 | – | `BACKUP DATABASE lab TO DISK=... WITH INIT, COMPRESSION, CHECKSUM`, `WITH DIFFERENTIAL`, `RESTORE VERIFYONLY`, `RESTORE DATABASE lab_restore ... WITH MOVE ..., NORECOVERY` | `BACKUP LOG` (FULL recovery), `RESTORE LOG ... WITH STOPAT = '<time>', RECOVERY` | logical file names from `RESTORE FILELISTONLY`; backups work on the AG primary |
| Oracle 26ai Free (23.26) | `expdp system/...@FREEPDB1 SCHEMAS=lab DIRECTORY=rdlab_bk`, `impdp ... REMAP_SCHEMA=lab:lab_restore` | RMAN (**not in the slim image**; full image: `BACKUP DATABASE PLUS ARCHIVELOG`) | Flashback Query `AS OF SCN`, `FLASHBACK TABLE t TO SCN n` (`ENABLE ROW MOVEMENT`), `FLASHBACK TABLE t TO BEFORE DROP` | wait > 5 s after DDL before flashback (SCN/time mapping granularity: ORA-01466) |
| Db2 12.1 | – | `BACKUP DB lab TO /backups COMPRESS` (offline), `UPDATE DB CFG ... LOGARCHMETH1 DISK:/backups/logs/ TRACKMOD ON`, `BACKUP DB lab ONLINE ... INCLUDE LOGS`, `... ONLINE INCREMENTAL`; restore chain: target image, base, target image `INTO labi`, `ROLLFORWARD DB labi TO END OF BACKUP` | `ARCHIVE LOG FOR DB lab`; `ROLLFORWARD DB labp TO <ts> USING LOCAL TIME AND COMPLETE OVERFLOW LOG PATH (<archive dir>)` | the compose healthcheck holds a CLP connection: retry `force applications all` + offline backup |
| ClickHouse 26.8 | – | `BACKUP DATABASE lab TO Disk('backups', 'full')` (config: `storage_configuration.disks.backups` + `backups.allowed_disk`), `ON CLUSTER`, incremental `SETTINGS base_backup = Disk(...)` | none (immutable parts) | `RESTORE DATABASE lab AS lab_restore` only works when the Keeper path uses `{uuid}` |
| CrateDB 6.4 | – | `CREATE REPOSITORY r TYPE fs WITH (location='/backups/crate')` (`path.repo`), `CREATE SNAPSHOT r.s ALL`, incremental by construction | – | `RESTORE SNAPSHOT r.s TABLE ... WITH (schema_rename_pattern='doc', schema_rename_replacement='restored')` |
| QuestDB 10 | – | `CHECKPOINT CREATE` -> copy `db/ conf/ .checkpoint/` -> `CHECKPOINT RELEASE` | – | new root + `touch _restore` before start (no tar in the image: `cp -a`) |
| MonetDB Dec2025 | `msqldump -d lab` -> `monetdb create/release lab_restore` -> `mclient -d lab_restore < dump` | `CALL sys.hot_snapshot('/backups/lab.tar')` -> untar into the dbfarm under a new name | – | a new db starts with password monetdb/monetdb: `ALTER USER SET PASSWORD` first |
| Firebird 5 | `gbak -b -g` / `gbak -c` | `nbackup -B 0` (level 0) + `-B 1` (increment) -> `nbackup -R new.fdb l0 l1` | – | run nbackup as the user that owns the .fdb (root in the official image) |
| H2 2.3 | `SCRIPT TO 'lab.sql'` / `RUNSCRIPT FROM` in a new db | `BACKUP TO 'lab.zip'` -> `java -cp h2.jar org.h2.tools.Restore -file lab.zip -dir /data -db lab_restore` | – | `COMPRESSION GZIP` not accepted through the PG-protocol server |
| SQLite 3.45 | `VACUUM INTO 'copy.db'` | `sqlite3.Connection.backup()` (page copy, online) | Litestream/LiteFS (WAL streaming) | – |
| DuckDB 1.5 | `EXPORT DATABASE 'dir' (FORMAT parquet, COMPRESSION zstd)` / `ATTACH + IMPORT DATABASE` | `COPY FROM DATABASE lab TO cp (SCHEMA)` + `INSERT ... SELECT` per table in FK order | – | plain `COPY FROM DATABASE` copies alphabetically and violates foreign keys |

## On Kubernetes (k3s runtime, `docs/kubernetes-backup-logging.md`)
- Scheduled backups = a CronJob per stack running the engine's client image against the primary's Service, writing into the same backup PVC
  (`k8s/gen-backup-addons.py`, `k8s/scenarios-backup.sh schedule|backup-now|artifacts`); engines whose backup only runs inside the server
  (Db2 `db2 backup`, QuestDB CHECKPOINT + copy) use `kubectl exec`.
- The same drills run on the cluster with `RDLAB_PLATFORM=k3s`: throw-away restore containers become Pods mounting the backup PVC with a
  NodePort on the reserved `restore_port` (`harness/rdlab/k8s_scratch.py`); `kubectl exec` has no `-u`, so OS-user steps are wrapped in gosu/su.
- RWO backup PVCs are fine on one node; multi-node clusters want RWX storage or a bucket target (WAL-G/pgBackRest, BR `s3://`, `nodelocal` -> `s3://`).

## Measured (Sept 2026, one 28-core host, scale 1 unless noted; full numbers in `results/SUMMARY.md`)
See the "Backup & recovery drills" table: backup seconds, bytes on disk, restore seconds, verified, PITR per strategy. Highlights and surprises are in `docs/findings.md` ("Backup and recovery").

## Anti-patterns the drills caught
- Restoring a PostgreSQL base backup into a container with default `max_connections` (100) while the primary had 300: `FATAL: recovery aborted because of insufficient parameter settings`.
- Trusting `ended - started` style filters or file names across major versions: MariaDB 11 renamed `xtrabackup_binlog_info` to `mariadb_backup_binlog_info`; CrateDB 6 turned timestamp subtraction into an interval.
- ClickHouse tables created with an explicit `{database}` Keeper path cannot be restored under another database name (`REPLICA_ALREADY_EXISTS`); use `{uuid}`.
- DuckDB `COPY FROM DATABASE` ignores foreign-key order; TiDB/CrateDB restores are in place (drop first); Oracle Data Pump has ~60 s fixed overhead even for a 35 MB schema.
- MySQL incremental "PITR" by replaying from the *previous* GTID position is only honest if the replay actually reaches the target GTID: check `Executed_Gtid_Set`/`gtid_slave_pos`, not just the row count.
