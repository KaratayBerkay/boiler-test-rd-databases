# Examples : Backup and Restore

Working command sequences. All target PostgreSQL 15, 16, and 17 unless a `v17+`
annotation says otherwise.

## 1. Custom-format logical backup and restore :

```bash
# back up one database to a single compressed archive
pg_dump -Fc -d mydb -f mydb.dump

# restore into a fresh database
createdb newdb
pg_restore -d newdb mydb.dump
```

`-Fc` archives are not SQL text; `psql -f` will not read them. Use `pg_restore`.

## 2. Parallel directory dump and parallel restore :

```bash
# 8-way parallel dump (directory format is required for --jobs)
pg_dump -Fd -j 8 -d mydb -f mydb_dumpdir

# 8-way parallel restore
pg_restore -j 8 -d newdb mydb_dumpdir
```

A custom (`-Fc`) archive also restores in parallel:

```bash
pg_dump -Fc -d mydb -f mydb.dump      # single-threaded dump
pg_restore -j 8 -d newdb mydb.dump    # parallel restore still works
```

## 3. Maximum compression :

```bash
# gzip level 9
pg_dump -Fc -Z 9 -d mydb -f mydb.dump

# zstd level 6 (v15+)
pg_dump -Fc --compress=zstd:6 -d mydb -f mydb.dump

# lz4, fast (v15+)
pg_dump -Fc --compress=lz4 -d mydb -f mydb.dump
```

## 4. Selective dump and restore :

```bash
# dump only specific tables
pg_dump -Fc -t 'public.orders' -t 'public.customers' -d mydb -f sub.dump

# dump a whole schema, exclude log tables
pg_dump -Fc -n 'sales' -T 'sales.*_log' -d mydb -f sales.dump

# restore just one table out of a full archive
pg_restore -d mydb -t orders mydb.dump
```

## 5. Schema-only and data-only :

```bash
pg_dump -Fc -s -d mydb -f schema.dump          # definitions only
pg_dump -Fc -a -d mydb -f data.dump            # data only
pg_dump -Fc --section=pre-data -d mydb -f pre.dump   # tables before indexes
```

## 6. TOC-file editing for a custom restore order :

```bash
# 1. list the archive contents
pg_restore -l mydb.dump > toc.txt

# 2. edit toc.txt: prefix a line with ';' to skip it, reorder lines as needed
#    ;2; 145344 TABLE species postgres      <-- this table will be skipped

# 3. restore using the edited list
pg_restore -L toc.txt -d newdb mydb.dump
```

## 7. Cluster globals with pg_dumpall :

```bash
# roles + tablespaces + config grants (NOT saved by pg_dump)
pg_dumpall --globals-only -f globals.sql

# full disaster-recovery set: globals + every database
pg_dumpall --globals-only -f globals.sql
for db in app reporting analytics; do
  pg_dump -Fc -d "$db" -f "${db}.dump"
done

# restore order: globals first, then each database
psql -d postgres -f globals.sql
for db in app reporting analytics; do
  createdb "$db"
  pg_restore -d "$db" "${db}.dump"
done
```

## 8. Physical backup with pg_basebackup :

```bash
# whole cluster, plain layout, WAL streamed alongside for consistency
pg_basebackup -D /backup/base -F plain -X stream -P

# compressed tar output
pg_basebackup -D /backup/base -F tar -z -P
```

Server prerequisites: `wal_level = replica` (or higher) and `max_wal_senders >= 2`.

## 9. WAL archiving setup :

```ini
# postgresql.conf  -- wal_level and archive_mode changes need a server restart
wal_level = replica
archive_mode = on
archive_command = 'test ! -f /mnt/archive/%f && cp %p /mnt/archive/%f'
```

```bash
# apply: wal_level + archive_mode require a full restart
pg_ctl -D /var/lib/postgresql/data restart
```

## 10. Point-In-Time Recovery :

```bash
# 1. stop the server, move the broken data dir aside
pg_ctl -D /var/lib/postgresql/data stop
mv /var/lib/postgresql/data /var/lib/postgresql/data.broken

# 2. restore the base backup into place
mkdir -p /var/lib/postgresql/data
tar -xf /backup/base/base.tar -C /var/lib/postgresql/data
```

```ini
# 3. postgresql.conf in the restored data directory
restore_command = 'cp /mnt/archive/%f %p'
recovery_target_time = '2026-05-20 14:25:00+02'
recovery_target_action = 'promote'
```

```bash
# 4. the empty recovery.signal file triggers archive-recovery mode
touch /var/lib/postgresql/data/recovery.signal

# 5. start; server replays WAL to the target time, then promotes
pg_ctl -D /var/lib/postgresql/data start
```

The target time must be later than the base backup's end. Without `recovery.signal`
the server starts normally and ignores the recovery settings.

## 11. v17 incremental backup (v17+) :

```ini
# postgresql.conf on the primary
summarize_wal = on
wal_summary_keep_time = '7d'
```

```bash
# Sunday: full baseline
pg_basebackup -D /bk/full -X stream

# Monday: incremental vs the full backup's manifest
pg_basebackup --incremental=/bk/full/backup_manifest -D /bk/incr1 -X stream

# Tuesday: incremental vs Monday's manifest
pg_basebackup --incremental=/bk/incr1/backup_manifest -D /bk/incr2 -X stream

# restore Tuesday's state: combine oldest -> newest into one full backup
pg_combinebackup /bk/full /bk/incr1 /bk/incr2 -o /var/lib/postgresql/data
```

## 12. Verify a backup :

```bash
# check files, sizes, and checksums against the manifest
pg_verifybackup /backup/base

# also verify the output of pg_combinebackup
pg_verifybackup /var/lib/postgresql/data
```
