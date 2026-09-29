# Anti-Patterns : Backup and Restore

Each entry : cause, symptom, and fix-pattern. Backup tools are CLI programs, so most
failures surface as long downtime or missing objects rather than a SQLSTATE error.

## AP-1 : Plain-format dump as the only backup of a large database

CAUSE : `pg_dump -Fp` (plain) chosen for readability, then kept as the sole backup.

SYMPTOM : the restore is a single `psql -f` replaying SQL serially. A multi-hundred-GB
database takes hours, all of it downtime, because data load and index builds cannot run
in parallel and cannot be reordered.

FIX : ALWAYS back up in custom (`-Fc`) or directory (`-Fd`) format. Both carry a table
of contents that lets `pg_restore -j N` load data and build indexes concurrently. Keep
plain format only for tiny databases or when the SQL text must be read or edited.

## AP-2 : Passing --jobs to a non-directory pg_dump

CAUSE : adding `-j 8` to a plain, custom, or tar `pg_dump`, expecting a parallel dump.

SYMPTOM : `pg_dump` errors out, or the option is rejected. Only directory format can be
written by multiple worker processes at once.

FIX : use `-Fd` when you want a parallel dump : `pg_dump -Fd -j 8 -d mydb -f dir`.
Note the asymmetry : `pg_restore -j` works for BOTH custom and directory archives, but
`pg_dump -j` works ONLY for directory format.

## AP-3 : Forgetting pg_dumpall globals

CAUSE : backing up each database with `pg_dump` and assuming that covers the cluster.

SYMPTOM : after restore, owning roles do not exist, so `ALTER TABLE ... OWNER TO` and
`GRANT` statements fail; tablespaces referenced by the dump are missing. The restored
database is unusable or has wrong ownership and permissions.

FIX : `pg_dump` saves only the contents of one database. Roles, tablespaces, and
configuration-parameter grants are cluster-global. ALWAYS also run
`pg_dumpall --globals-only -f globals.sql`, and restore `globals.sql` with `psql`
BEFORE restoring any per-database dump.

## AP-4 : Restoring a physical backup onto a different major version

CAUSE : taking a `pg_basebackup` from a v16 server and trying to start it under v17
(or vice versa).

SYMPTOM : the server refuses to start; the data directory's `PG_VERSION` and on-disk
catalog layout do not match the running binary. Physical backups are byte-level copies
tied to one major version and platform.

FIX : physical backups restore only onto the SAME major version. To move data across
major versions use a LOGICAL backup (`pg_dump` / `pg_dumpall`), which emits
version-portable SQL, or use `pg_upgrade` for an in-place major upgrade.

## AP-5 : No WAL archiving, so no point-in-time recovery

CAUSE : taking `pg_basebackup` images but never configuring `archive_mode` and
`archive_command`.

SYMPTOM : after data corruption at 14:30, the newest backup is from 02:00. Recovery can
only return the cluster to the 02:00 state; everything between 02:00 and 14:30 is lost,
including the chance to stop just before the bad change.

FIX : configure WAL archiving (`wal_level = replica`, `archive_mode = on`,
`archive_command`) so every WAL segment is saved. With archived WAL plus a base backup,
PITR can recover to any second after the base backup's end via `recovery_target_time`.

## AP-6 : Hand-copying a running data directory

CAUSE : using `cp -r` or `rsync` on the data directory of a live server instead of
`pg_basebackup`.

SYMPTOM : the copy contains torn pages and an inconsistent mix of files written at
different instants. The restored cluster fails to start or corrupts on first access.

FIX : use `pg_basebackup`, which brackets the copy with `pg_backup_start` /
`pg_backup_stop` and (with `-X stream`) ships the concurrent WAL so the image is
crash-consistent. A raw filesystem copy is only safe on a cleanly stopped server.

## AP-7 : Treating a v17 incremental backup as restorable on its own

CAUSE : pointing a restore at an incremental backup directory produced by
`pg_basebackup --incremental`.

SYMPTOM : the incremental directory is not a usable cluster; it contains only the
blocks that changed since the reference backup. Starting a server on it fails.

FIX : an incremental backup is meaningful only with its full backup and every
incremental between them. Run `pg_combinebackup full incr1 ... incrN -o /restore`,
passing directories oldest to newest, to build a synthetic full backup, then restore
that. Keep the entire chain; a missing link is unrecoverable.

## AP-8 : An archive_command that overwrites or fakes success

CAUSE : `archive_command = 'cp %p /mnt/archive/%f'` with no overwrite guard, or a
command that returns exit 0 even when the copy failed.

SYMPTOM : a WAL segment is silently overwritten or never actually stored, yet
PostgreSQL marks it archived and recycles it. The gap is invisible until a PITR attempt
fails partway through replay.

FIX : the `archive_command` must refuse to overwrite an existing file and must exit
non-zero on any failure, for example
`test ! -f /mnt/archive/%f && cp %p /mnt/archive/%f`. PostgreSQL retries a segment
until the command reports success, so a correct command guarantees the segment is
stored exactly once. Verify the archive periodically.
