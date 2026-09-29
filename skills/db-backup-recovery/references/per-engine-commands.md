# Per-engine backup / restore commands (verified by `harness/rdlab/backup/*.py`)

All paths are inside the containers; `/backups` is the shared volume declared in each `stacks/<engine>/compose.yaml`
(created by the `volumes-init`/`backup-init` one-shot job with mode 1777). `<mk>` = the harness marker; `lab` = the benchmark database.

## PostgreSQL 18 (`backup/pg.py`)
```bash
# logical: directory format dumps and restores in parallel, zstd is faster than the default gzip
pg_dump -U lab -d lab -Fd -j 4 --compress=zstd -f /backups/dump
pg_dumpall -U lab --globals-only -f /backups/globals.sql          # roles/tablespaces are not in pg_dump
createdb lab_restore && pg_restore -U lab -d lab_restore -j 4 --no-owner /backups/dump
# physical
pg_basebackup -U lab -D /backups/full -Fp -Xs --checkpoint=fast --manifest-checksums=CRC32C
pg_verifybackup /backups/full
# PG17+ incremental (primary: summarize_wal=on, wal_summary_keep_time=1d)
pg_basebackup -U lab -D /backups/incr1 -Fp -Xs --checkpoint=fast --incremental=/backups/full/backup_manifest
pg_combinebackup /backups/full /backups/incr1 -o "$PGDATA"       # synthetic full for the restore
# PITR (primary: archive_mode=on, archive_command='test ! -f /backups/wal/%f && cp %p /backups/wal/%f', archive_timeout=60)
SELECT pg_create_restore_point('rdlab_pitr'); SELECT pg_switch_wal();      -- then wait for pg_stat_archiver.last_archived_wal
cp -a /backups/full/. "$PGDATA"
cat >> "$PGDATA/postgresql.auto.conf" <<EOF
restore_command = 'cp /backups/wal/%f %p'
recovery_target_name = 'rdlab_pitr'
recovery_target_action = 'promote'
EOF
touch "$PGDATA/recovery.signal"; postgres -c max_connections=300 -c hot_standby=on ...   # >= the primary's settings
```
Gotchas: command-line `-c` options outrank `ALTER SYSTEM`; put runtime-tunable settings in `postgresql.conf` (init script). The scratch
instance needs `max_connections`, `max_worker_processes`, `max_prepared_transactions`, `max_locks_per_transaction`, `max_wal_senders`
at least as large as the primary's. pgBackRest / WAL-G are the production tools (not in the pgvector image); they automate exactly
this flow (repository, full/diff/incr, WAL archive push/get, PITR) with checksums, parallelism and retention.

## Citus 13 (`backup/citus.py`)
- Logical: `pg_dump` through the coordinator (COPY pulls the distributed rows); `pg_restore` gives plain local tables -> re-run
  `create_distributed_table` / `create_reference_table`. Citus metadata (`pg_dist_*`) is not in the dump.
- Consistent PITR: `SELECT citus_create_restore_point('rdlab_citus_pitr')` (blocks 2PC while it writes the point on every node),
  each node archives its WAL to `/backups/wal/<node>/`, `pg_basebackup` per node; restore every node to
  `recovery_target_name='rdlab_citus_pitr'` in containers whose network aliases are the original node names, so the restored
  coordinator's `pg_dist_node` resolves to the restored workers.

## MySQL 9.7 (`backup/mysql.py`)
```bash
mysqlsh --uri root:pw@localhost:3306 -- util dump-tables lab --all --outputUrl=/backups/shell --threads=4 --compression=zstd
mysqlsh --uri root:pw@localhost:3306 -- util load-dump /backups/shell --schema=lab_restore --threads=4 --resetProgress   # needs local_infile=ON
mysqldump -uroot -ppw --single-transaction --routines --triggers --events --set-gtid-purged=OFF lab > /backups/lab.sql
mysql -uroot -ppw lab_restore < /backups/lab.sql
```
```sql
INSTALL PLUGIN clone SONAME 'mysql_clone.so';
CLONE LOCAL DATA DIRECTORY = '/backups/clone';                -- physical hot copy, then: mysqld --datadir=/backups/clone --server-id=99
SELECT state, gtid_executed FROM performance_schema.clone_status;
-- PITR on the clone: replay the source binlog by GTID and stop before the bad transaction
CHANGE REPLICATION SOURCE TO SOURCE_HOST='source', SOURCE_USER='repl', SOURCE_PASSWORD='replpass', SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1;
START REPLICA UNTIL SQL_BEFORE_GTIDS='<server_uuid>:<n>';     -- n = first transaction after the last good gtid_executed
SHOW REPLICA STATUS;                                           -- Replica_SQL_Running: No, Executed_Gtid_Set ends at n-1
```
The official `mysql:9` image ships `mysqlsh` and the clone plugin but **no `mysqlbinlog`**; replication-until-GTID is the PITR path that
needs nothing else. `binlog-expire-logs-seconds` bounds the PITR window.

## MariaDB 11.8 (`backup/mysql.py`)
```bash
mariadb-dump -uroot -ppw --single-transaction --routines --triggers --events --gtid lab > /backups/lab.sql
mariadb-backup --backup --user=root --password=pw --target-dir=/backups/full
mariadb-backup --prepare --target-dir=/backups/full
mariadb-backup --backup --user=root --password=pw --target-dir=/backups/inc1 --incremental-basedir=/backups/full
mariadb-backup --prepare --target-dir=/backups/full --incremental-dir=/backups/inc1     # applies inc1 into full
cat /backups/full/mariadb_backup_binlog_info        # binlog.000002  110871327  1-1-85   (11.x name; older: xtrabackup_binlog_info)
# scratch instance: mariadbd --datadir=/backups/full --server-id=77 --skip-slave-start
```
```sql
SET GLOBAL gtid_slave_pos = '1-1-85';
CHANGE MASTER TO master_host='primary', master_user='repl', master_password='replpass', master_use_gtid=slave_pos;
START SLAVE UNTIL master_gtid_pos = '1-1-90';                 -- the GTID before the DELETE; check @@gtid_slave_pos afterwards
```
In-place undo of a bad ROW-format transaction: `mariadb-binlog --flashback --start-position=<pos1> --stop-position=<pos2> /var/lib/mysql/binlog.000002 | mariadb lab`
(positions from `SHOW MASTER STATUS` before/after).

## Percona XtraDB Cluster 8.4 (`backup/mysql.py`)
```bash
XB=/usr/bin/pxc_extra/pxb-8.4/bin/xtrabackup                 # bundled for SST, not on PATH
$XB --backup --user=root --password=pw --socket=/tmp/mysql.sock --target-dir=/backups/full
$XB --backup ... --target-dir=/backups/inc1 --incremental-basedir=/backups/full
$XB --prepare --apply-log-only --target-dir=/backups/full
$XB --prepare --target-dir=/backups/full --incremental-dir=/backups/inc1
# restore: mysqld --datadir=/backups/full --wsrep-provider=none --pxc-encrypt-cluster-traffic=OFF (as uid 1001)
```

## TiDB 8.5 (`backup/tidb.py`) — BR is linked into tidb-server
```sql
BACKUP DATABASE lab TO 'local:///backups/full';                        -- Destination | Size | BackupTS | Queue Time | Execution Time
BACKUP DATABASE lab TO 'local:///backups/inc1' LAST_BACKUP = <BackupTS>;
DROP TABLE inventory; RESTORE TABLE lab.inventory FROM 'local:///backups/full';   -- no rename: restore in place
FLASHBACK TABLE backup_probe;                                            -- after DROP, within tidb_gc_life_time
SELECT COUNT(*) FROM backup_probe AS OF TIMESTAMP '2026-09-14 08:29:01.123456';
FLASHBACK CLUSTER TO TIMESTAMP '2026-09-14 08:29:01.123456';            -- whole cluster, in place, ~40 s here, blocks reads/writes
```
`local://` needs the same directory on every TiKV and TiDB container (one shared volume does it). Log backup / PITR across GC needs the `br` binary.

## CockroachDB 26.2 (`backup/cockroach.py`)
```sql
BACKUP DATABASE lab INTO 'nodelocal://1/lab' WITH revision_history;
BACKUP DATABASE lab INTO LATEST IN 'nodelocal://1/lab' WITH revision_history;      -- incremental layer
SHOW BACKUP LATEST IN 'nodelocal://1/lab';
RESTORE DATABASE lab FROM LATEST IN 'nodelocal://1/lab' WITH new_db_name = 'lab_restore';
RESTORE DATABASE lab FROM LATEST IN 'nodelocal://1/lab' AS OF SYSTEM TIME '1789375647.5' WITH new_db_name = 'lab_pitr';
```
`nodelocal://1/` is node 1's `<store>/extern`; other nodes reach it through node 1. Backups ran without any license key on v26.2.

## YugabyteDB 2026.1 (`backup/yugabyte.py`)
```bash
postgres/bin/ysql_dump -h yb1 -U yugabyte -d lab -f /backups/lab.sql; bin/ysqlsh -h yb1 -U yugabyte -d lab_restore -f /backups/lab.sql
bin/yb-admin -master_addresses yb1:7100,yb2:7100,yb3:7100 create_database_snapshot ysql.lab      # -> snapshot id; list_snapshots -> COMPLETE
bin/yb-admin ... export_snapshot <id> /backups/lab.snapshot                                      # metadata for an off-cluster copy
bin/yb-admin ... restore_snapshot <id>                                                            # in place; list_snapshot_restorations -> RESTORED
bin/yb-admin ... create_snapshot_schedule 1 10 ysql.lab                                           # every 1 min, keep 10 min -> {"schedule_id": ...}
bin/yb-admin ... restore_snapshot_schedule <schedule-id> "2026-09-14 09:05:12.123456"            # PITR, in place
```
Only one schedule per namespace (`delete_snapshot_schedule` first); `DROP DATABASE` fails while other sessions use it (terminate them).

## SQL Server 2025 (`backup/mssql.py`)
```sql
BACKUP DATABASE [lab] TO DISK = N'/backups/lab-full.bak' WITH INIT, COMPRESSION, CHECKSUM, STATS = 25;
RESTORE VERIFYONLY FROM DISK = N'/backups/lab-full.bak' WITH CHECKSUM;
RESTORE FILELISTONLY FROM DISK = N'/backups/lab-full.bak';           -- logical names for WITH MOVE
RESTORE DATABASE [lab_restore] FROM DISK = N'/backups/lab-full.bak' WITH MOVE N'lab' TO N'/var/opt/mssql/data/lab_restore_lab.mdf', MOVE N'lab_log' TO N'...ldf', RECOVERY;
BACKUP DATABASE [lab] TO DISK = N'/backups/lab-diff.bak' WITH DIFFERENTIAL, INIT, COMPRESSION, CHECKSUM;
BACKUP LOG [lab] TO DISK = N'/backups/lab-log1.trn' WITH INIT, COMPRESSION, CHECKSUM;
RESTORE DATABASE [lab_pitr] FROM DISK = N'...full.bak' WITH MOVE ..., NORECOVERY;
RESTORE DATABASE [lab_pitr] FROM DISK = N'...diff.bak' WITH NORECOVERY;
RESTORE LOG [lab_pitr] FROM DISK = N'...log1.trn' WITH STOPAT = N'2026-09-14 09:20:31.123', RECOVERY;
SELECT backup_size, compressed_backup_size FROM msdb.dbo.backupset WHERE database_name = 'lab';
```

## Oracle 26ai Free, 23.26 (`backup/oracle.py`)
```bash
sqlplus / as sysdba: ALTER SESSION SET CONTAINER = FREEPDB1; CREATE OR REPLACE DIRECTORY rdlab_bk AS '/backups'; GRANT READ, WRITE ON DIRECTORY rdlab_bk TO lab;
expdp system/pw@localhost/FREEPDB1 SCHEMAS=lab DIRECTORY=rdlab_bk DUMPFILE=lab.dmp LOGFILE=exp.log REUSE_DUMPFILES=YES
CREATE USER lab_restore IDENTIFIED BY x QUOTA UNLIMITED ON USERS; GRANT CONNECT, RESOURCE, CREATE VIEW TO lab_restore;
impdp system/pw@localhost/FREEPDB1 SCHEMAS=lab REMAP_SCHEMA=lab:lab_restore DIRECTORY=rdlab_bk DUMPFILE=lab.dmp TABLE_EXISTS_ACTION=REPLACE   # rc 5 = warnings
```
```sql
GRANT EXECUTE ON DBMS_FLASHBACK TO lab; GRANT FLASHBACK ANY TABLE TO lab;
ALTER TABLE backup_probe ENABLE ROW MOVEMENT;
SELECT DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER FROM dual;             -- wait > 5 s after any DDL on the table
SELECT COUNT(*) FROM backup_probe AS OF SCN :scn;  FLASHBACK TABLE backup_probe TO SCN :scn;  FLASHBACK TABLE backup_probe TO BEFORE DROP;
```
RMAN is absent from `gvenzl/oracle-free:*-slim*`; with the full image: `ALTER DATABASE ARCHIVELOG` (mount), `RMAN> CONFIGURE CONTROLFILE AUTOBACKUP ON; BACKUP AS COMPRESSED BACKUPSET DATABASE PLUS ARCHIVELOG; RECOVER DATABASE UNTIL TIME '...'`.

## Db2 12.1 (`backup/db2.py`, `su - db2inst1 -c "db2 ..."`)
```
db2 force applications all; db2 deactivate db lab; db2 backup db lab to /backups compress without prompting        # offline (retry: the healthcheck reconnects)
db2 restore db lab from /backups taken at <ts> into labr replace existing without prompting; db2 rollforward db labr complete
db2 update db cfg for lab using LOGARCHMETH1 DISK:/backups/logs/ TRACKMOD ON; <offline backup again: leaves "backup pending">
db2 backup db lab online to /backups compress include logs without prompting
db2 backup db lab online incremental to /backups compress include logs without prompting
db2 restore db lab incremental from /backups taken at <inc> into labi logtarget /backups/lt replace existing without prompting   # target image
db2 restore db lab incremental from /backups taken at <full> into labi without prompting                                          # base
db2 restore db lab incremental from /backups taken at <inc> into labi without prompting                                           # target again
db2 archive log for db lab
db2 rollforward db labi to end of backup and complete overflow log path (/backups/logs/db2inst1/LAB/NODE0000/LOGSTREAM0000/C0000000)
db2 rollforward db labp to 2026-09-14-10.35.13 using local time and complete overflow log path (<same dir>)                     # PITR
```
`INCREMENTAL AUTOMATIC` resolved the wrong chain once old images had been deleted from the history: the manual sequence is deterministic.
`OVERFLOW LOG PATH` takes one directory (a second one means another partition). Drop a half-restored `labp` before retrying (SQL2574N).

## ClickHouse 26.8 (`backup/clickhouse.py`)
```xml
<storage_configuration><disks><backups><type>local</type><path>/backups/</path></backups></disks></storage_configuration>
<backups><allowed_disk>backups</allowed_disk><allowed_path>/backups/</allowed_path></backups>
```
```sql
BACKUP DATABASE lab TO Disk('backups', 'full');                              -- one replica
BACKUP DATABASE lab ON CLUSTER lab_cluster TO Disk('backups', 'cluster');    -- every shard once, replicas share the parts
BACKUP DATABASE lab TO Disk('backups', 'inc1') SETTINGS base_backup = Disk('backups', 'full');
RESTORE DATABASE lab AS lab_restore FROM Disk('backups', 'full');            -- Keeper path must use {uuid}
DROP TABLE lab.inventory ON CLUSTER lab_cluster SYNC; RESTORE TABLE lab.inventory ON CLUSTER lab_cluster FROM Disk('backups', 'cluster');
SELECT status, num_files, total_size, compressed_size FROM system.backups ORDER BY start_time DESC;
```
`system.backups.total_size` counts files referenced from the base backup; bytes actually written = `du` of the backup directory.

## CrateDB 6.4 (`backup/crate.py`) — node setting `-Cpath.repo=/backups`
```sql
CREATE REPOSITORY lab_backups TYPE fs WITH (location = '/backups/crate', compress = true);
CREATE SNAPSHOT lab_backups.snap1 ALL WITH (wait_for_completion = true);
RESTORE SNAPSHOT lab_backups.snap1 TABLE doc.customers, doc.orders WITH (wait_for_completion = true, schema_rename_pattern = 'doc', schema_rename_replacement = 'restored');
DROP TABLE backup_probe; RESTORE SNAPSHOT lab_backups.snap2 TABLE doc.backup_probe WITH (wait_for_completion = true);
SELECT name, state, started, finished FROM sys.snapshots;
```

## QuestDB 10 (`backup/questdb.py`)
`CHECKPOINT CREATE;` -> `cp -a db conf .checkpoint snapshot /backups/questdb-full/` -> `CHECKPOINT RELEASE;` (always release, even on failure).
Restore: copy into a fresh root, `touch /var/lib/questdb/_restore`, `chown -R questdb`, start the same major version.

## MonetDB Dec2025 (`backup/monetdb.py`)
```bash
export DOTMONETDBFILE=/tmp/.monetdb   # user=monetdb / password=...
msqldump -d lab > /backups/lab.sql
monetdb create lab_restore && monetdb release lab_restore
mclient -d lab_restore -s "ALTER USER SET PASSWORD 'labpass' USING OLD PASSWORD 'monetdb'"   # new dbs start with monetdb/monetdb
mclient -d lab_restore < /backups/lab.sql
mclient -d lab -s "CALL sys.hot_snapshot('/backups/lab-hot.tar')"
tar -C /var/monetdb5/dbfarm -xf /backups/lab-hot.tar --transform 's,^lab/,lab_snap/,' && monetdb release lab_snap
```

## Firebird 5 (`backup/firebird.py`, `/opt/firebird/bin`)
```bash
gbak -b -g -user lab -password labpass localhost:/var/lib/firebird/data/lab.fdb /backups/lab.fbk
gbak -c -user lab -password labpass /backups/lab.fbk localhost:/var/lib/firebird/data/lab_restore.fdb
nbackup -B 0 /var/lib/firebird/data/lab.fdb /backups/lab-level0.nbk -user lab -password labpass     # as the file owner (root in the image)
nbackup -B 1 /var/lib/firebird/data/lab.fdb /backups/lab-level1.nbk -user lab -password labpass
nbackup -R /var/lib/firebird/data/lab_nbk.fdb /backups/lab-level0.nbk /backups/lab-level1.nbk
```

## H2 2.3 (`backup/h2.py`, PostgreSQL-protocol server)
`BACKUP TO '/backups/lab.zip'`; `java -cp /opt/h2.jar org.h2.tools.Restore -file /backups/lab.zip -dir /data -db lab_restore`;
`SCRIPT TO '/backups/lab.sql'`; on a new database `RUNSCRIPT FROM '/backups/lab.sql'` (the `COMPRESSION GZIP` option is rejected over the PG protocol).

## SQLite 3.45 / DuckDB 1.5 (`backup/embedded.py`)
```sql
VACUUM INTO 'data/sqlite/backups/lab-vacuum.db';          -- consistent, compacted; PRAGMA integrity_check on the copy
-- python: src.backup(dst, pages=4096)                     -- sqlite3_backup API, page copy that lets writers in between
EXPORT DATABASE 'data/duckdb/backups/export-parquet' (FORMAT parquet, COMPRESSION zstd);
ATTACH 'lab-import.duckdb' AS imp; USE imp; IMPORT DATABASE 'data/duckdb/backups/export-parquet';
ATTACH 'lab-copy.duckdb' AS cp; COPY FROM DATABASE lab TO cp (SCHEMA); INSERT INTO cp.customers SELECT * FROM lab.customers; ...  -- FK order
```
