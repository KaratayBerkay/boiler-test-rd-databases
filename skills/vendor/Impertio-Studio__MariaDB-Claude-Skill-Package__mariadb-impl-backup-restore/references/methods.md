# MariaDB Backup and Restore : Methods Reference

Complete option reference for `mariadb-dump`, `mariadb-backup`, and `mariadb-binlog`, plus decision tables, the RPO/RTO matrix, the transportable-tablespace flow in detail, and the binary-rename table.

## Binary Rename Table

| Modern name (10.5+) | Legacy alias | Removed-from notes |
|---|---|---|
| `mariadb-dump` | `mysqldump` | Compatibility symlink ; package-maintainer choice whether to keep into 11.x. ALWAYS update scripts to the modern name. |
| `mariadb-binlog` | `mysqlbinlog` | Same. |
| `mariadb-backup` | `mariabackup` | Compatibility symlink. The legacy command name `mysqlbackup` is NOT a MariaDB tool : it is an Oracle MySQL Enterprise tool. |
| `mariadb-check` | `mysqlcheck` | Used in verification routines. |
| `mariadb` | `mysql` | Client used as the receiver for `mariadb-binlog | mariadb` PITR pipelines. |

Per Cluster-3 vooronderzoek §3, scripts hard-coding `mysqldump` may break when distributions drop the symlink in package updates after MariaDB 11.0+. Treat the modern names as canonical.

## mariadb-dump Option Reference (production-relevant subset)

| Option | Default | When to use |
|---|---|---|
| `--single-transaction` | OFF | ALWAYS on InnoDB workloads ; takes a transactional snapshot with NO global lock. Has NO effect on MyISAM/Aria (which get table-locked anyway). |
| `--quick` | ON in newer versions | Streams rows without buffering ; required for tables that do not fit in memory. |
| `--lock-tables` | ON (per database) | Default behaviour : `LOCK TABLES READ` on every table in each database before dumping. Replaced by `--single-transaction` for InnoDB. |
| `--lock-all-tables` | OFF | Acquires a global `FLUSH TABLES WITH READ LOCK`. NEVER use on production ; blocks all writes globally. |
| `--routines` | OFF | INCLUDE stored procedures and stored functions. Default OFF silently drops them on restore. |
| `--triggers` | ON | Include triggers. Confirm with `--triggers` explicitly for cross-version safety. |
| `--events` | OFF | INCLUDE scheduled events (the `mysql.event` table contents). |
| `--master-data=2` | OFF | Write the binlog file + position as an SQL comment in the dump. Required to seed a replica or pin a PITR start point. `=1` writes an executable `CHANGE MASTER TO`. |
| `--source-data=2` | OFF | 10.5+ rename of `--master-data`. Same semantics. |
| `--flush-logs` | OFF | Rotate binary logs at start of dump ; the next binlog file then covers the post-dump period cleanly. |
| `--hex-blob` | OFF | Encode BLOB columns as hex. Always set if any binary data exists ; otherwise charset-conversion can corrupt bytes. |
| `--all-databases` | OFF | Dump every database including `mysql`. Default is `--databases` (named DBs) or single-DB-positional. |
| `--no-data` | OFF | Schema-only dump (DDL). |
| `--no-create-info` | OFF | Data-only dump (no CREATE TABLE). |
| `--default-character-set=utf8mb4` | utf8mb4 in 10.6+ | Set to match server charset to avoid encoding-conversion on restore. |
| `--add-drop-database`, `--add-drop-table` | varies | Include `DROP DATABASE/TABLE IF EXISTS` for idempotent re-runs. |
| `--ignore-table=db.t` | none | Exclude specific tables (heavy log tables for example). Repeatable. |

## mariadb-backup Option Reference

### Operations (mutually exclusive)

| Option | Purpose |
|---|---|
| `--backup` | Take a backup, write to `--target-dir`. |
| `--prepare` | Apply pending redo and (without `--apply-log-only`) roll back uncommitted txns. MANDATORY before restore. |
| `--copy-back` | Copy backup files into the live `datadir` ; preserves the backup in `--target-dir`. |
| `--move-back` | Same as `--copy-back` but moves the files (faster, deletes the backup as it goes ; only use if you have another copy). |

### Target directory

| Option | Notes |
|---|---|
| `--target-dir=<path>` | REQUIRED. For `--backup` the directory must be empty or not exist (per KB) ; for `--prepare`/`--copy-back` it points to the backup just taken. |
| `--datadir=<path>` | Optional override for the server data directory during `--copy-back` ; useful for sandbox restores. |

### Incremental

| Option | Phase | Purpose |
|---|---|---|
| `--incremental-basedir=<path>` | `--backup` | Path to the parent backup (full or previous incremental) ; only newer pages are copied. |
| `--incremental-dir=<path>` | `--prepare` | Path to the incremental directory to apply on top of `--target-dir`. |
| `--apply-log-only` | `--prepare` | Apply only the redo phase ; defer the rollback phase. Use on the base and all but the final increment. |

### Partial backup

| Option | Notes |
|---|---|
| `--databases="db1 db2"` | Space-separated list of databases (and optionally `db.tbl`). |
| `--databases-exclude=` | Inverse selector. |
| `--databases-file=<file>` | Read list from a file. |
| `--tables=<regex>` | Regex match on database.table names. |
| `--tables-exclude=<regex>` | Inverse regex. |
| `--tables-file=<file>` | List of fully-qualified tables. |
| `--export` | During `--prepare` ONLY ; generates the `.cfg` file alongside `.ibd` for transportable-tablespace restore. |

### Connection

| Option | Notes |
|---|---|
| `--user=`, `--password=` | Credentials for the backup user. |
| `--host=`, `--port=` | TCP connection. |
| `--socket=` | Unix socket. |
| `--defaults-file=` | Load credentials from `[mariabackup]` section of a my.cnf-style file (avoids exposing the password on the command line). |

### Performance and streaming

| Option | Notes |
|---|---|
| `--parallel=N` | Number of threads for file copy ; 4-8 typical on SSD. |
| `--stream=xbstream` | Write to stdout in xbstream format for piping to compression/encryption. |
| `--compress` | DEPRECATED (QuickLZ unmaintained) ; use `--stream` + external tool. |

### Replication and Galera

| Option | Notes |
|---|---|
| `--slave-info` | Record `CHANGE MASTER TO` for the replica's position at the backup point ; used to bootstrap new replicas. |
| `--safe-slave-backup` | Pause the SQL thread before backup to avoid lock contention from concurrent replication. |
| `--galera-info` | Record the Galera GTID coordinate (`xtrabackup_galera_info` file). |
| `--no-backup-locks` | Skip `BACKUP LOCK` / `BACKUP UNLOCK` ; rare ; only on pre-10.4 servers without backup-lock support. |

### Required privileges for the backup user (10.5+ recommended set)

```sql
CREATE USER 'mariabackup_user'@'localhost' IDENTIFIED BY '<secret>';
GRANT RELOAD, PROCESS, LOCK TABLES, BINLOG MONITOR, REPLICA MONITOR
  ON *.* TO 'mariabackup_user'@'localhost';
-- 10.5+ : the BACKUP_ADMIN privilege replaces some of the above for backup lock semantics
GRANT BACKUP_ADMIN ON *.* TO 'mariabackup_user'@'localhost';
```

On 10.4 and earlier use `REPLICATION CLIENT` instead of `BINLOG MONITOR`/`REPLICA MONITOR`. Insufficient privileges cause `mariadb-backup` to fail with a confusing `Access denied` ; always test the user before the first scheduled run.

## mariadb-binlog Option Reference (PITR-relevant subset)

| Option | Purpose |
|---|---|
| `--start-datetime='YYYY-MM-DD HH:MM:SS'` | Skip events with timestamps before this moment. Inclusive ; events AT this datetime are replayed. |
| `--stop-datetime='YYYY-MM-DD HH:MM:SS'` | Stop BEFORE the first event with a timestamp at or after this moment. To stop at a precise event, use `--stop-position`. |
| `--start-position=<n>` | Start at this binlog byte position. Get the post-backup coordinate from `xtrabackup_binlog_info`. |
| `--stop-position=<n>` | Stop before this binlog byte position. |
| `--database=<name>` | Filter events to a single database. |
| `--read-from-remote-server` | Pull binlogs from a remote MariaDB server instead of local files. |
| `--base64-output=decode-rows` | Decode ROW-format binlog events into pseudo-SQL for inspection (not for replay). |
| `--verbose` | Include decoded RBR events as pseudo-SQL comments. |

Datetime format is `YYYY-MM-DD HH:MM:SS` in the server's local timezone. Multiple binlog files can be passed in a single invocation in chronological order.

### Canonical PITR command

```bash
mariadb-binlog \
  --start-position=<coord-from-backup-info> \
  --stop-datetime='2026-05-19 14:31:59' \
  /var/lib/mysql/binlog/mariadb-bin.00012{3,4,5} \
  | mariadb -u root -p --binary-mode
```

`--binary-mode` on the receiving client preserves binary content faithfully. Without it, certain BLOB / utf8-encoded binary bytes get mangled.

## Transportable Tablespace Flow (Single-Table Restore)

Per the KB partial-backup-and-restore page, the full single-table flow is:

| Step | Command | Notes |
|---|---|---|
| 1 | `mariadb-backup --prepare --export --target-dir=/backup/full` | `--export` is REQUIRED to produce `.cfg` files. |
| 2 | On target : `CREATE TABLE t LIKE source_t;` | Schema MUST match source exactly (cols, types, indexes, row format, charset). |
| 3 | `ALTER TABLE t DISCARD TABLESPACE;` | Removes the empty `.ibd` placeholder. |
| 4 | `cp /backup/full/db/source_t.{ibd,cfg} /var/lib/mysql/db/t.{ibd,cfg}` | Rename files if target table name differs. |
| 5 | `chown mysql:mysql /var/lib/mysql/db/t.{ibd,cfg}` | Required ; otherwise `IMPORT TABLESPACE` fails with permission error. |
| 6 | `ALTER TABLE t IMPORT TABLESPACE;` | InnoDB validates the `.cfg` against the table schema. |

Common failure mode : `ERROR 1808 Schema mismatch`. Cause : the empty target table differs from the source in some attribute (e.g. `ROW_FORMAT=COMPACT` vs `DYNAMIC`, missing secondary index, different charset). Recreate the empty table with the EXACT source DDL.

## RPO / RTO Matrix Per Strategy

| Strategy | Typical RPO | Typical RTO (500 GB DB) | Storage cost | Operational cost |
|---|---|---|---|---|
| Weekly logical dump only | 7 days | Hours to days (statement replay) | Low | Very low |
| Nightly logical dump only | 24 hours | Hours (statement replay) | Low | Low |
| Nightly physical backup only | 24 hours | 30-60 min (file copy + prepare) | Medium | Low |
| Nightly full + hourly incremental | 1 hour | 45-90 min (chain prepare) | Medium | Medium |
| Nightly full + hourly incremental + continuous binlog archival | seconds to minutes | 1-2 hours (chain + binlog replay) | Medium | Medium-high |
| Galera 3-node cluster + nightly backup on dedicated node | seconds (synchronous replication) | minutes (failover) + backup as additional layer | High | High |

The cheapest dramatic improvement is adding continuous binlog archival to a nightly-physical-backup setup : it drops RPO from 24 hours to single minutes for the cost of an `rsync` cron job.

## Galera Backup Considerations

| Concern | Mitigation |
|---|---|
| Backup during SST corrupts the donor's snapshot | NEVER run `mariadb-backup` on a node with `wsrep_local_state_comment='Donor/Desynced'`. Check before backup. |
| Backup throttles cluster via flow control | `SET GLOBAL wsrep_desync=ON;` before backup ; `SET GLOBAL wsrep_desync=OFF;` after. |
| Backup node accidentally becomes Primary Component holder during split | Set `pc.weight=0` on the dedicated backup node so it never wins elections. |
| GTID coordinate must be captured for cluster bootstrap from backup | Use `--galera-info` ; the resulting `xtrabackup_galera_info` carries the Galera state UUID and last applied seqno. |

Dedicated backup-node pattern : keep one Galera node permanently de-prioritised (`pc.weight=0`), de-synced during backup windows, and used for `mariadb-backup --backup --galera-info` to feed the off-cluster backup target.
