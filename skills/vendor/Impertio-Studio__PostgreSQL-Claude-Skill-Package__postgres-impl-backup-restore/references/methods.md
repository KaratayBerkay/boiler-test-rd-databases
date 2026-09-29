# Methods : Backup and Restore

Option tables for the backup and restore tools. All verified against PostgreSQL 17
official documentation. Version annotations mark features added after v15.

## pg_dump :

Logical backup of one database. Online, takes only ACCESS SHARE locks (no blocking of
normal reads/writes).

### Format and parallelism :

| Option | Values | Notes |
|--------|--------|-------|
| `-F`, `--format` | `p` plain, `c` custom, `d` directory, `t` tar | Plain is default. |
| `-j`, `--jobs=N` | integer | Parallel dump. **Directory format only.** |
| `-Z`, `--compress` | `level` or `method[:detail]` | `method` = `gzip`, `lz4`, `zstd`, `none`. `lz4` / `zstd` added v15. Tar format cannot compress. |
| `-f`, `--file` | path | Output file, or directory for `-Fd`. |

### Object selection :

| Option | Effect |
|--------|--------|
| `-s`, `--schema-only` | Definitions only, no data. |
| `-a`, `--data-only` | Data only, no definitions. |
| `-t`, `--table=PATTERN` | Dump only matching tables. Repeatable. |
| `-T`, `--exclude-table=PATTERN` | Skip matching tables. Repeatable. |
| `-n`, `--schema=PATTERN` | Dump only matching schemas. Repeatable. |
| `-N`, `--exclude-schema=PATTERN` | Skip matching schemas. |
| `--section` | `pre-data`, `data`, or `post-data`. |
| `--filter=FILE` | Read include/exclude patterns from a file (`-` = stdin). **v17+.** |

Section order for a parallel-loaded restore is `pre-data` → `data` → `post-data`, so
indexes and constraints (`post-data`) are built after the bulk data load.

## pg_restore :

Restores a custom, directory, or tar archive. Cannot read a plain SQL dump (that needs
`psql`).

| Option | Effect |
|--------|--------|
| `-d`, `--dbname` | Connect and restore directly into this database. |
| `-j`, `--jobs=N` | Parallel restore. Works for **custom and directory** archives. |
| `-C`, `--create` | Create the target database before restoring. |
| `-c`, `--clean` | `DROP` objects before recreating them. |
| `-1`, `--single-transaction` | Wrap the whole restore in one transaction. |
| `-t`, `--table` | Restore only the named table(s). |
| `-n`, `--schema` | Restore only objects in the named schema. |
| `--section` | Restore only `pre-data` / `data` / `post-data`. |
| `-l`, `--list` | Print the archive table of contents. |
| `-L`, `--use-list=FILE` | Restore only the items listed in FILE, in file order. |

TOC-edit workflow : `pg_restore -l a.dump > toc` , comment out lines with a leading `;`
or reorder them, then `pg_restore -L toc -d newdb a.dump`.

## pg_dumpall :

Dumps the whole cluster, including objects `pg_dump` cannot see : roles, tablespaces,
and privilege grants on configuration parameters. Output is always a plain SQL script,
restored with `psql`.

| Option | Effect |
|--------|--------|
| `-g`, `--globals-only` | Roles and tablespaces only, no database contents. |
| `-r`, `--roles-only` | Roles only. |
| `-t`, `--tablespaces-only` | Tablespaces only. |
| `--no-role-passwords` | Omit role passwords from the dump. |
| `-f`, `--file` | Output file. |
| `-c`, `--clean` | Emit `DROP` statements; restore must connect to `postgres` first. |

## pg_basebackup :

Physical (binary) backup of the entire cluster; the base for PITR and for seeding
standbys.

| Option | Values | Notes |
|--------|--------|-------|
| `-D`, `--pgdata` | directory | Required. Backup destination. |
| `-F`, `--format` | `p` plain, `t` tar | Plain = ready-to-start data dir. |
| `-X`, `--wal-method` | `none`, `fetch`, `stream` | `stream` (default) streams WAL on a 2nd connection; needs `max_wal_senders >= 2`. |
| `-R`, `--write-recovery-conf` | flag | Writes `standby.signal` + connection info into `postgresql.auto.conf`. For standby setup. |
| `-S`, `--slot` | name | Replication slot for WAL streaming. |
| `-C`, `--create-slot` | flag | Create the slot named by `-S` first. |
| `-c`, `--checkpoint` | `fast`, `spread` | `fast` forces an immediate checkpoint. |
| `-P`, `--progress` | flag | Progress reporting. |
| `-z`, `--gzip` / `--compress` | method[:detail] | Compression, tar format. |
| `-i`, `--incremental` | manifest path | Incremental backup vs a prior manifest. **v17+.** |

`-R` produces a STANDBY (writes `standby.signal`). For PITR you instead create
`recovery.signal` yourself; see the recovery parameters below.

## WAL archiving parameters (postgresql.conf) :

| Parameter | Required value | Notes |
|-----------|----------------|-------|
| `wal_level` | `replica` or higher | Restart required to change. |
| `archive_mode` | `on` | Restart required to change. |
| `archive_command` | shell command | `%p` = WAL path, `%f` = WAL filename, `%%` = literal `%`. Must exit 0 only on real success. |
| `archive_library` | library name | Alternative to `archive_command`. |
| `max_wal_senders` | `>= 2` | Needed for `pg_basebackup -X stream`. |

Example archive command (refuses to overwrite) :
`archive_command = 'test ! -f /mnt/archive/%f && cp %p /mnt/archive/%f'`

## Recovery parameters (PITR, postgresql.conf) :

| Parameter | Effect |
|-----------|--------|
| `restore_command` | Shell command to fetch one archived WAL file. `%f` = name wanted, `%p` = destination path. Must exit 0 on success, non-zero when the file is absent (not an error). |
| `recovery_target_time` | Stop recovery at this timestamp. |
| `recovery_target_name` | Stop at a named restore point (`pg_create_restore_point`). |
| `recovery_target_xid` | Stop at a transaction id. |
| `recovery_target_lsn` | Stop at a WAL LSN. |
| `recovery_target_inclusive` | Whether the target record itself is applied. |
| `recovery_target_action` | `pause` (default), `promote`, or `shutdown` when target is reached. |
| `recovery_target_timeline` | Which timeline to follow. |

`recovery.signal` : an empty file in the data directory. Its presence puts the server
into archive-recovery mode at startup; the server deletes it when recovery completes.

## PITR restore sequence :

1. Stop the server if running.
2. Copy the current data directory and tablespaces aside (preserves unarchived WAL).
3. Remove all files under the data directory and tablespace roots.
4. Restore the base backup into the data directory with correct ownership.
5. Empty `pg_wal/` from the restored backup; recreate the directory.
6. Copy any unarchived WAL segments saved in step 2 into `pg_wal/`.
7. Set `restore_command` and the `recovery_target_*` parameters in postgresql.conf.
8. Create an empty `recovery.signal` file in the data directory.
9. Start the server; it replays WAL to the target, then runs `recovery_target_action`.
10. Verify the recovered data.

The recovery target MUST be after the base backup's end (`pg_backup_stop` time).

## pg_combinebackup :

Reconstructs a synthetic full backup from a full backup plus its incremental chain.

| Option | Effect |
|--------|--------|
| `-o`, `--output` | Output directory. Currently required. |
| `-T`, `--tablespace-mapping=OLD=NEW` | Relocate a tablespace. Repeatable. |
| `-N`, `--no-sync` | Skip the fsync of output files (faster, less safe). |
| `--manifest-checksums` | `NONE`, `CRC32C` (default), `SHA224/256/384/512`. |

Pass backup directories oldest → newest : full backup first, final incremental last.
An incremental backup alone is NOT restorable; it must go through `pg_combinebackup`.

## pg_verifybackup :

Validates a backup against its `backup_manifest` (file list, sizes, checksums). Run it
after every `pg_basebackup` and after `pg_combinebackup`.

## Sources :

- https://www.postgresql.org/docs/17/app-pgdump.html
- https://www.postgresql.org/docs/17/app-pgrestore.html
- https://www.postgresql.org/docs/17/app-pg-dumpall.html
- https://www.postgresql.org/docs/17/app-pgbasebackup.html
- https://www.postgresql.org/docs/17/app-pgcombinebackup.html
- https://www.postgresql.org/docs/17/continuous-archiving.html
