---
name: postgres-impl-backup-restore
description: >
  Use when setting up backups, choosing logical vs physical, doing point-in-time recovery, or configuring v17 incremental backup.
  Prevents slow restore from plain-format dumps (no parallel), missing roles after restore (forgot pg_dumpall globals), and being unable to recover to a precise time without WAL archiving.
  Covers logical vs physical decision, pg_dump formats (plain / custom / directory / tar) and --jobs parallelism, pg_restore, pg_dumpall globals, pg_basebackup, PITR with WAL archiving, v17 incremental backup (summarize_wal + pg_combinebackup).
  Keywords: pg_dump, pg_restore, pg_basebackup, pg_dumpall, backup, restore, PITR, point in time recovery, WAL archiving, incremental backup, pg_combinebackup, custom format, parallel restore, how to back up postgres, restore is slow, recover to a specific time, lost my roles after restore
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17 (incremental backup is v17+)."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-backup-restore

## Quick Reference :

PostgreSQL has two backup families and you almost always need both.

- **Logical** (`pg_dump`, `pg_dumpall`) : a SQL-or-archive snapshot of one database.
  Portable across major versions and CPU architectures, restorable per-table, but slow
  to restore for large databases and consistent only as of backup start.
- **Physical** (`pg_basebackup` + archived WAL) : a binary copy of the whole cluster.
  Fast to restore, enables Point-In-Time Recovery to any second within retention, but
  pinned to the same major version.

Real-world rule : run physical backups + WAL archiving for disaster recovery and PITR,
AND periodic logical dumps for cross-version migration and accidental-drop recovery.

```
what do you need?
  ├─ migrate to a new major version / new arch  → logical  (pg_dump)
  ├─ restore one dropped table                  → logical  (custom or directory)
  ├─ fast full-cluster restore after disk loss  → physical (pg_basebackup + WAL)
  ├─ recover to "just before the bad UPDATE"    → physical + PITR (WAL archiving)
  └─ daily backup of a huge DB, small windows   → physical, v17 incremental backup
```

The single most damaging mistake : `pg_dump` in plain format (`-Fp`) as the only
backup. Plain format cannot be restored in parallel and cannot restore selectively, so
a large-database restore becomes hours of single-threaded downtime. ALWAYS dump in
custom (`-Fc`) or directory (`-Fd`) format.

## When To Use This Skill :

ALWAYS use this skill when :
- Designing a backup strategy or choosing logical vs physical
- Running `pg_dump`, `pg_restore`, `pg_basebackup`, `pg_dumpall`, or `pg_combinebackup`
- Setting up WAL archiving or performing Point-In-Time Recovery
- Configuring v17 incremental backup (`summarize_wal`, `--incremental`)
- A restore is slow, or roles / tablespaces are missing after a restore

NEVER use this skill for :
- Setting up a streaming-replica standby server (use postgres-impl-streaming-replication)
- Logical replication publications and subscriptions (use postgres-impl-logical-replication)
- VACUUM, bloat, or transaction-id wraparound (use postgres-impl-vacuum-bloat-toolkit)
- Connection or authentication failures (use postgres-errors-connection-auth)

## Decision Trees :

### Logical vs physical :

```
need cross-major-version or cross-architecture portability?
  └─ YES → logical (pg_dump). physical backups are version-locked.

need to restore a single table / schema, not the whole cluster?
  └─ YES → logical, custom or directory format (supports selective restore).

need fast restore of a multi-hundred-GB cluster?
  └─ YES → physical (pg_basebackup). logical restore replays SQL, far slower.

need to recover to an exact point in time (before a bad change)?
  └─ YES → physical + WAL archiving + PITR. logical only restores backup-start state.

answer is "more than one of the above" → do BOTH. this is the normal answer.
```

### pg_dump format choice :

```
-Fp  plain      → human-readable SQL. NO parallel, NO selective restore.
                  Use only for tiny DBs or when you must read/edit the SQL.
-Fc  custom     → compressed single-file archive. Parallel RESTORE, selective
                  restore, TOC reordering. Default choice for one-file backups.
-Fd  directory  → one file per table + TOC. Parallel DUMP (--jobs) AND parallel
                  restore. Default choice when dump speed matters.
-Ft  tar        → tar archive, no compression. Legacy. Avoid for new work.

rule: NEVER -Fp as the primary backup. Use -Fc, or -Fd when you need --jobs.
```

### Is this cluster PITR-ready :

```
wal_level >= replica ?            ─ no → set it, restart. PITR impossible without it.
archive_mode = on ?               ─ no → set it, restart.
archive_command (or _library) ?   ─ no → set it. WAL is not being saved → no PITR.
a recent base backup exists ?     ─ no → take pg_basebackup now.
all four YES → PITR is possible to any time after the base backup's end.
```

### v17 incremental backup chain :

```
on primary (v17+): summarize_wal = on   (+ wal_summary_keep_time)
  │
  1. full baseline   : pg_basebackup -D /bk/full
  2. incremental N    : pg_basebackup --incremental=/bk/<prev>/backup_manifest -D /bk/incrN
  │
to restore: pg_combinebackup /bk/full /bk/incr1 ... /bk/incrN -o /restore
            (pass oldest → newest; incrementals are USELESS standalone)
```

## Patterns :

### Pattern 1 : Logical backup in custom format

ALWAYS dump with `-Fc` (custom) or `-Fd` (directory), never `-Fp`, for any database you
might need to restore quickly or selectively.
NEVER rely on a plain SQL dump as the only backup of a non-trivial database.

```bash
# custom format: single compressed archive, parallel-restorable
pg_dump -Fc -d mydb -f mydb.dump

# restore (custom/directory archives need pg_restore, not psql)
pg_restore -d newdb mydb.dump
```

WHY : custom and directory archives carry a table of contents, so `pg_restore` can load
data and build indexes in parallel and can restore a subset. A plain dump is a flat SQL
script replayed serially by `psql` with no reordering and no parallelism.

### Pattern 2 : Parallel directory dump

ALWAYS use directory format with `--jobs=N` when dump time matters; it is the only
format that dumps tables concurrently.
NEVER pass `--jobs` to a plain, custom, or tar dump; only directory format accepts it.

```bash
# 8-way parallel dump into a directory
pg_dump -Fd -j 8 -d mydb -f mydb_dumpdir
```

WHY : directory format writes one file per table, so N worker processes write
simultaneously. The leader takes ACCESS SHARE locks on every target object up front so
nothing is dropped mid-dump. Custom format is a single file and cannot be written by
multiple processes, so it has no parallel dump.

### Pattern 3 : Parallel restore

ALWAYS pass `--jobs=N` to `pg_restore` when restoring a custom or directory archive
into a large database.
NEVER expect `--jobs` to help a plain SQL dump; that path goes through `psql` serially.

```bash
pg_restore -j 8 -d newdb mydb.dump
```

WHY : `pg_restore -j` loads table data and then builds indexes and constraints
concurrently across N connections. Index builds dominate large restores, so parallelism
is the biggest single restore-time win. Custom AND directory formats both support it.

### Pattern 4 : pg_dumpall for cluster globals

ALWAYS run `pg_dumpall --globals-only` alongside per-database `pg_dump`. `pg_dump` does
NOT save roles, tablespaces, or configuration-parameter grants.
NEVER assume a per-database dump is a complete cluster backup.

```bash
# cluster-wide globals: roles, tablespaces, config grants
pg_dumpall --globals-only -f globals.sql

# restore globals first, then each database
psql -d postgres -f globals.sql
```

WHY : roles and tablespaces live at the cluster level, outside any single database.
A restore from `pg_dump` alone produces a database whose owning roles do not exist, so
ownership and `GRANT`s fail. Restore `globals.sql` before the database dumps.

### Pattern 5 : Physical backup with pg_basebackup

ALWAYS use `pg_basebackup` for a fast, whole-cluster physical backup and the base of
PITR.
NEVER copy the data directory of a running server by hand; use `pg_basebackup`, which
brackets the copy with `pg_backup_start` / `pg_backup_stop`.

```bash
# whole cluster, stream WAL alongside so the backup is self-consistent
pg_basebackup -D /backup/base -F plain -X stream -P
```

WHY : `-X stream` opens a second connection and streams the WAL generated during the
copy, so the backup is internally consistent without needing the archive. `-F plain`
writes a ready-to-start data directory; `-F tar` writes tar files. Requires
`max_wal_senders >= 2` on the server.

### Pattern 6 : WAL archiving setup (the prerequisite for PITR)

ALWAYS configure WAL archiving before you need PITR; a base backup alone only restores
the backup-start state.
NEVER use an `archive_command` that overwrites an existing file or reports success
without verifying the copy.

```ini
# postgresql.conf  (wal_level / archive_mode changes require a restart)
wal_level = replica
archive_mode = on
archive_command = 'test ! -f /mnt/archive/%f && cp %p /mnt/archive/%f'
```

WHY : `%p` is the WAL file path, `%f` its name. The `test ! -f` guard refuses to
clobber an already-archived segment, and `cp` must return zero only on a real success.
Without archived WAL there is nothing to replay past the base backup, so no PITR.

### Pattern 7 : Point-In-Time Recovery

ALWAYS recover to a target time AFTER the base backup's end; you cannot recover into
the window while the base backup was still running.
NEVER start the server before creating the `recovery.signal` file; without it the
server starts normally and ignores the recovery settings.

```ini
# postgresql.conf in the restored data directory
restore_command = 'cp /mnt/archive/%f %p'
recovery_target_time = '2026-05-20 14:25:00+02'
recovery_target_action = 'promote'
```

```bash
# in the restored data directory: this empty file triggers recovery mode
touch /restore/data/recovery.signal
pg_ctl -D /restore/data start
```

WHY : on startup the server sees `recovery.signal`, replays archived WAL via
`restore_command` until `recovery_target_time`, then performs
`recovery_target_action` and removes the signal file. See references/methods.md for the
full restore sequence.

### Pattern 8 : v17 incremental backup

ALWAYS keep the full chain (full backup + every incremental) and combine with
`pg_combinebackup` before restore; an incremental backup is unusable on its own.
NEVER pass incremental directories to `pg_combinebackup` out of order.

```ini
# postgresql.conf on the primary, v17+
summarize_wal = on
wal_summary_keep_time = '7d'
```

```bash
# v17+ : baseline, then incrementals referencing the previous manifest
pg_basebackup -D /bk/full
pg_basebackup --incremental=/bk/full/backup_manifest   -D /bk/incr1
pg_basebackup --incremental=/bk/incr1/backup_manifest  -D /bk/incr2

# reconstruct a full backup: oldest first, newest last
pg_combinebackup /bk/full /bk/incr1 /bk/incr2 -o /restore/data
```

WHY : with `summarize_wal = on` the server tracks which blocks changed, so
`--incremental` ships only modified blocks. `pg_combinebackup` merges the chain into a
synthetic full backup. Each incremental references its predecessor's
`backup_manifest`; a missing link breaks the chain. v17+ only.

## Anti-Patterns :

(Cause, symptom, and fix for each in references/anti-patterns.md)

- AP-1 : Plain-format (`-Fp`) dump as the only backup of a large DB. Restore is serial.
- AP-2 : Passing `--jobs` to a non-directory `pg_dump`. Only `-Fd` dumps in parallel.
- AP-3 : Forgetting `pg_dumpall --globals-only`. Roles and tablespaces missing on restore.
- AP-4 : Restoring a physical backup onto a different major version. Binary-incompatible.
- AP-5 : No WAL archiving. Only restore to the backup moment, no PITR.
- AP-6 : Hand-copying a running data directory instead of `pg_basebackup`. Torn pages.
- AP-7 : Treating a v17 incremental backup as restorable standalone. Needs pg_combinebackup.
- AP-8 : `archive_command` that overwrites or fakes success. Silent WAL loss.

## Reference Links :

- [references/methods.md](references/methods.md) : Full option tables for pg_dump, pg_restore, pg_basebackup, pg_dumpall, pg_combinebackup, and recovery parameters
- [references/examples.md](references/examples.md) : Working backup and restore command sequences, version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, and fix-pattern

## See Also :

- postgres-impl-streaming-replication : `pg_basebackup -R` to seed a standby, WAL senders
- postgres-impl-logical-replication : per-table replication, an alternative to dump-based migration
- postgres-impl-vacuum-bloat-toolkit : keep the cluster healthy so backups stay small
- postgres-core-configuration : `wal_level`, `max_wal_senders`, `archive_mode` placement
- Vooronderzoek section : §17 Backup + Restore
- Official docs : https://www.postgresql.org/docs/17/backup.html
