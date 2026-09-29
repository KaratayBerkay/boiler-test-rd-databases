# rd-databases — cross-engine results

Generated 2026-09-14T12:48:25+00:00 from `results-k3s/<engine>/latest.json` (the k3s runtime (`RDLAB_PLATFORM=k3s`, ports via k8s/ports.json)). Timings are client-observed p50 latencies in milliseconds (warm cache, single connection, 10 iterations unless noted). `--` = unsupported syntax, `ERR` = failed at run time.

## Engines

| engine | version | category | image | load rows/s (events) | schema+load s | replication |
|---|---|---|---|---:|---:|---|
| ClickHouse | ClickHouse 26.8.2.7 | analytical (columnar) | `clickhouse/clickhouse-server:latest` | 52117 | 8.63 | - |
| CockroachDB | CockroachDB CCL v26.2.6 (x86_64-pc-linux-gnu, built 2026/08/ | distributed SQL | `cockroachdb/cockroach:latest` | 13115 | 56.46 | - |
| MySQL 9 | 9.7.2 | row-store OLTP | `mysql:9` | 49281 | 10.68 | - |
| PostgreSQL 18 | PostgreSQL 18.6 (Debian 18.6-1.pgdg12+2) on x86_64-pc-linux- | row-store OLTP | `pgvector/pgvector:pg18` | 137011 | 6.59 | physical streaming (async, slot) |

## Query capability & latency matrix (p50 ms)

| query | ClickHouse | CockroachDB | MySQL 9 | PostgreSQL 18 |
|---|---:|---:|---:|---:|
| `q00_ping` | · | · | · | · |
| `q01_pk_lookup` | · | · | · | · |
| `q02_range_scan` | · | · | · | · |
| `q03_join_agg` | · | · | · | · |
| `q04_window_rank` | · | · | · | · |
| `q05_window_running` | · | · | · | · |
| `q06_recursive_cte` | · | · | · | · |
| `q07_cte_cohort` | · | · | · | · |
| `q08_exists_semijoin` | · | · | · | · |
| `q09_correlated_scalar` | · | · | · | · |
| `q10_lateral_topn` | · | · | · | · |
| `q10b_topn_window` | · | · | · | · |
| `q11_grouping_sets` | · | · | · | · |
| `q12_json_filter` | · | · | · | · |
| `q13_fulltext` | · | · | · | · |
| `q14_upsert` | · | · | · | · |
| `q15_merge` | · | · | · | · |
| `q16_offset_pagination` | · | · | · | · |
| `q17_keyset_pagination` | · | · | · | · |
| `q17b_keyset_rowvalue` | · | · | · | · |
| `q18_count_distinct` | · | · | · | · |
| `q19_anti_join` | · | · | · | · |
| `q19b_not_exists` | · | · | · | · |
| `q20_gaps_islands` | · | · | · | · |
| `q21_percentile` | · | · | · | · |
| `q22_string_agg` | · | · | · | · |
| `q23_case_pivot` | · | · | · | · |
| `q24_or_predicate` | · | · | · | · |
| `q25_in_list` | · | · | · | · |
| `q26_nonsargable` | · | · | · | · |
| `q27_update_single` | · | · | · | · |
| `q28_insert_single` | · | · | · | · |
| `q29_txn_order` | · | · | · | · |

## Feature probes

| feature | ClickHouse | CockroachDB | MySQL 9 | PostgreSQL 18 |
|---|:---:|:---:|:---:|:---:|

## Optimisation experiments (speed-up of best variant vs first variant, p50)

| experiment | ClickHouse | CockroachDB | MySQL 9 | PostgreSQL 18 |
|---|---:|---:|---:|---:|

## Connections

| engine | max conn | connect p50 ms (direct) | connect p50 ms (proxy) | storm 200: opened/failed | 1 worker qps | 32 workers qps | 32w p99 ms | deadlock detected (ms) | RR write conflict |
|---|---:|---:|---:|---|---:|---:|---:|---|---|

## Replication / scaling

| engine | kind | visibility lag p50 / p95 / max ms | replica rejects writes | catch-up after 20k rows (ms) | read scaling gain | failover downtime ms |
|---|---|---|---|---|---:|---:|
| PostgreSQL 18 | physical streaming (async, slot) | 0.90 / 14 / 32 | yes 25006 | 3.6 | 0.94 | 6407.9 |

## Backup & recovery drills (backup -> restore elsewhere -> fingerprint of every table; PITR = point-in-time / incremental probe)

| engine | strategy | kind | tool | backup s | size MB | restore s | verified | PITR / probe | notes |
|---|---|---|---|---:|---:|---:|:---:|:---:|---|
| ClickHouse | `backup_disk` | physical | BACKUP DATABASE TO Disk('backups', ...) | 0.06 | 13.5 | 1.28 | ✅ | · | single-replica backup of every table's parts + metadata into the `backups` disk (/backups); restored under a new database name on the same s |
| ClickHouse | `backup_cluster` | physical | BACKUP DATABASE ON CLUSTER | 0.49 | 13.6 | 0.41 | ✅ | · | cluster-coordinated backup (each shard once, the replicas split the parts); drill: DROP TABLE ... ON CLUSTER then RESTORE TABLE ... ON CLUST |
| ClickHouse | `incremental` | incremental | BACKUP ... SETTINGS base_backup | 0.08 | 0.1 | 0.21 | ✅ | ✅ | only parts not present in the base backup are written; RESTORE from the incremental resolves the base automatically |
| CockroachDB | `full` | physical | BACKUP DATABASE INTO 'nodelocal://1/lab' WITH revision_history | 1.11 | 21.7 | 1.43 | ✅ | · | distributed backup job (every node exports its ranges as SSTs) with MVCC revision history; restored under a new database name |
| CockroachDB | `incremental` | incremental | BACKUP DATABASE INTO LATEST IN | 0.41 | 0.1 | 1.13 | ✅ | ✅ | incremental layer appended to the same collection (INTO LATEST IN); RESTORE FROM LATEST applies full + incrementals |
| CockroachDB | `pitr` | pitr | RESTORE ... AS OF SYSTEM TIME | 0.37 | 0.1 | 1.14 | ✅ | ✅ | revision_history keeps every MVCC version in the backup chain; RESTORE ... AS OF SYSTEM TIME picks the state before the DELETE |
| MySQL 9 | `mysqlsh_dump` | logical | mysqlsh util dump-tables / load-dump | 2.33 | 9.2 | 10.35 | ✅ | · | MySQL Shell dump utility: 4 threads, zstd chunks, loaded with LOAD DATA LOCAL INFILE into a second schema |
| MySQL 9 | `mysqldump` | logical | mysqldump --single-transaction | 0.99 | 43.3 | 12.85 | ✅ | · | single-transaction consistent snapshot, plain SQL restored serially with the mysql client |
| MySQL 9 | `clone` | physical | CLONE LOCAL DATA DIRECTORY + START REPLICA UNTIL SQL_BEFORE_GTIDS | 2.18 | 258.0 | 1.53 | ✅ | ✅ | clone plugin physical hot copy (no external tool), scratch instance replays the source binlog with GTID auto-positioning and stops before th |
| PostgreSQL 18 | `pg_dump` | logical | pg_dump -Fd -j4 --compress=zstd | pg_restore -j4 | 0.67 | 9.2 | 1.51 | ✅ | · | directory format, 4 parallel jobs, zstd; globals dumped separately with pg_dumpall |
| PostgreSQL 18 | `pg_basebackup` | physical | pg_basebackup -Fp -Xs | 1.38 | 113.4 | 0.24 | ✅ | · | plain-format full copy with streamed WAL (-Xs), verified with pg_verifybackup; restored by copying into a scratch container |
| PostgreSQL 18 | `incremental` | incremental | pg_basebackup --incremental + pg_combinebackup | 2.86 | 20.7 | 0.25 | ✅ | ✅ | block-level incremental (summarize_wal=on, PG17+), restored with pg_combinebackup full incr -o PGDATA |
| PostgreSQL 18 | `pitr` | pitr | archive_command + restore_command + recovery_target_name | 0.0 | 304.0 | 0.26 | ✅ | ✅ | WAL archive (archive_command -> /backups/wal) replayed on top of the full backup up to the named restore point; the DELETE that followed is  |

## Logging (slow-query capture, audit trail, cost of logging every statement; container logs = kubelet container logs (`/var/log/pods`, rotated by container-log-max-size/files))

| engine | slow-query sink | slow query captured (after s) | fast query filtered | audit mechanism | audit event found | full statement logging: qps baseline -> logged (overhead %) | bytes / statement |
|---|---|:---:|:---:|---|:---:|---|---:|
| MySQL 9 | slow query log file (long_query_time) | ✅ (0.6) | ✅ | general log (no audit plugin in MySQL Community) | ✅ | 18499 -> 18579 (-0.4 %) | 637.0 |
| PostgreSQL 18 | jsonlog file (logging_collector) via log_min_duration_statement | ✅ (0.45) | ✅ | log_statement=ddl (pgaudit not bundled in the image) | ✅ | 23086 -> 21072 (8.7 %) | 1460.6 |

## Notes per engine

- **ClickHouse**: Two replicas of one shard using ReplicatedMergeTree with ClickHouse Keeper; DDL runs ON CLUSTER. No transactions, no foreign keys; secondary "indexes" are data-skipping (bloom filter) indexes; UPDATE/DELETE are asynchronous mutations. 
- **CockroachDB**: 3-node insecure cluster; every range replicated 3x via Raft, any node serves reads and writes (no promotion needed). HAProxy round-robins connections across nodes (the recommended production pattern). Insecure mode: no passwords. 
- **MySQL 9**: MySQL 9.x (innovation track). GTID-based asynchronous replication with SOURCE_AUTO_POSITION, replica in super_read_only; ProxySQL 3 splits SELECTs to the reader hostgroup (source+replica) and everything else to the source. 
- **PostgreSQL 18**: Official postgres:18 image plus pgvector. Physical streaming replication (async) with a replication slot; PgBouncer in transaction mode with max_prepared_statements; HAProxy write (5000) and round-robin read (5001) endpoints. 
