---
name: mariadb-impl-backup-restore
description: >
  Use when planning backup strategy, performing logical or physical backup, restoring from backup, doing point-in-time recovery, or running incremental backup chains.
  Prevents the common mistake of mysqldump locking production, missing PITR coverage with only logical backups, restoring incremental chain in wrong order, or backing up a Galera donor mid-transfer.
  Covers mysqldump / mariadb-dump 10.5+ logical backup, mariabackup / mariadb-backup physical hot-backup (incremental chains, --prepare, --copy-back), point-in-time recovery via mariadb-binlog --start-datetime / --start-position, single-table restore via --export + DISCARD/IMPORT TABLESPACE, encrypted backup via --stream piped to gpg/openssl, backup verification, RPO/RTO planning matrix, and Galera-specific donor-node selection.
  Keywords: backup, restore, mysqldump, mariadb-dump, mariabackup, mariadb-backup, physical backup, logical backup, hot backup, incremental backup, point in time recovery, PITR, RPO, RTO, mysqlbinlog, mariadb-binlog, --single-transaction, --prepare, --apply-log-only, --copy-back, --move-back, --export, --incremental-basedir, --incremental-dir, --target-dir, --stream, xbstream, DISCARD TABLESPACE, IMPORT TABLESPACE, binlog replay, Galera donor, SST backup, my backup is slow, how to restore single table, how to do point in time recovery, restore is missing data, last full backup is too old, backup hangs, dump locks tables, how often should I backup
license: MIT
compatibility: "Designed for Claude Code. Requires MariaDB 10.6-LTS, 10.11-LTS, 11.x, 12.x."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# MariaDB Backup and Restore

A backup that has never been restored is not a backup. This skill covers the two production-grade backup paths in MariaDB (logical via `mariadb-dump`, physical via `mariadb-backup`), incremental chains, point-in-time recovery via binlog replay, single-table restore via the transportable-tablespace flow, encrypted streaming backups, verification routines, and the RPO/RTO trade-offs that drive the strategy choice. Read this before designing any backup plan for a non-trivial database.

## Quick Reference

- ALWAYS use `mariadb-backup` (renamed from `mariabackup` in 10.5+) for production : it is the hot, InnoDB-aware physical backup tool. Reserve `mariadb-dump` for small databases, test fixtures, or cross-version migrations.
- ALWAYS run `mariadb-backup --prepare` on a physical backup BEFORE restoring. The raw `--backup` output is in an inconsistent crash-recovery state ; `--prepare` applies pending redo and rolls back uncommitted transactions.
- ALWAYS pass `--single-transaction` to `mariadb-dump` on InnoDB workloads. Without it the default behaviour is `LOCK TABLES` per database, which blocks all writes for the dump duration. `--single-transaction` is ignored for MyISAM and Aria tables (they are NOT transactional), which still get locked.
- ALWAYS pair `--routines`, `--triggers`, and `--events` with `mariadb-dump`. Defaults DO NOT include them ; restored databases silently lose stored procedures, triggers, and scheduled events.
- ALWAYS keep continuous binlog archival if RPO must be tighter than your backup interval. A full backup at 02:00 with no binlogs gives 24-hour worst-case RPO ; the same backup plus archived binlogs gives sub-minute RPO.
- ALWAYS use `--apply-log-only` on the base and all intermediate incremental prepares, and the plain `--prepare` (no `--apply-log-only`) on the final increment. Wrong order or wrong flag corrupts the chain.
- ALWAYS verify every backup type at least monthly by restoring to a sandbox host and running `mariadb-check --all-databases` plus a row-count sanity query against canonical tables.
- NEVER run `mariadb-dump` on a multi-GB production database without `--single-transaction` : table locks will cascade into application timeouts.
- NEVER restore an incremental chain in the wrong order. Order is : prepare base with `--apply-log-only`, prepare each increment in order with `--apply-log-only`, prepare the final increment WITHOUT `--apply-log-only`, then `--copy-back`.
- NEVER use the deprecated `--compress` option in `mariadb-backup` : it relies on the unmaintained QuickLZ algorithm. Use `--stream=xbstream | zstd -T0` instead.
- NEVER take a Galera SST-donor backup on a node that is currently donating an SST to a joiner : it can corrupt the backup mid-transfer. Run `mariadb-backup` on a non-donor node, prefer a dedicated backup node (`wsrep_node_name=backup-1` + `pc.weight=0` so it never wins elections).
- NEVER store backups on the same physical host as the database : a disk-loss event takes both with it. Always replicate or rsync to a remote target before considering the backup retained.
- The `mysqldump`, `mysqlbinlog`, and `mariabackup` binaries are 10.5+ compatibility symlinks for `mariadb-dump`, `mariadb-binlog`, and `mariadb-backup`. Old names work but scripts must update before MariaDB 11.0+ packaging changes drop the symlinks in some distributions.

## Decision Trees

### Backup-tool selection

```
Database size < 50 GB AND restore-time budget > 30 min ?
   YES : mariadb-dump --single-transaction --routines --triggers --events
   NO  : continue

Need hot backup (cannot stop writes) AND mostly InnoDB ?
   YES : mariadb-backup (physical hot backup)
   NO  : continue

Cross-version migration or schema-only export ?
   YES : mariadb-dump --no-data for schema ; mariadb-dump --no-create-info for data
   NO  : mariadb-backup is the default for production-size data
```

`mariadb-dump` restore replays SQL statement-by-statement : a 100 GB dump can take many hours to restore. `mariadb-backup` restore is a file-copy operation : the same data restores in tens of minutes.

### RPO / RTO and strategy selection

```
RPO requirement : how much data loss is acceptable ?
   < 1 minute   : full + incremental + continuous binlog archival
   < 1 hour     : full nightly + incremental hourly
   < 24 hours   : full nightly (logical or physical)
   > 24 hours   : weekly full only (not recommended for production)

RTO requirement : how quickly must the database be back up ?
   < 30 minutes  : mariadb-backup --copy-back (file copy ; minutes)
   < 4 hours     : mariadb-dump replay (statement-by-statement ; depends on DB size)
   > 4 hours     : any
```

A common production-grade plan : full `mariadb-backup` daily at 02:00, hourly `mariadb-backup --incremental-basedir=` chain, binlogs archived off-host continuously (e.g. via `purge_binary_logs` cron after rsync). This gives RPO under one minute (limited by binlog flush interval) and RTO under one hour for a 500 GB database.

### Point-in-time recovery scenario

```
Need to undo a destructive query (DROP TABLE, UPDATE without WHERE) ?
   YES : restore most-recent full + increments before the bad query,
         then replay binlogs with --stop-position right BEFORE the bad event
   NO  : continue

Need to roll forward to a specific moment ?
   YES : restore full + increments, replay binlogs with --stop-datetime='<moment>'
   NO  : continue

Need just the latest data ?
   restore full + all increments + replay all binlogs to end of file
```

PITR requires : (1) `binlog_format=ROW` (or MIXED) for safe replay across non-deterministic statements, (2) `log_bin=ON` and a known coordinate or datetime to stop at, (3) all binlogs from the moment of the base backup forward, with no gaps.

### Single-table restore

```
Production table dropped or corrupted, full restore unacceptable ?
   YES : transportable-tablespace flow :
         1. mariadb-backup --prepare --export --target-dir=/path
         2. On target : CREATE TABLE with matching schema
         3. ALTER TABLE t DISCARD TABLESPACE
         4. cp backup/db/t.ibd backup/db/t.cfg /var/lib/mysql/db/
         5. chown mysql:mysql /var/lib/mysql/db/t.{ibd,cfg}
         6. ALTER TABLE t IMPORT TABLESPACE
   NO  : full restore to a sandbox + mariadb-dump of the single table
```

The `--export` option during `--prepare` is REQUIRED to produce the `.cfg` metadata file alongside the `.ibd` tablespace file. Without `.cfg`, `IMPORT TABLESPACE` fails. Schema must match the source EXACTLY (column types, indexes, row format).

### Galera donor-node selection

```
Standalone server ?  use the only node, schedule when writes are quiet.
Galera cluster ?     dedicate one node as the backup-donor :
   wsrep_node_name=backup-1
   pc.weight=0                   (never wins primary-component election)
   wsrep_desync=ON during backup (de-syncs the node from cluster flow control)
   wsrep_desync=OFF after backup completes
NEVER back up a node currently donating an SST.
```

`pc.weight=0` keeps the backup node from being selected as the primary partition holder during a split, so a backup that runs across a network blip does not produce an inconsistent snapshot. `wsrep_desync=ON` removes flow-control feedback from the donor during the backup so cluster throughput is not throttled to the backup's pace.

## Patterns

### Logical backup with mariadb-dump (production-grade)

```bash
# 10.5+ : use the modern alias and the safety-flags InnoDB needs
mariadb-dump \
  --single-transaction \
  --quick \
  --routines \
  --triggers \
  --events \
  --hex-blob \
  --master-data=2 \
  --flush-logs \
  --all-databases \
  --user=backup_user --password='<secret>' \
  | zstd -T0 > /backup/$(date +%F)-full.sql.zst
```

- `--single-transaction` : InnoDB consistent snapshot with no global lock.
- `--quick` : do not buffer rows in memory ; stream them out.
- `--master-data=2` : write the binlog coordinate as an SQL comment ; needed for setting up replicas or PITR start position.
- `--flush-logs` : rotate binlogs at start so the next binlog covers everything after the dump.
- `--hex-blob` : binary-safe BLOB encoding.

Restore : `zstd -d -c /backup/<file>.sql.zst | mariadb -u root -p`.

### Physical full backup with mariadb-backup

```bash
# Target dir MUST be empty or not exist (per KB)
mariadb-backup --backup \
  --target-dir=/backup/full-$(date +%F) \
  --user=mariabackup_user --password='<secret>' \
  --parallel=4 \
  --galera-info \
  --slave-info

# Prepare (MANDATORY before any restore)
mariadb-backup --prepare \
  --target-dir=/backup/full-$(date +%F)

# Restore : stop server, empty datadir, copy-back, fix perms, start server
systemctl stop mariadb
rm -rf /var/lib/mysql/*
mariadb-backup --copy-back \
  --target-dir=/backup/full-$(date +%F)
chown -R mysql:mysql /var/lib/mysql/
systemctl start mariadb
```

`--galera-info` writes the cluster GTID coordinate ; `--slave-info` writes `CHANGE MASTER TO` for setting up a replica from this backup.

### Incremental backup chain

```bash
# Sunday : full
mariadb-backup --backup \
  --target-dir=/backup/2026-W20/full \
  --user=mariabackup_user --password='<secret>'

# Monday-Saturday : incremental, each pointing to previous
mariadb-backup --backup \
  --target-dir=/backup/2026-W20/inc-mon \
  --incremental-basedir=/backup/2026-W20/full \
  --user=mariabackup_user --password='<secret>'

mariadb-backup --backup \
  --target-dir=/backup/2026-W20/inc-tue \
  --incremental-basedir=/backup/2026-W20/inc-mon \
  --user=mariabackup_user --password='<secret>'
# ... continue chain ...

# Restore : prepare in chain order, --apply-log-only on all but final
mariadb-backup --prepare --apply-log-only \
  --target-dir=/backup/2026-W20/full

mariadb-backup --prepare --apply-log-only \
  --target-dir=/backup/2026-W20/full \
  --incremental-dir=/backup/2026-W20/inc-mon

mariadb-backup --prepare --apply-log-only \
  --target-dir=/backup/2026-W20/full \
  --incremental-dir=/backup/2026-W20/inc-tue

# Final increment : NO --apply-log-only (rolls back uncommitted)
mariadb-backup --prepare \
  --target-dir=/backup/2026-W20/full \
  --incremental-dir=/backup/2026-W20/inc-sat

# Then standard copy-back as in the full-backup pattern
```

The chain is rebuilt INTO the `full` directory ; do not change `--target-dir` between prepare steps. `--apply-log-only` defers the rollback phase so subsequent increments can apply cleanly on top.

### Point-in-time recovery walkthrough

```bash
# Scenario : someone ran DROP TABLE orders at 14:32 today.
# Last full backup completed at 02:00 with --master-data=2.
# Binlogs since 02:00 are in /var/lib/mysql/binlog/.

# Step 1 : restore the most recent full + applicable increments to a SANDBOX
# (NEVER restore over a production server during incident response).
systemctl stop mariadb-sandbox
rm -rf /var/lib/mysql-sandbox/*
mariadb-backup --copy-back \
  --target-dir=/backup/2026-05-19/full
chown -R mysql:mysql /var/lib/mysql-sandbox/
systemctl start mariadb-sandbox

# Step 2 : extract start coordinate from the backup
grep "CHANGE MASTER" /backup/2026-05-19/full/xtrabackup_binlog_info
# Example output : mariadb-bin.000123  4523

# Step 3 : replay binlogs from the backup coordinate UP TO 14:31:59
mariadb-binlog \
  --start-position=4523 \
  --stop-datetime='2026-05-19 14:31:59' \
  /var/lib/mysql/binlog/mariadb-bin.000123 \
  /var/lib/mysql/binlog/mariadb-bin.000124 \
  /var/lib/mysql/binlog/mariadb-bin.000125 \
  | mariadb --socket=/var/run/mysqld/sandbox.sock -u root -p

# Step 4 : dump the orders table from sandbox and import to production
mariadb-dump --socket=/var/run/mysqld/sandbox.sock \
  -u root -p production_db orders > /tmp/orders.sql
mariadb -u root -p production_db < /tmp/orders.sql
```

`--stop-datetime` stops BEFORE the matching event ; use `--stop-position=<n>` for exact-coordinate stop when you have the position from the binlog. The pipe to `mariadb` (or `mysql` legacy alias) replays events as SQL on the target server.

### Single-table restore via transportable tablespace

```bash
# Step 1 : prepare backup with --export to generate .cfg files
mariadb-backup --prepare --export \
  --target-dir=/backup/full-2026-05-19

# Step 2 : on the target server, recreate the table with IDENTICAL schema
mariadb -u root -p production_db <<SQL
CREATE TABLE orders_restored LIKE orders;
ALTER TABLE orders_restored DISCARD TABLESPACE;
SQL

# Step 3 : copy the tablespace and cfg files into the datadir
cp /backup/full-2026-05-19/production_db/orders.ibd \
   /var/lib/mysql/production_db/orders_restored.ibd
cp /backup/full-2026-05-19/production_db/orders.cfg \
   /var/lib/mysql/production_db/orders_restored.cfg
chown mysql:mysql /var/lib/mysql/production_db/orders_restored.{ibd,cfg}

# Step 4 : import the tablespace
mariadb -u root -p production_db <<SQL
ALTER TABLE orders_restored IMPORT TABLESPACE;
SQL
```

The schema of the empty target table must match the source EXACTLY : column types, NULL/NOT NULL, ROW_FORMAT, KEY_BLOCK_SIZE, character set, all secondary indexes. Mismatches cause `ERROR 1808 Schema mismatch`.

### Encrypted streaming backup

```bash
# Stream xbstream to gpg ; never write plaintext to disk
mariadb-backup --backup \
  --stream=xbstream \
  --target-dir=/tmp \
  --user=mariabackup_user --password='<secret>' \
  | gpg --batch --yes --cipher-algo AES256 \
        --passphrase-file /etc/mariadb/backup-passphrase \
        --symmetric --output /backup/full-$(date +%F).xbstream.gpg

# Restore : decrypt, extract, prepare, copy-back
mkdir -p /restore/full-2026-05-19
gpg --batch --decrypt --passphrase-file /etc/mariadb/backup-passphrase \
    /backup/full-2026-05-19.xbstream.gpg \
  | mbstream -x -C /restore/full-2026-05-19
mariadb-backup --prepare --target-dir=/restore/full-2026-05-19
# ... then copy-back as usual ...
```

`--compress` (built-in QuickLZ) is deprecated ; ALWAYS stream to a third-party tool (`zstd`, `gzip`, `gpg`) for compression and encryption. The 12.0.1+ `file_key_management_use_pbkdf2` server-side key-derivation is unrelated : that protects the data-at-rest in the live datadir, not the backup output.

### Backup verification routine

```bash
#!/bin/bash
# Monthly verification : restore to sandbox, run sanity checks
BACKUP=/backup/full-$(date +%F)
SANDBOX_DIR=/var/lib/mysql-verify
SANDBOX_SOCK=/var/run/mysqld/verify.sock

systemctl stop mariadb-verify
rm -rf "$SANDBOX_DIR"/*
mariadb-backup --copy-back --target-dir="$BACKUP" --datadir="$SANDBOX_DIR"
chown -R mysql:mysql "$SANDBOX_DIR"
systemctl start mariadb-verify

mariadb-check --socket="$SANDBOX_SOCK" -u root -p --all-databases || exit 1
mariadb --socket="$SANDBOX_SOCK" -u root -p -e \
  "SELECT COUNT(*) FROM production_db.orders" \
  | tee /var/log/backup-verify-$(date +%F).log
```

An unverified backup is not a backup. If the verification script has never run successfully, assume the backup will fail on the day you need it.

## Cross-References

- `mariadb-impl-replication-setup` for `--master-data` coordinates, GTID, and bootstrapping replicas from a `mariadb-backup` snapshot via `--slave-info`.
- `mariadb-impl-galera-cluster` for donor-node selection, `wsrep_desync` semantics, and SST-vs-IST trade-offs.
- `mariadb-core-replication-model` for MariaDB GTID (domain-server-sequence) vs MySQL GTID (uuid:seqno) incompatibility, which constrains migration backup strategies (per L-004).
- `mariadb-errors-replication-failures` for symptom-based debugging when PITR replay halts on a duplicate-key or missing-row error.
- `mariadb-core-security-model` for `mariabackup_user` privileges (RELOAD, PROCESS, LOCK TABLES, REPLICATION CLIENT, BACKUP_ADMIN on 10.5+).

## Reference Files

- `references/methods.md` : complete option reference for `mariadb-dump`, `mariadb-backup`, and `mariadb-binlog` ; the transportable-tablespace flow in full ; the RPO/RTO matrix per strategy ; Galera-specific options and version-rename table.
- `references/examples.md` : 10+ end-to-end working examples covering full, incremental, encrypted, partial, PITR, single-table, replica-bootstrap, and verification scenarios.
- `references/anti-patterns.md` : 8 production failures (dump-on-prod-without-single-transaction, restore-without-prepare, PITR-without-binlogs, wrong-chain-order, Galera-SST-collision, missing-routines, cross-version-grant-loss, same-host backup storage) with cause, symptom, and fix.

## Sources

Verified against : `mariadb.com/kb/en/mariabackup/`, `mariadb.com/docs/server/server-usage/backup-and-restore/mariadb-backup/full-backup-and-restore-with-mariadb-backup`, `mariadb.com/docs/server/server-usage/backup-and-restore/mariadb-backup/incremental-backup-and-restore-with-mariadb-backup`, `mariadb.com/docs/server/server-usage/backup-and-restore/mariadb-backup/partial-backup-and-restore-with-mariadb-backup`, `mariadb.com/docs/server/server-usage/backup-and-restore/mariadb-backup/mariadb-backup-options`, `mariadb.com/kb/en/mysqldump/`, `mariadb.com/kb/en/mariadb-binlog/`, `mariadb.com/docs/server/clients-and-utilities/logging-tools/mariadb-binlog/using-mariadb-binlog`, `mariadb.com/kb/en/point-in-time-recovery/`. Last verified : 2026-05-19.
