# MariaDB Backup and Restore : Anti-Patterns

Eight production-grade failure modes drawn from MariaDB KB warnings, JIRA, and field experience. Each entry : cause, symptom, fix, and the correct alternative.

---

## AP-1 : mariadb-dump on production without --single-transaction

### Cause
Default `mariadb-dump` invocation issues `LOCK TABLES READ` on every table in each database before dumping. On a multi-database production server, this serialises behind every write transaction and acquires a global table-lock that blocks every INSERT, UPDATE, and DELETE for the dump duration.

### Symptom
- Application connection-pool exhaustion partway through the dump.
- Long-running write queries killed by the connection-pool timeout.
- Read-replica falls behind because the primary cannot commit during the dump.
- 502/504 errors in front-end services.

### Why it fails
The default `--lock-tables` behaviour is correct for MyISAM (which needs it for consistency) but catastrophic for InnoDB workloads at production scale. InnoDB's MVCC makes the lock unnecessary IF you take a transactional snapshot ; `--single-transaction` does exactly that.

### Fix
ALWAYS add `--single-transaction` for InnoDB-only workloads :

```bash
# WRONG : silently locks all tables
mariadb-dump --all-databases -u backup -p > /backup/full.sql

# RIGHT : transactional snapshot, no global lock
mariadb-dump \
  --single-transaction --quick --routines --triggers --events --hex-blob \
  --source-data=2 \
  --all-databases \
  -u backup -p > /backup/full.sql
```

### Caveat for mixed-engine schemas
`--single-transaction` is silently ignored for MyISAM and Aria tables : they still get `LOCK TABLES READ`. For mixed-engine schemas, switch to `mariadb-backup` (physical hot backup) which is engine-aware.

---

## AP-2 : Restoring a physical backup without --prepare

### Cause
A fresh `mariadb-backup --backup` output is in a crash-recovery state : InnoDB transactions that committed during the backup are in the redo log but not yet applied to the data files. `--prepare` applies them and rolls back uncommitted transactions. Without `--prepare`, the data files are an inconsistent snapshot.

### Symptom
- Server starts but logs `InnoDB: page is corrupted` or similar.
- Tables visible in `SHOW TABLES` but `SELECT` returns errors.
- Worst case : silent data corruption discovered later when constraint violations appear in clean queries.

### Why it fails
The KB is explicit : `--prepare` is mandatory. The `--backup` operation is an InnoDB hot-copy that captures a moving target ; the redo log is what makes the result consistent.

### Fix

```bash
# After --backup, BEFORE --copy-back :
mariadb-backup --prepare --target-dir=/backup/full-$(date +%F)
```

For incremental chains the prepare phase is multi-step (per AP-4 below).

---

## AP-3 : PITR plan without continuous binlog archival

### Cause
Backup plan covers only the periodic snapshot (logical or physical) and assumes binlogs are "right there on the server". When the server's disk fails or someone runs `PURGE BINARY LOGS BEFORE '<date>'`, the binlogs needed to recover between the last backup and the failure moment are gone. RPO collapses to "the last full backup", regardless of what the plan says.

### Symptom
- Disaster recovery succeeds in restoring the last nightly backup but loses up to 24 hours of writes.
- Compliance audits fail "RPO < 1 hour" claims.
- Customer support spends hours reconstructing the missing window from application logs.

### Why it fails
Binlogs are the ONLY source of changes between backups. They must be archived OFF the source host with the same discipline as the backups themselves.

### Fix
Configure a binlog rotation policy that ships binlogs off-host before purging :

```ini
[mariadb]
log_bin            = /var/lib/mysql/binlog/mariadb-bin
binlog_format      = ROW
expire_logs_days   = 7
sync_binlog        = 1
```

```bash
# Cron job every 5 minutes : ship completed binlogs to remote
*/5 * * * * mysql -u root -e "FLUSH BINARY LOGS;" \
  && rsync -a --remove-source-files \
       /var/lib/mysql/binlog/mariadb-bin.0* backup-host:/binlog-archive/
```

`expire_logs_days` only purges binlogs older than 7 days on the primary ; the archived copies on `backup-host` are the recovery source. Setting `expire_logs_days` too low without verified off-host copies recreates the problem at a smaller scale.

---

## AP-4 : Restoring an incremental chain in the wrong order

### Cause
Incremental backups are deltas against a specific parent (the previous full or incremental). The prepare phase reconstructs the chain INTO the base directory by applying deltas in chronological order with `--apply-log-only` on all but the final increment. Wrong order, wrong flag, or skipping an increment corrupts the result.

### Symptom
- `mariadb-backup --copy-back` succeeds.
- Server starts but `SELECT` returns rows that match neither the base backup nor any individual increment.
- InnoDB crash recovery logs reference LSN values that do not match the redo log state.

### Why it fails
InnoDB redo apply is order-sensitive : LSN N+1 cannot be applied before LSN N. The `--apply-log-only` flag tells `--prepare` to apply redo but NOT roll back uncommitted transactions ; rolling back too early prevents subsequent increments from applying.

### Fix

```bash
# Correct sequence for full + 3 increments
mariadb-backup --prepare --apply-log-only --target-dir=/backup/full
mariadb-backup --prepare --apply-log-only --target-dir=/backup/full \
  --incremental-dir=/backup/inc-1
mariadb-backup --prepare --apply-log-only --target-dir=/backup/full \
  --incremental-dir=/backup/inc-2
mariadb-backup --prepare --target-dir=/backup/full \
  --incremental-dir=/backup/inc-3      # NOTE : NO --apply-log-only here
```

The final `--prepare` (without `--apply-log-only`) performs the rollback phase. Now `/backup/full` is restore-ready.

### Recovery
If you ran the chain in the wrong order, there is no recovery from the partially-applied directory. Start from the original (unmodified) `full` + increment dirs and run the chain again ; this is why some teams keep an immutable archive copy of the raw backups and prepare in a working clone.

---

## AP-5 : Backup taken on a Galera node mid-SST

### Cause
A Galera node serving as the SST donor to a joining node is in a special state where its redo log and data files are being streamed to the joiner. A `mariadb-backup --backup` initiated against this node captures an unstable snapshot : the donor's state is changing under the backup tool.

### Symptom
- Backup completes without error.
- `--prepare` succeeds.
- Restore appears to succeed.
- Later : InnoDB internal consistency checks (`mariadb-check`) report corruption ; rare ROW events fail to apply on a re-bootstrapped replica.

### Why it fails
SST opens the donor's full datadir for read with crash-recovery semantics. Concurrent backup competes for the same pages and races on flush-list mutations. The backup output reflects an intermediate state that has no consistent recovery point.

### Fix
Before every backup, verify donor state :

```bash
mariadb -u root -p -e "
  SELECT @@wsrep_node_name AS node,
         @@wsrep_local_state_comment AS state;
"
# Expected : state = 'Synced' (NOT 'Donor/Desynced', NOT 'Joiner')
```

If state is anything other than `Synced`, postpone the backup. Even better : dedicate a node with `pc.weight=0` that never serves SST (because the cluster will not vote a 0-weight node as the donor unless no other option exists).

---

## AP-6 : mariadb-dump without --routines --triggers --events

### Cause
Default `mariadb-dump` includes `--triggers=ON` but `--routines=OFF` and `--events=OFF`. Stored procedures, functions, and scheduled events live in `mysql.proc` / `mysql.event` and are silently omitted from the dump.

### Symptom
- Restored database appears complete.
- Application calls a stored procedure : `ERROR 1305 PROCEDURE x does not exist`.
- Scheduled jobs (e.g. nightly aggregation events) silently never run.
- Debugging takes hours because the schema and data are intact.

### Why it fails
Routines and events are stored procedures-as-code ; they are not table data and require explicit inclusion. The defaults were chosen for compatibility with older MySQL pipelines that did not have a stable routine-dump format.

### Fix
ALWAYS use the full safe-flag set :

```bash
mariadb-dump \
  --single-transaction \
  --routines \
  --triggers \
  --events \
  --hex-blob \
  -u backup -p \
  --all-databases > /backup/full.sql
```

For multi-tenant per-database dumps, the same flags apply per database.

---

## AP-7 : Cross-version restore loses user privileges

### Cause
The privilege schema changed in MariaDB 10.4+ : `mysql.user` was replaced by `mysql.global_priv` (a more flexible JSON-document layout). Restoring a `mariadb-dump --all-databases` from 10.3 onto 10.4+ or vice versa with default options drags in the source's privilege tables, which may be the wrong schema for the target.

### Symptom
- Restore "succeeds".
- Server restarts.
- All user logins fail with `Access denied` or `Authentication method unknown`.
- Recovery requires `--skip-grant-tables` boot and manual privilege rebuild.

### Why it fails
The `mysql` schema is version-specific. A dump from 10.3 contains `mysql.user`, but 10.4+ ignores that table and reads from `mysql.global_priv` ; the restored data is in the wrong place and the server has no grants.

### Fix
For cross-version restore, exclude the `mysql` schema from the dump and rebuild grants on the target :

```bash
# On source : dump everything except mysql
mariadb-dump \
  --single-transaction --routines --triggers --events --hex-blob \
  --databases db1 db2 db3 \
  -u backup -p > /backup/data-only.sql

# On source : dump grants in version-portable SQL
mariadb -u root -p -BNe "
  SELECT CONCAT('SHOW CREATE USER ',QUOTE(User),'@',QUOTE(Host),';')
    FROM mysql.user WHERE User NOT IN ('mariadb.sys','root','mysql','PUBLIC')
" | mariadb -u root -p > /backup/users.sql

mariadb -u root -p -BNe "
  SELECT CONCAT('SHOW GRANTS FOR ',QUOTE(User),'@',QUOTE(Host),';')
    FROM mysql.user WHERE User NOT IN ('mariadb.sys','root','mysql','PUBLIC')
" | mariadb -u root -p \
  | grep -E '^GRANT' | sed 's/$/;/' > /backup/grants.sql

# On target : restore data, then users, then grants
mariadb -u root -p < /backup/data-only.sql
mariadb -u root -p < /backup/users.sql
mariadb -u root -p < /backup/grants.sql
FLUSH PRIVILEGES;
```

For physical backups across major versions : do NOT do that. `mariadb-backup` requires the same InnoDB on-disk format on source and target. Use a logical dump for major-version restores.

---

## AP-8 : Storing backups on the same physical host as the database

### Cause
The backup target directory is on the same physical disk (or VM, or rack) as the live `datadir`. A disk failure, ransomware event, filesystem corruption, or accidental `rm -rf /var/lib` takes both the database AND the backups in one stroke.

### Symptom
- Routine backups succeed for months or years.
- Hardware fault destroys the database and the backup volume simultaneously.
- Recovery is impossible from local backups ; only off-host copies (if any) survive.

### Why it fails
A backup is only insurance against the failure modes that do NOT also destroy the backup. Local-disk backups protect against logical corruption (bad UPDATE, dropped table) but NOT physical/storage/security loss.

### Fix
The 3-2-1 rule (industry-standard, applies cleanly to MariaDB) :

- **3** copies of the data (production + backup + off-site).
- **2** distinct media types (e.g. NVMe production + S3-class object storage).
- **1** off-site copy (different physical location, different administrative domain).

```bash
# Local backup
mariadb-backup --backup --target-dir=/backup/local/full-$(date +%F)
mariadb-backup --prepare --target-dir=/backup/local/full-$(date +%F)

# Ship to remote object storage (encrypted)
tar -cf - /backup/local/full-$(date +%F) \
  | gpg --batch --yes --cipher-algo AES256 \
        --passphrase-file /etc/mariadb/backup-passphrase \
        --symmetric \
  | aws s3 cp - s3://company-backups/mariadb/full-$(date +%F).tar.gpg

# Verify retrievable copy actually exists at the remote location
aws s3 ls s3://company-backups/mariadb/full-$(date +%F).tar.gpg \
  || mail -s "REMOTE BACKUP MISSING" ops@example < /dev/null
```

If the off-host copy has not been verified retrievable within the last 7 days, treat the off-host copy as missing.
