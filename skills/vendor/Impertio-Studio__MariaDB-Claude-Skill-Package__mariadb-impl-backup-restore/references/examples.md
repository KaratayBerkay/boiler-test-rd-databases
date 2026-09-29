# MariaDB Backup and Restore : Working Examples

Ten end-to-end examples covering full and incremental physical backups, logical dumps, PITR, single-table restore, encrypted streaming, replica bootstrap, partial backups, Galera-aware backups, and the monthly verification routine. Every command verified against the cited KB / docs page.

---

## Example 1 : Full logical backup with mariadb-dump

```bash
# 10.5+ : production-safe logical full backup, compressed and binlog-coordinated
mariadb-dump \
  --single-transaction \
  --quick \
  --routines \
  --triggers \
  --events \
  --hex-blob \
  --source-data=2 \
  --flush-logs \
  --all-databases \
  --default-character-set=utf8mb4 \
  --user=backup_user \
  --password='<secret>' \
  | zstd -T0 -19 > /backup/$(date +%F)-full.sql.zst

# Restore : decompress and pipe to mariadb client
zstd -d -c /backup/2026-05-19-full.sql.zst | mariadb -u root -p
```

- `--single-transaction` : InnoDB consistent snapshot, no global lock.
- `--source-data=2` : writes the binlog coordinate as a comment for PITR/replica bootstrapping.
- `--flush-logs` : rotates the binary log so post-dump events are in a fresh file.
- `zstd -T0 -19` : multithreaded high-ratio compression ; typical 6-10x on SQL text.

---

## Example 2 : Per-database logical backup

```bash
# Useful for restoring one tenant's database without touching others
for DB in tenant_acme tenant_globex tenant_initech ; do
  mariadb-dump \
    --single-transaction --quick --routines --triggers --events --hex-blob \
    --user=backup_user --password='<secret>' \
    "$DB" \
    | zstd -T0 > "/backup/$(date +%F)-${DB}.sql.zst"
done

# Restore one tenant
zstd -d -c /backup/2026-05-19-tenant_acme.sql.zst \
  | mariadb -u root -p tenant_acme
```

The target database must exist before restore ; add `CREATE DATABASE tenant_acme;` first or use `--databases tenant_acme` (which includes the `CREATE DATABASE` statement).

---

## Example 3 : Physical full backup with mariadb-backup

```bash
# Backup
mariadb-backup --backup \
  --target-dir=/backup/full-$(date +%F) \
  --user=mariabackup_user --password='<secret>' \
  --parallel=4 \
  --slave-info \
  --galera-info

# Prepare (MANDATORY ; applies pending redo)
mariadb-backup --prepare \
  --target-dir=/backup/full-$(date +%F)

# Restore : full cycle
systemctl stop mariadb
mv /var/lib/mysql /var/lib/mysql.broken
mkdir -m 750 /var/lib/mysql
mariadb-backup --copy-back \
  --target-dir=/backup/full-$(date +%F)
chown -R mysql:mysql /var/lib/mysql/
systemctl start mariadb

# Verify
mariadb -u root -p -e "SHOW DATABASES; SELECT VERSION();"
```

`/var/lib/mysql` MUST be empty or not exist at `--copy-back` time. Moving the old datadir aside (rather than `rm -rf`) preserves a fallback while you confirm the restore succeeded.

---

## Example 4 : Incremental backup chain (weekly cycle)

```bash
# Sunday : full
mariadb-backup --backup \
  --target-dir=/backup/2026-W20/full \
  --user=mariabackup_user --password='<secret>'

# Monday-Saturday : incremental, each chained to the previous
PREV=/backup/2026-W20/full
for DAY in mon tue wed thu fri sat ; do
  THIS=/backup/2026-W20/inc-${DAY}
  mariadb-backup --backup \
    --target-dir="$THIS" \
    --incremental-basedir="$PREV" \
    --user=mariabackup_user --password='<secret>'
  PREV="$THIS"
done
```

Restore prepare phase :

```bash
# Base : --apply-log-only (defer rollback)
mariadb-backup --prepare --apply-log-only \
  --target-dir=/backup/2026-W20/full

# Each intermediate increment : --apply-log-only
for DAY in mon tue wed thu fri ; do
  mariadb-backup --prepare --apply-log-only \
    --target-dir=/backup/2026-W20/full \
    --incremental-dir=/backup/2026-W20/inc-${DAY}
done

# FINAL increment : NO --apply-log-only (now we roll back uncommitted)
mariadb-backup --prepare \
  --target-dir=/backup/2026-W20/full \
  --incremental-dir=/backup/2026-W20/inc-sat

# Then copy-back the assembled `full` directory as in Example 3
```

Chain rebuild lands INTO the `full` directory. Order matters ; applying `inc-tue` before `inc-mon` corrupts the result.

---

## Example 5 : Point-in-time recovery

```bash
# Incident : someone ran `DROP TABLE orders` at 14:32 today.
# Most-recent full backup completed at 02:00 with --slave-info.

# Step 1 : restore the full + relevant incrementals into a SANDBOX server
systemctl stop mariadb@sandbox
rm -rf /var/lib/mysql-sandbox/*
mariadb-backup --copy-back \
  --target-dir=/backup/2026-05-19/full \
  --datadir=/var/lib/mysql-sandbox
chown -R mysql:mysql /var/lib/mysql-sandbox/
systemctl start mariadb@sandbox

# Step 2 : extract backup binlog coordinate
cat /backup/2026-05-19/full/xtrabackup_binlog_info
# Example output : mariadb-bin.000123  4523  0-1-9876

# Step 3 : replay binlogs from that coordinate to 14:31:59
mariadb-binlog \
  --start-position=4523 \
  --stop-datetime='2026-05-19 14:31:59' \
  /var/lib/mysql/binlog/mariadb-bin.000123 \
  /var/lib/mysql/binlog/mariadb-bin.000124 \
  /var/lib/mysql/binlog/mariadb-bin.000125 \
  | mariadb \
      --socket=/var/run/mysqld/sandbox.sock \
      -u root -p \
      --binary-mode

# Step 4 : extract the recovered table and import to production
mariadb-dump \
  --socket=/var/run/mysqld/sandbox.sock \
  -u root -p production_db orders \
  > /tmp/orders-recovered.sql
mariadb -u root -p production_db < /tmp/orders-recovered.sql
```

`--stop-datetime` stops BEFORE the first event with that timestamp ; pick a value strictly before the destructive event. For exact-event stop, identify the position via `mariadb-binlog --verbose mariadb-bin.000125 | less` and use `--stop-position=<n>`.

---

## Example 6 : Single-table restore via transportable tablespace

```bash
# Prerequisite : a prepared backup with --export
mariadb-backup --prepare --export \
  --target-dir=/backup/2026-05-19/full

# 1. Recreate the empty target table with IDENTICAL DDL
mariadb -u root -p production_db <<'SQL'
CREATE TABLE orders_restore (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  customer_id BIGINT UNSIGNED NOT NULL,
  amount      DECIMAL(12,2)   NOT NULL,
  status      ENUM('draft','sent','paid','void') NOT NULL DEFAULT 'draft',
  created_at  TIMESTAMP(6)    NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY ix_customer (customer_id),
  KEY ix_status_created (status, created_at)
) ENGINE=InnoDB
  ROW_FORMAT=DYNAMIC
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_uca1400_ai_ci;
ALTER TABLE orders_restore DISCARD TABLESPACE;
SQL

# 2. Copy the tablespace + cfg files
cp /backup/2026-05-19/full/production_db/orders.ibd \
   /var/lib/mysql/production_db/orders_restore.ibd
cp /backup/2026-05-19/full/production_db/orders.cfg \
   /var/lib/mysql/production_db/orders_restore.cfg
chown mysql:mysql /var/lib/mysql/production_db/orders_restore.{ibd,cfg}

# 3. Import
mariadb -u root -p production_db <<'SQL'
ALTER TABLE orders_restore IMPORT TABLESPACE;
SELECT COUNT(*) FROM orders_restore;
SQL
```

If the schema does not match exactly, IMPORT fails with `ERROR 1808 Schema mismatch`. Always run `SHOW CREATE TABLE orders\G` on the source first, then copy that DDL onto the target verbatim (renaming the table).

---

## Example 7 : Encrypted streaming backup with gpg

```bash
# Stream xbstream output to gpg ; never write plaintext to disk
mariadb-backup --backup \
  --stream=xbstream \
  --target-dir=/tmp \
  --user=mariabackup_user --password='<secret>' \
  | gpg --batch --yes --cipher-algo AES256 \
        --passphrase-file /etc/mariadb/backup-passphrase \
        --symmetric \
        --output /backup/full-$(date +%F).xbstream.gpg

# Restore : decrypt, extract via mbstream, prepare, copy-back
mkdir -p /restore/full-2026-05-19
gpg --batch --decrypt \
    --passphrase-file /etc/mariadb/backup-passphrase \
    /backup/full-2026-05-19.xbstream.gpg \
  | mbstream -x -C /restore/full-2026-05-19

mariadb-backup --prepare --target-dir=/restore/full-2026-05-19

systemctl stop mariadb
rm -rf /var/lib/mysql/*
mariadb-backup --copy-back --target-dir=/restore/full-2026-05-19
chown -R mysql:mysql /var/lib/mysql/
systemctl start mariadb
```

Replace `gpg` with `openssl enc -aes-256-cbc -pbkdf2 -salt -pass file:...` if `gpg` is unavailable. The deprecated built-in `--compress` (QuickLZ) MUST NOT be used.

---

## Example 8 : Partial backup of a single database

```bash
# Backup only the tenant_acme database
mariadb-backup --backup \
  --target-dir=/backup/tenant_acme-$(date +%F) \
  --databases="tenant_acme mysql" \
  --user=mariabackup_user --password='<secret>'

# Prepare with --export so we can selectively import tables later
mariadb-backup --prepare --export \
  --target-dir=/backup/tenant_acme-$(date +%F)
```

Note : the `mysql` schema is included so user privileges are part of the backup. If you exclude `mysql`, the restored tenant database will be orphaned without its grants.

---

## Example 9 : Bootstrap a replica from a mariadb-backup snapshot

```bash
# On the PRIMARY : take a backup with --slave-info
mariadb-backup --backup \
  --target-dir=/backup/replica-bootstrap-$(date +%F) \
  --slave-info \
  --user=mariabackup_user --password='<secret>'

mariadb-backup --prepare \
  --target-dir=/backup/replica-bootstrap-$(date +%F)

# Ship the backup to the new replica
rsync -a /backup/replica-bootstrap-$(date +%F)/ \
  replica-1.internal:/backup/bootstrap/

# On the REPLICA : copy-back and bootstrap
systemctl stop mariadb
rm -rf /var/lib/mysql/*
mariadb-backup --copy-back --target-dir=/backup/bootstrap
chown -R mysql:mysql /var/lib/mysql/
systemctl start mariadb

# Read the binlog coordinate that --slave-info recorded
cat /var/lib/mysql/xtrabackup_slave_info
# Example : CHANGE MASTER TO MASTER_LOG_FILE='mariadb-bin.000123', MASTER_LOG_POS=4523;

# Apply on the replica
mariadb -u root -p <<'SQL'
CHANGE MASTER TO
  MASTER_HOST='primary.internal',
  MASTER_USER='repl_user',
  MASTER_PASSWORD='<repl-secret>',
  MASTER_LOG_FILE='mariadb-bin.000123',
  MASTER_LOG_POS=4523;
START SLAVE;
SHOW SLAVE STATUS\G
SQL
```

`xtrabackup_slave_info` carries the exact coordinate ; do not guess. For GTID-based replication on MariaDB use the `xtrabackup_info` file's `slave_info`-equivalent fields (per `mariadb-impl-replication-setup`).

---

## Example 10 : Monthly verification routine

```bash
#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR=/backup/full-$(date +%F)
SANDBOX_DIR=/var/lib/mysql-verify
SANDBOX_SOCK=/var/run/mysqld/verify.sock
SANDBOX_PORT=3307
LOG=/var/log/backup-verify-$(date +%F).log

systemctl stop mariadb@verify
rm -rf "$SANDBOX_DIR"/*
mariadb-backup --copy-back --target-dir="$BACKUP_DIR" --datadir="$SANDBOX_DIR"
chown -R mysql:mysql "$SANDBOX_DIR"
systemctl start mariadb@verify

# Wait for socket
for i in $(seq 1 30) ; do
  [ -S "$SANDBOX_SOCK" ] && break
  sleep 1
done

# Structural integrity
mariadb-check \
  --socket="$SANDBOX_SOCK" \
  -u root -p"$ROOT_PWD" \
  --all-databases >> "$LOG"

# Row-count sanity vs production canonical table
EXPECTED=$(mariadb -u root -p"$ROOT_PWD" \
  -se "SELECT COUNT(*) FROM production_db.orders")
RESTORED=$(mariadb --socket="$SANDBOX_SOCK" -u root -p"$ROOT_PWD" \
  -se "SELECT COUNT(*) FROM production_db.orders")

echo "expected=$EXPECTED restored=$RESTORED" >> "$LOG"
[ "$EXPECTED" = "$RESTORED" ] || {
  echo "VERIFICATION FAILED" | mail -s "Backup verification FAILED" ops@example
  exit 1
}
```

A backup that has never been restored to a working server is a hope, not a backup. Schedule this script monthly. If it has not run successfully in the last 60 days, treat the backup chain as untrusted.

---

## Example 11 : Galera dedicated-backup-node configuration

```ini
# my.cnf on the dedicated backup node (one of the 3+ Galera members)
[mariadb]
wsrep_on                       = ON
wsrep_provider                 = /usr/lib/galera/libgalera_smm.so
wsrep_cluster_address          = gcomm://node-1.internal,node-2.internal,backup-1.internal
wsrep_cluster_name             = production_cluster
wsrep_node_name                = backup-1
wsrep_node_address             = backup-1.internal
wsrep_sst_method               = mariabackup
wsrep_sst_auth                 = sstuser:<sst-secret>
binlog_format                  = ROW
default_storage_engine         = InnoDB
innodb_autoinc_lock_mode       = 2
wsrep_provider_options         = "pc.weight=0"
```

Backup script :

```bash
# Pre-backup : de-sync from cluster flow control
mariadb -u root -p -e "SET GLOBAL wsrep_desync = ON;"

mariadb-backup --backup \
  --target-dir=/backup/galera-full-$(date +%F) \
  --galera-info \
  --user=mariabackup_user --password='<secret>'

# Post-backup : re-sync
mariadb -u root -p -e "SET GLOBAL wsrep_desync = OFF;"
```

`pc.weight=0` keeps the backup node from being voted Primary Component holder during a network split, so a restored backup is never the only surviving partition by accident. `wsrep_desync=ON` exempts the node from cluster flow-control for the duration of the backup, so the rest of the cluster runs at full speed while this node consumes IO.
