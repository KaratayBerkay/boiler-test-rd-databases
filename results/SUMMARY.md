# rd-databases — cross-engine results

Generated 2026-09-14T12:48:24+00:00 from `results/<engine>/latest.json` (the Docker Compose runtime). Timings are client-observed p50 latencies in milliseconds (warm cache, single connection, 10 iterations unless noted). `--` = unsupported syntax, `ERR` = failed at run time.

## Engines

| engine | version | category | image | load rows/s (events) | schema+load s | replication |
|---|---|---|---|---:|---:|---|
| Citus 13 (PG 18) sharded, 5x2 | PostgreSQL 18.4 (Debian 18.4-1.pgdg13+1) on x86_64-pc-linux- | distributed SQL (sharded PostgreSQL) | `citusdata/citus:latest` | 142318 | 14.28 | streaming replica per worker shard (Citus secondary nodes) + hash sharding over 5 workers |
| ClickHouse | ClickHouse 26.8.2.7 | analytical (columnar) | `clickhouse/clickhouse-server:latest` | 40805 | 32.25 | ReplicatedMergeTree (multi-writer, Keeper-coordinated, eventually consistent) |
| CockroachDB | CockroachDB CCL v26.2.6 (x86_64-pc-linux-gnu, built 2026/08/ | distributed SQL | `cockroachdb/cockroach:latest` | 13374 | 206.8 | Raft consensus, 3 replicas per range (multi-active) |
| CrateDB | CrateDB 6.4.4 | distributed SQL (search-backed) | `crate:latest` | 17278 | 87.18 | sharded, replicated shards (multi-active, eventually consistent) |
| IBM Db2 12.1 CE | DB2 v12.1.5.0 | row-store OLTP | `icr.io/db2_community/db2:latest` | 12310 | 127.88 | none |
| DuckDB | DuckDB 1.5.5 | embedded analytical (columnar) | `(embedded, python duckdb package)` | 408635 | 7.08 | none |
| Firebird 5 | Firebird LI-V6.3.4.1812 Firebird 5.0 | row-store OLTP | `firebirdsql/firebird:5` | 1200 | 521.04 | none |
| H2 2.x | PostgreSQL 8.2.23 server protocol using H2 2.3.232 (2024-08- | embedded / Java server | `rdlab/h2:2.3.232 (eclipse-temurin:21-jre + h2 jar)` | 6618 | 250.29 | none |
| MariaDB 11 | 11.8.9-MariaDB-ubu2404-log | row-store OLTP | `mariadb:11` | 110677 | 15.85 | GTID async (binlog row) + MaxScale auto-failover |
| MonetDB | MonetDB 11.55.7 | analytical (columnar) | `monetdb/monetdb:latest` | 396626 | 3.71 | none |
| SQL Server 2025 | Microsoft SQL Server 2025 (RTM-CU8-GDR) (KB5122769) - 17.0.4 | row-store OLTP | `mcr.microsoft.com/mssql/server:2025-latest` | 2727 | 607.48 | Availability Group (read-scale, sync commit, auto seeding) |
| MySQL 9 | 9.7.2 | row-store OLTP | `mysql:9` | 72264 | 27.06 | GTID async (binlog row) |
| Oracle 26ai Free | Oracle AI Database 26ai Free Release 23.26.3.0.0 - Develop,  | row-store OLTP | `gvenzl/oracle-free:23-slim-faststart` | 13372 | 95.84 | none |
| PostgreSQL 18 | PostgreSQL 18.6 (Debian 18.6-1.pgdg12+2) on x86_64-pc-linux- | row-store OLTP | `pgvector/pgvector:pg18` | 120951 | 26.32 | physical streaming (async, slot) |
| Percona XtraDB Cluster 8.4 (Galera) | 8.4.10-10.1 | multi-master (Galera) | `percona/percona-xtradb-cluster:8.4` | 41383 | 43.77 | Galera (virtually synchronous multi-master) |
| QuestDB | QuestDB 10.0.1 | time-series | `questdb/questdb:latest` | 505305 | 4.46 | none |
| SQLite 3 | SQLite 3.45.1 | embedded | `(embedded, python stdlib sqlite3)` | 31785 | 45.08 | none |
| TiDB 8.5 | 8.0.11-TiDB-v8.5.8 | distributed SQL (HTAP) | `pingcap/tidb:v8.5.8 (+ pd, tikv v8.5.8)` | 26134 | 83.6 | Raft (TiKV, 3 replicas per region), stateless SQL layer |
| YugabyteDB | PostgreSQL 15.12-YB-2026.1.1.2-b0 on x86_64-pc-linux-gnu, co | distributed SQL | `yugabytedb/yugabyte:latest` | 38732 | 59.61 | Raft per tablet, RF=3 (multi-active) |

## Query capability & latency matrix (p50 ms)

| query | Citus 13 (PG 18) sharded, 5x2 | ClickHouse | CockroachDB | CrateDB | IBM Db2 12.1 CE | DuckDB | Firebird 5 | H2 2.x | MariaDB 11 | MonetDB | SQL Server 2025 | MySQL 9 | Oracle 26ai Free | PostgreSQL 18 | Percona XtraDB Cluster 8.4 (Galera) | QuestDB | SQLite 3 | TiDB 8.5 | YugabyteDB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `q00_ping` | 0.28 | 5.05 | 0.89 | 2.23 | 0.81 | 0.29 | 2.54 | 41 | 0.27 | 0.46 | 0.63 | 0.25 | 0.40 | 0.25 | 0.20 | 0.29 | 0.01 | 0.89 | 0.22 |
| `q01_pk_lookup` | 1.44 | 7.71 | 1.94 | 4.89 | 1.88 | 0.82 | 4.12 | 41 | 0.72 | 1.45 | 1.00 | 0.38 | 0.45 | 0.35 | 0.31 | 0.44 | 0.01 | 2.01 | 0.48 |
| `q02_range_scan` | 27 | 17 | 167 | 26 | 37 | 7.14 | 46 | 49 | 65 | 7.92 | 35 | 86 | 12 | 9.44 | 80 | 1.56 | 19 | 15 | 171 |
| `q03_join_agg` | 14.26 s | 42 | 1.59 s | 402 | 120 | 28 | 197 | 42 | 692 | 24 | 152 | 473 | 101 | 79 | 450 | 68 | 238 | 878 | 1.34 s |
| `q04_window_rank` | 289 | 26 | 414 | 402 | 105 | 28 | 243 | 1.31 s | 963 | 61 | 82 | 1.18 s | 109 | 93 | 1.15 s | -- | 615 | 162 | 794 |
| `q05_window_running` | 30 | 8.37 | 167 | 18 | 20 | 8.99 | 26 | 44 | 71 | 3.10 | 36 | 65 | 18 | 12 | 61 | 8.46 | 45 | 6.09 | 298 |
| `q06_recursive_cte` | 3.01 | 29 | 10 | -- | 8.96 | 4.55 | 17 | 56 | 4.68 | 4.59 | 19 | 4.12 | 2.13 | 1.58 | 4.32 | -- | 0.74 | 15 | 5.53 |
| `q07_cte_cohort` | 1.02 s | 28 | 580 | 321 | 298 | 56 | 654 | 42 | 666 | 92 | 111 | 1.62 s | 190 | 98 | 1.53 s | 110 | 1.10 s | 228 | 695 |
| `q08_exists_semijoin` | -- | 20 | 3.49 s | 105.94 s | 87 | 23 | 29 | 44 | 280 | 8.89 | 233 | 304 | 32 | 18 | 290 | -- | 17 | 132 | 763 |
| `q09_correlated_scalar` | 13 | -- | 75 | 4.80 s | 27 | 5.66 | 25 | 49 | 10 | 6.16 | 28 | 29 | 9.66 | 5.94 | 30 | -- | 7.50 | 40 | 658 |
| `q10_lateral_topn` | -- | -- | 23 | -- | 13 | 9.31 | -- | -- | -- | -- | 10 | 14 | 4.39 | 1.88 | 11 | -- | -- | 294 | 150 |
| `q10b_topn_window` | 9.18 | 8.07 | 25 | 15 | 11 | 7.65 | 35 | 51 | 6.67 | 7.26 | 22 | 16 | 7.18 | 1.66 | 14 | 1.41 | 2.73 | 34 | 122 |
| `q11_grouping_sets` | -- | 19 | -- | -- | 22 | 9.53 | -- | -- | 92 | 5.92 | 39 | 87 | 15 | 10 | 80 | -- | -- | 23 | 291 |
| `q12_json_filter` | 2.35 | 11 | 36 | 9.77 | 57 | 3.22 | -- | -- | 2.74 | 23 | 7.34 | 2.81 | 3.50 | 0.91 | 3.12 | -- | 1.12 | 5.12 | 11 |
| `q13_fulltext` | 0.93 | 11 | 132 | 9.86 | 2.78 | 20 | 15 | 43 | 7.00 | 2.69 | 20 | 8.96 | 11 | 0.47 | 8.83 | 2.11 | 0.23 | ERR | 79 |
| `q14_upsert` | -- | -- | 2.48 | ERR | 1.42 | 4.04 | 3.92 | 41 | 0.45 | 4.76 | 35 | 1.27 | 0.98 | 1.09 | 1.73 | -- | 0.04 | -- | 2.75 |
| `q15_merge` | ERR | -- | -- | -- | 0.98 | 3.01 | 3.39 | 41 | -- | 4.73 | 11 | -- | 1.10 | 0.67 | -- | -- | -- | -- | -- |
| `q16_offset_pagination` | 276 | 11 | 329 | 307 | 152 | 25 | 69 | 42 | 82 | 214 | 87 | 112 | 232 | 20 | 102 | 5.35 | 257 | 347 | 365 |
| `q17_keyset_pagination` | 26 | 10 | 150 | 15 | 23 | 8.91 | 33 | 42 | 57 | 4.39 | 27 | 66 | 20 | 10 | 61 | 1.58 | 27 | 3.39 | 345 |
| `q17b_keyset_rowvalue` | 35 | -- | 185 | -- | -- | 7.69 | -- | -- | 59 | -- | -- | 64 | -- | 10 | 62 | -- | 27 | 2.62 | 270 |
| `q18_count_distinct` | 326 | 14 | 605 | 84 | 138 | 19 | 179 | 41 | 381 | 30 | 30 | 366 | 101 | 161 | 356 | 8.05 | 257 | 99 | 1.52 s |
| `q19_anti_join` | 1.94 s | 12 | 306 | 400 | 60 | 12 | 112 | 41 | 13 | 34 | 15 | 64 | 35 | 22 | 53 | 43 | 38 | 480 | 888 |
| `q19b_not_exists` | -- | 7.67 | 330 | 6.07 s | 58 | 7.24 | 13 | 41 | 12 | 18 | 13 | 63 | 11 | 3.52 | 52 | -- | 2.77 | 460 | 2.09 s |
| `q20_gaps_islands` | 510 | 37 | 1.05 s | 207 | 309 | 18 | 583 | 1.07 s | 397 | 60 | 58 | 653 | 217 | 182 | 568 | 44 | 378 | 118 | 888 |
| `q21_percentile` | 330 | 10 | 276 | 148 | 118 | 5.45 | -- | 41 | 315 | 34 | 177 | -- | 319 | 56 | -- | 1.63 s | -- | -- | 396 |
| `q22_string_agg` | 7.86 s | 9.69 | 23 | 1.02 s | 12 | 7.31 | 652 | 47 | 11 | 5.03 | 17 | 8.14 | 4.19 | 4.94 | 11 | 40 | 2.05 | 43 | 146 |
| `q23_case_pivot` | 7.93 s | 12 | 211 | 251 | 98 | 20 | 117 | 41 | 514 | 17 | 82 | 597 | 63 | 36 | 576 | 42 | 345 | 75 | 283 |
| `q24_or_predicate` | 4.83 | 7.28 | 2.84 | 6.49 | 1.12 | 2.58 | 4.17 | 57 | 0.59 | 1.69 | 2.65 | 0.39 | 0.58 | 0.41 | 0.79 | 1.01 | 0.02 | 5.67 | 1.48 |
| `q25_in_list` | 14 | 11 | 6.56 | 33 | 11 | 10.00 | 18 | 48 | 9.09 | 6.18 | 9.44 | 4.21 | 2.43 | 1.87 | 8.78 | 3.12 | 0.61 | 23 | 5.31 |
| `q26_nonsargable` | 73 | 6.77 | 182 | 19 | 13 | 4.10 | 24 | 41 | 49 | 3.82 | 40 | 45 | 39 | 22 | 43 | 1.28 | 121 | 2.46 | 189 |
| `q27_update_single` | 1.56 | 23 | 3.86 | 5.40 | 1.18 | 1.02 | 2.64 | 41 | 0.54 | 1.86 | 3.59 | 0.18 | 0.70 | 0.96 | 1.58 | 17 | 0.02 | 4.05 | 1.72 |
| `q28_insert_single` | 1.76 | 81 | 2.39 | 5.47 | 1.39 | 2.23 | 3.38 | 41 | 0.70 | 5.88 | 4.19 | 1.16 | 0.75 | 0.95 | 1.93 | 0.40 | 0.04 | 3.35 | 1.54 |
| `q29_txn_order` | 6.47 | -- | 9.96 | -- | 4.95 | 7.89 | 11 | 246 | 3.48 | 13 | 11 | 8.43 | 1.27 | 2.02 | 5.42 | -- | 0.12 | 15 | 6.07 |

## Feature probes

| feature | Citus 13 (PG 18) sharded, 5x2 | ClickHouse | CockroachDB | CrateDB | IBM Db2 12.1 CE | DuckDB | Firebird 5 | H2 2.x | MariaDB 11 | MonetDB | SQL Server 2025 | MySQL 9 | Oracle 26ai Free | PostgreSQL 18 | Percona XtraDB Cluster 8.4 (Galera) | QuestDB | SQLite 3 | TiDB 8.5 | YugabyteDB |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| materialized_view | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ |
| generated_column | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| identity | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| returning | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ✅ |
| sequence | ✅ | ❌ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ |
| arrays | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ⚠️ | ✅ | ❌ | ❌ | ⚠️ | ❌ | ❌ | ✅ | ❌ | ✅ | ⚠️ | ❌ | ✅ |
| vector_type | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ✅ | ❌ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ |
| statement_timeout | ✅ | ✅ | ✅ | ✅ | · | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ |
| filter_clause | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ✅ |
| distinct_on | ✅ | ✅ | ✅ | ❌ | ⚠️ | ✅ | ❌ | ⚠️ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| qualify | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ | ✅ | ❌ | ⚠️ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| check_constraint | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| fk_enforced | ❌ | ❌ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| savepoint | ✅ | ❌ | ✅ | ❌ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| temporal_query | ❌ | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ |
| json_aggregate | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| isolation_serializable | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| isolation_repeatable_read | ✅ | · | ✅ | · | ✅ | · | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | · | · | ✅ | ✅ |
| isolation_read_uncommitted | ✅ | · | ✅ | · | ✅ | · | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | · | ✅ | ❌ | ✅ |
| regex_match | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| citus_shards | ✅ | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · |
| citus_secondary_reads | ⚠️ | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · |
| partitioning | · | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| json_type | · | ✅ | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · |
| cte_dml | · | · | ✅ | · | · | · | · | · | · | · | · | · | · | ✅ | · | · | · | · | ✅ |
| follower_read | · | · | ✅ | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · |
| geo_type | · | · | · | ✅ | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · |
| pivot_statement | · | · | · | · | · | ✅ | · | · | · | · | · | · | · | · | · | · | · | · | · |
| invisible_index | · | · | · | · | · | · | · | · | ✅ | · | · | ✅ | · | · | ✅ | · | · | ✅ | · |
| isolation_snapshot | · | · | · | · | · | · | · | · | · | · | ✅ | · | · | · | · | · | · | · | · |
| columnstore_index | · | · | · | · | · | · | · | · | · | · | ✅ | · | · | · | · | · | · | · | · |
| boolean_type | · | · | · | · | · | · | · | · | · | · | · | · | ✅ | · | · | · | · | · | · |
| server_cursor | · | · | · | · | · | · | · | · | · | · | · | · | · | ✅ | · | · | · | · | ✅ |
| sample_by | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | ✅ | · | · | · |
| asof_join | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | ✅ | · | · | · |
| strict_table | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | · | ✅ | · | · |

## Optimisation experiments (speed-up of best variant vs first variant, p50)

| experiment | Citus 13 (PG 18) sharded, 5x2 | ClickHouse | CockroachDB | CrateDB | IBM Db2 12.1 CE | DuckDB | Firebird 5 | H2 2.x | MariaDB 11 | MonetDB | SQL Server 2025 | MySQL 9 | Oracle 26ai Free | PostgreSQL 18 | Percona XtraDB Cluster 8.4 (Galera) | QuestDB | SQLite 3 | TiDB 8.5 | YugabyteDB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `opt01_composite_index` | x14.08 (index_(event_type,occurred_at)) | x1.0 (no_index) | x12.1 (index_(event_type,occurred_at)) | 6.153 / - / - ms | x32.76 (index_(event_type,occurred_at)) | x1.17 (index_(event_type,occurred_at)) | x53.09 (index_(event_type,occurred_at)) | x1.01 (index_(occurred_at,event_type)) | x34.96 (index_(event_type,occurred_at)) | x1.14 (index_(occurred_at,event_type)) | x2.85 (index_(event_type,occurred_at)) | x20.37 (index_(event_type,occurred_at)) | x12.57 (index_(event_type,occurred_at)) | x16.44 (index_(event_type,occurred_at)) | x23.62 (index_(event_type,occurred_at)) | 1.731 / - / - ms | x44.82 (index_(event_type,occurred_at)) | x1.0 (no_index) | x13.93 (index_(event_type,occurred_at)) |
| `opt02_covering_index` | x1.53 (covering_include) | x1.27 (covering_include) | x4.41 (covering_include) | 22.875 / - ms | 20.211 / - ms | 3.775 / - ms | 37.847 / - ms | 76.001 / - ms | 19.421 / - ms | 6.352 / - ms | x2.4 (covering_include) | 29.133 / - ms | 7.282 / - ms | x1.01 (covering_include) | 26.615 / - ms | 2.432 / - ms | 4.494 / - ms | 40.287 / - ms | x1.01 (covering_include) |
| `opt03_sargable_predicate` | x14.34 (sargable_with_index) | x1.59 (sargable_no_index) | x17.46 (sargable_with_index) | x1.69 (sargable_no_index) | x10.35 (nonsargable_with_index) | x2.18 (sargable_no_index) | x7.65 (sargable_with_index) | x1.0 (nonsargable_with_index) | x18.3 (sargable_with_index) | x2.82 (sargable_with_index) | x13.28 (sargable_with_index) | x9.72 (sargable_with_index) | x41.34 (sargable_with_index) | x24.52 (sargable_with_index) | x7.8 (sargable_with_index) | x3.46 (sargable_no_index) | x290.98 (sargable_with_index) | x1.41 (sargable_with_index) | x1.84 (sargable_with_index) |
| `opt04_pagination` | x35.83 (keyset_rowvalue_with_index) | x1.62 (keyset_with_index) | x100.72 (keyset_rowvalue_with_index) | x33.57 (keyset_no_index) | x88.37 (keyset_with_index) | x2.79 (keyset_rowvalue_with_index) | x5.24 (keyset_with_index) | x1.0 (keyset_with_index) | x60.1 (keyset_with_index) | x48.79 (keyset_with_index) | x40.98 (keyset_with_index) | x12.2 (keyset_with_index) | x24.13 (keyset_with_index) | x99.86 (keyset_rowvalue_with_index) | x40.08 (keyset_with_index) | x6.41 (keyset_no_index) | x2374.19 (keyset_rowvalue_with_index) | x107.49 (keyset_no_index) | x1.35 (keyset_rowvalue_with_index) |
| `opt05_correlated_vs_join` | x2.05 (join_rewrite) | None / 9.139 ms | x2.76 (join_rewrite) | x194.85 (join_rewrite) | x1.08 (join_rewrite) | x1.92 (join_rewrite) | x1.0 (correlated_subquery) | x1.0 (correlated_subquery) | x1.0 (correlated_subquery) | x1.16 (join_rewrite) | x1.34 (join_rewrite) | x1.73 (join_rewrite) | x1.39 (join_rewrite) | x2.43 (join_rewrite) | x2.15 (join_rewrite) | None / 4.276 ms | x2.26 (join_rewrite) | x1.0 (correlated_subquery) | x28.82 (join_rewrite) |
| `opt06_antijoin_forms` | x1.14 (not_in) | x1.54 (not_in) | x1.08 (not_in) | x1.0 (left_join_is_null) | x1.0 (left_join_is_null) | x2.24 (not_exists) | x8.39 (not_exists) | x1.0 (left_join_is_null) | x1.16 (not_in) | x2.23 (not_exists) | x1.01 (not_exists) | x1.01 (not_in) | x3.12 (not_exists) | x6.81 (not_exists) | x1.01 (not_exists) | 48.788 / None / None ms | x23.93 (not_in) | x1.15 (not_exists) | x1.0 (left_join_is_null) |
| `opt07_or_vs_union` | x1.0 (or_predicate) | 12.029 / None ms | x1.01 (union_rewrite) | x1.0 (or_predicate) | x1.0 (or_predicate) | x1.0 (or_predicate) | x1.31 (union_rewrite) | x1.19 (union_rewrite) | x1.0 (or_predicate) | x1.0 (or_predicate) | x1.0 (or_predicate) | x1.0 (or_predicate) | x1.0 (or_predicate) | x1.0 (or_predicate) | x1.0 (or_predicate) | x1.29 (union_rewrite) | x1.0 (or_predicate) | x1.37 (union_rewrite) | x1.0 (or_predicate) |
| `opt08_partial_index` | x2.07 (full_index_(status,ordered_at)) | x1.0 (no_partial_index) | x7.58 (partial_index_pending) | 12.484 / - / - ms | x3.18 (full_index_(status,ordered_at)) | x1.09 (full_index_(status,ordered_at)) | x3.32 (full_index_(status,ordered_at)) | x1.0 (full_index_(status,ordered_at)) | x5.24 (full_index_(status,ordered_at)) | x1.0 (no_partial_index) | x1.0 (no_partial_index) | x6.45 (full_index_(status,ordered_at)) | x1.02 (full_index_(status,ordered_at)) | x9.56 (partial_index_pending) | x4.6 (full_index_(status,ordered_at)) | 0.87 / - / - ms | x16.34 (full_index_(status,ordered_at)) | x1.0 (no_partial_index) | x17.33 (full_index_(status,ordered_at)) |
| `opt09_stats_refresh` | x1.01 (after_analyze) | x1.21 (after_analyze) | x1.08 (after_analyze) | x1.07 (after_analyze) | x1.04 (after_analyze) | x1.04 (after_analyze) | x1.0 (after_bulk_insert_no_analyze) | x1.0 (after_analyze) | x6.65 (after_analyze) | x1.18 (after_analyze) | x1.0 (after_bulk_insert_no_analyze) | x1.03 (after_analyze) | x1.0 (after_analyze) | x1.0 (after_bulk_insert_no_analyze) | x1.01 (after_analyze) | x1.0 (after_bulk_insert_no_analyze) | x1.01 (after_analyze) | x1.0 (after_bulk_insert_no_analyze) | x1.0 (after_analyze) |
| `opt10_batch_inserts` | 381 / 1617 / 6420 rows/s | 12 / 2554 / 2783 rows/s | 440 / 2805 / 6325 rows/s | 194 / 29162 / 15057 rows/s | 643 / 31941 / 12565 rows/s | 430 / 450 / 1782 rows/s | 264 / 903 / ERR rows/s | 24 / 7555 / 9301 rows/s | 1616 / 3486 / 34143 rows/s | 98 / 113 / 8703 rows/s | 210 / 903 / 2534 rows/s | 388 / 948 / 29579 rows/s | 697 / 8714 / ERR rows/s | 634 / 18990 / 34082 rows/s | 418 / 2535 / 29923 rows/s | 3173 / 18416 / 8638 rows/s | 6721 / 24786 / 20840 rows/s | 330 / 565 / 10030 rows/s | 334 / 716 / 9310 rows/s |
| `opt11_prepared_statements` | 1.3129 / 1.3039 / 0.5571 ms | n/a | 1.7845 / 1.683 / 1.7006 ms | n/a | n/a | n/a | n/a | 41.0676 / 41.0028 / 41.0391 ms | n/a | n/a | n/a | n/a | n/a | 0.848 / 0.5487 / 0.8389 ms | n/a | n/a | n/a | n/a | 1.0894 / 0.8751 / 1.1681 ms |

## Connections

| engine | max conn | connect p50 ms (direct) | connect p50 ms (proxy) | storm 200: opened/failed | 1 worker qps | 32 workers qps | 32w p99 ms | deadlock detected (ms) | RR write conflict |
|---|---:|---:|---:|---|---:|---:|---:|---|---|
| Citus 13 (PG 18) sharded, 5x2 | 300 | 6.48 | 2.08 | 200/0 | 776 | 1842 | 121 | yes 1000.7 | rejected |
| ClickHouse | 4096 | 33 | - | 200/0 | 200 | 3098 | 22 | - | - |
| CockroachDB | -1 | 2.84 | 3.17 | 9/0 | 615 | 20598 | 2.71 | yes 103.7 | rejected |
| CrateDB | None | 2.82 | - | - | 388 | 16602 | 3.82 | - | - |
| IBM Db2 12.1 CE | -1 | 4.20 | - | 200/0 | 743 | 22127 | 2.29 | yes 5746.2 | succeeded |
| DuckDB | None | 0.45 | - | - | 1348 | 980 | 151 | yes 1.2 | rejected |
| Firebird 5 | None | 85 | - | 200/0 | 264 | 5620 | 8.91 | yes 10002.6 | rejected |
| H2 2.x | None | 761 | - | 200/0 | 24 | 778 | 78 | yes 44.4 | rejected |
| MariaDB 11 | 1000 | 1.42 | 2.23 | 200/0 | 1843 | 44871 | 1.43 | yes 1.6 | rejected |
| MonetDB | 64 | 2.94 | - | 64/10 | 767 | 19945 | 2.95 | no | - |
| SQL Server 2025 | 32767 | 173 | - | 200/0 | 797 | 40906 | 1.38 | yes 3738.2 | succeeded |
| MySQL 9 | 1000 | 1.29 | 1.42 | 995/15 | 1929 | 49644 | 1.22 | yes 1.5 | succeeded |
| Oracle 26ai Free | 322 | 41 | - | 0/200 | 1603 | 16607 | 2.53 | yes 3267.9 | succeeded |
| PostgreSQL 18 | 300 | 15 | 8.89 | 200/0 | 3109 | 51390 | 1.28 | yes 1001.5 | rejected |
| Percona XtraDB Cluster 8.4 (Galera) | 1000 | 1.63 | 2.27 | 200/0 | 1429 | 48097 | 1.26 | yes 1.6 | succeeded |
| QuestDB | None | 1.14 | - | 200/0 | 3558 | 32363 | 11 | - | - |
| SQLite 3 | None | 0.58 | - | - | 99732 | 443278 | 0.11 | no | rejected |
| TiDB 8.5 | 0 | 2.72 | - | 200/0 | 540 | 22383 | 2.60 | yes 6.2 | succeeded |
| YugabyteDB | 500 | 21 | - | 200/0 | 901 | 35658 | 1.55 | yes 351.7 | rejected |

## Replication / scaling

| engine | kind | visibility lag p50 / p95 / max ms | replica rejects writes | catch-up after 20k rows (ms) | read scaling gain | failover downtime ms |
|---|---|---|---|---|---:|---:|
| Citus 13 (PG 18) sharded, 5x2 | streaming replica per worker shard (Citus secondary nodes) + hash sharding over 5 workers | 1.81 / 2.65 / 3.18 | yes 25006 | 6.8 | 0.98 | 1585.5 |
| ClickHouse | ReplicatedMergeTree (multi-writer, Keeper-coordinated, eventually consistent) | 29 / 30 / 30 | no | 31.7 | 0.98 | 1553.2 |
| CockroachDB | Raft consensus, 3 replicas per range (multi-active) | 1.54 / 1.95 / 2.68 | no | 35.0 | 1.15 | 494.4 |
| CrateDB | sharded, replicated shards (multi-active, eventually consistent) | 11 / 24 / 46 | no | 15.6 | 0.73 | - |
| IBM Db2 12.1 CE | none | single-node | - | - | - | - |
| DuckDB | none | single-node | - | - | - | - |
| Firebird 5 | none | single-node | - | - | - | - |
| H2 2.x | none | single-node | - | - | - | - |
| MariaDB 11 | GTID async (binlog row) + MaxScale auto-failover | 2.55 / 2.80 / 2.83 | yes 1290 | 7.1 | 1.01 | 2471.0 |
| MonetDB | none | single-node | - | - | - | - |
| SQL Server 2025 | Availability Group (read-scale, sync commit, auto seeding) | 942 / 961 / 973 | yes 3906 | 169.2 | 0.93 | 1582.5 |
| MySQL 9 | GTID async (binlog row) | 2.57 / 2.76 / 2.91 | yes 1290 | 24.7 | None | 364.8 |
| Oracle 26ai Free | none | single-node | - | - | - | - |
| PostgreSQL 18 | physical streaming (async, slot) | 0.85 / 2.60 / 2.79 | yes 25006 | 3.2 | 1.03 | 511.9 |
| Percona XtraDB Cluster 8.4 (Galera) | Galera (virtually synchronous multi-master) | 0.71 / 14 / 349 | no | 67.9 | 1.05 | 6261.4 |
| QuestDB | none | single-node | - | - | - | - |
| SQLite 3 | none | single-node | - | - | - | - |
| TiDB 8.5 | Raft (TiKV, 3 replicas per region), stateless SQL layer | 1.69 / 2.03 / 2.13 | no | 22.0 | 0.98 | 633.5 |
| YugabyteDB | Raft per tablet, RF=3 (multi-active) | 1.71 / 1.91 / 53 | no | 85.5 | 0.82 | None |

## Multi-connection bulk-insert load test (rows/s; batch size and duration per engine in latest.json)

| engine | mode | entry point | connections | rows/s | batch p50 ms | errors | busiest container (avg cores during inserts) |
|---|---|---|---:|---:|---:|---:|---|
| Citus 13 (PG 18) sharded, 5x2 | values | primary | 4 | 11867 | 315 | 0 | citus-coordinator 0.77 |
| Citus 13 (PG 18) sharded, 5x2 | values | primary | 16 | 9733 | 1.60 s | 0 | citus-coordinator 0.86 |
| Citus 13 (PG 18) sharded, 5x2 | values | primary | 64 | 8333 | 9.60 s | 0 | citus-coordinator 1.18 |
| Citus 13 (PG 18) sharded, 5x2 | values | pgbouncers | 4 | 12067 | 327 | 0 | citus-coordinator 0.81 |
| Citus 13 (PG 18) sharded, 5x2 | values | pgbouncers | 16 | 10467 | 1.51 s | 0 | citus-coordinator 0.85 |
| Citus 13 (PG 18) sharded, 5x2 | values | pgbouncers | 64 | 12133 | 6.80 s | 0 | citus-coordinator 1.08 |
| Citus 13 (PG 18) sharded, 5x2 | copy | primary | 4 | 13733 | 281 | 0 | citus-coordinator 0.72 |
| Citus 13 (PG 18) sharded, 5x2 | copy | primary | 16 | 13667 | 1.21 s | 0 | citus-coordinator 0.78 |
| Citus 13 (PG 18) sharded, 5x2 | copy | primary | 64 | 13200 | 5.70 s | 0 | citus-w1 0.95 |
| Citus 13 (PG 18) sharded, 5x2 | copy | pgbouncers | 4 | 13000 | 301 | 0 | citus-coordinator 0.69 |
| Citus 13 (PG 18) sharded, 5x2 | copy | pgbouncers | 16 | 14067 | 1.19 s | 0 | citus-coordinator 0.80 |
| Citus 13 (PG 18) sharded, 5x2 | copy | pgbouncers | 64 | 15600 | 5.29 s | 0 | citus-w1 1.06 |
| CrateDB | values | primary | 4 | 75067 | 49 | 0 | crate-1 4.58 |
| CrateDB | values | primary | 16 | 133267 | 87 | 0 | crate-1 10.34 |
| CrateDB | values | primary | 32 | 155667 | 175 | 0 | crate-1 11.55 |
| IBM Db2 12.1 CE | values | primary | 4 | 47200 | 77 | 0 | db2 1.09 |
| IBM Db2 12.1 CE | values | primary | 16 | 29333 | 315 | 4 | db2 1.03 |
| IBM Db2 12.1 CE | values | primary | 32 | 5000 | 7.18 s | 6 | db2 0.23 |
| Firebird 5 | values | primary | 4 | 4000 | 1.02 s | 0 | firebird 1.00 |
| Firebird 5 | values | primary | 16 | 21333 | 591 | 0 | firebird 2.73 |
| Firebird 5 | values | primary | 32 | 19200 | 1.67 s | 0 | firebird 3.21 |
| H2 2.x | values | primary | 4 | 52267 | 71 | 0 | h2 2.10 |
| H2 2.x | values | primary | 16 | 52800 | 296 | 0 | h2 3.05 |
| H2 2.x | values | primary | 32 | 42467 | 743 | 0 | h2 3.20 |
| SQL Server 2025 | values | primary | 4 | 13400 | 84 | 0 | mssql-1 3.45 |
| SQL Server 2025 | values | primary | 16 | 32620 | 122 | 0 | mssql-1 10.79 |
| SQL Server 2025 | values | primary | 32 | 5920 | 1.79 s | 0 | mssql-1 3.21 |
| Oracle 26ai Free | values | primary | 4 | 19267 | 54 | 0 | oracle 1.12 |
| Oracle 26ai Free | values | primary | 16 | 18067 | 621 | 0 | oracle 1.63 |
| Oracle 26ai Free | values | primary | 32 | 17600 | 1.41 s | 0 | oracle 1.93 |
| Percona XtraDB Cluster 8.4 (Galera) | values | primary | 4 | 44933 | 39 | 0 | pxc-1 1.44 |
| Percona XtraDB Cluster 8.4 (Galera) | values | primary | 16 | 44600 | 233 | 0 | pxc-1 1.95 |
| Percona XtraDB Cluster 8.4 (Galera) | values | primary | 32 | 61533 | 223 | 0 | pxc-1 2.55 |
| Percona XtraDB Cluster 8.4 (Galera) | values | haproxy | 4 | 14133 | 59 | 0 | pxc-2 1.48 |
| Percona XtraDB Cluster 8.4 (Galera) | values | haproxy | 16 | 30467 | 101 | 0 | pxc-3 1.40 |
| Percona XtraDB Cluster 8.4 (Galera) | values | haproxy | 32 | 32667 | 163 | 0 | pxc-1 1.51 |
| TiDB 8.5 | values | primary | 4 | 34267 | 88 | 0 | tidb-1 1.88 |
| TiDB 8.5 | values | primary | 16 | 48533 | 254 | 0 | tidb-1 2.58 |
| TiDB 8.5 | values | primary | 32 | 52133 | 436 | 0 | tidb-1 2.59 |
| YugabyteDB | values | primary | 4 | 35733 | 111 | 0 | yb-1 3.28 |
| YugabyteDB | values | primary | 16 | 81667 | 188 | 0 | yb-1 6.24 |
| YugabyteDB | values | primary | 32 | 67667 | 391 | 0 | yb-1 6.38 |

## Backup & recovery drills (backup -> restore elsewhere -> fingerprint of every table; PITR = point-in-time / incremental probe)

| engine | strategy | kind | tool | backup s | size MB | restore s | verified | PITR / probe | notes |
|---|---|---|---|---:|---:|---:|:---:|:---:|---|
| Citus 13 (PG 18) sharded, 5x2 | `pg_dump` | logical | pg_dump -Fd -j | pg_restore -j | 4.22 | 33.0 | 9.94 | ✅ | · | logical backup of the whole cluster via the coordinator; the restore yields plain local tables (re-run create_distributed_table/create_refer |
| Citus 13 (PG 18) sharded, 5x2 | `cluster_pitr` | pitr | pg_basebackup per node + citus_create_restore_point + recovery_target_name | 10.51 | 564.4 | 8.26 | ✅ | ✅ | pg_basebackup of 6 primaries + per-node WAL archives; citus_create_restore_point gives one consistent recovery target; the 6 nodes restored  |
| ClickHouse | `backup_disk` | physical | BACKUP DATABASE TO Disk('backups', ...) | 0.07 | 45.9 | 0.23 | ✅ | · | single-replica backup of every table's parts + metadata into the `backups` disk (/backups); restored under a new database name on the same s |
| ClickHouse | `backup_cluster` | physical | BACKUP DATABASE ON CLUSTER | 0.58 | 45.9 | 0.62 | ✅ | · | cluster-coordinated backup (each shard once, the replicas split the parts); drill: DROP TABLE ... ON CLUSTER then RESTORE TABLE ... ON CLUST |
| ClickHouse | `incremental` | incremental | BACKUP ... SETTINGS base_backup | 0.08 | 0.1 | 0.22 | ✅ | ✅ | only parts not present in the base backup are written; RESTORE from the incremental resolves the base automatically |
| CockroachDB | `full` | physical | BACKUP DATABASE INTO 'nodelocal://1/lab' WITH revision_history | 3.42 | 73.6 | 3.94 | ✅ | · | distributed backup job (every node exports its ranges as SSTs) with MVCC revision history; restored under a new database name |
| CockroachDB | `incremental` | incremental | BACKUP DATABASE INTO LATEST IN | 0.47 | 0.1 | 5.62 | ✅ | ✅ | incremental layer appended to the same collection (INTO LATEST IN); RESTORE FROM LATEST applies full + incrementals |
| CockroachDB | `pitr` | pitr | RESTORE ... AS OF SYSTEM TIME | 0.38 | 0.1 | 3.14 | ✅ | ✅ | revision_history keeps every MVCC version in the backup chain; RESTORE ... AS OF SYSTEM TIME picks the state before the DELETE |
| CrateDB | `snapshot` | snapshot | CREATE REPOSITORY fs + CREATE SNAPSHOT ALL / RESTORE SNAPSHOT | 1.75 | 146.2 | 2.22 | ✅ | · | fs repository on the shared volume; snapshots copy Lucene segments (compressed); RESTORE ... schema_rename_replacement restores into another |
| CrateDB | `incremental` | incremental | second CREATE SNAPSHOT (new segments only) | 0.18 | 0.1 | 0.18 | ✅ | ✅ | a second snapshot into the same repository only adds new segment files (incremental by construction) |
| IBM Db2 12.1 CE | `offline_full` | physical | BACKUP DATABASE ... COMPRESS (offline) / RESTORE ... INTO | 8.52 | 160.1 | 10.51 | ✅ | · | offline (no connections) compressed full image; restored into a new database on the same instance (rollforward complete only needed once the |
| IBM Db2 12.1 CE | `online_incremental` | incremental | LOGARCHMETH1 + BACKUP ONLINE INCREMENTAL + RESTORE INCREMENTAL AUTOMATIC | 5.39 | 427.3 | 16.22 | ✅ | ✅ | archive logging (LOGARCHMETH1 DISK) enables online backups; TRACKMOD ON makes incremental images possible; manual chain restore: target imag |
| IBM Db2 12.1 CE | `pitr` | pitr | ARCHIVE LOG + ROLLFORWARD ... TO <timestamp> | 2.38 | 0.9 | 13.22 | ✅ | ✅ | archived logs (ARCHIVE LOG forces the switch) replayed on the restored chain up to the timestamp before the DELETE |
| DuckDB | `export_database` | logical | EXPORT DATABASE (FORMAT parquet) / IMPORT DATABASE | 0.189 | 25.0 | 6.233 | ✅ | · | schema.sql + load.sql + one zstd parquet file per table: portable across DuckDB versions; IMPORT DATABASE replays it |
| DuckDB | `copy_from_database` | physical | ATTACH + COPY FROM DATABASE | 7.32 | 90.5 | 1.685 | ✅ | · | schema copied with COPY FROM DATABASE (SCHEMA), rows inserted per table in FK order (plain COPY FROM DATABASE violates foreign keys because  |
| Firebird 5 | `gbak` | logical | gbak -b / gbak -c | 6.4 | 40.1 | 8.81 | ✅ | · | transportable format (any platform / version), garbage collected on the way; the restore rebuilds the database and its indexes |
| Firebird 5 | `nbackup` | incremental | nbackup -B 0/1 + nbackup -R | 0.58 | 75.4 | 0.23 | ✅ | ✅ | page-level physical backup with incremental levels (only pages changed since the previous level); restore merges the chain |
| H2 2.x | `backup_zip` | physical | BACKUP TO 'lab.zip' + org.h2.tools.Restore | 29.19 | 150.2 | 7.68 | ✅ | · | online zip of the .mv.db file taken by the server; the Restore tool unpacks it under a new database name |
| H2 2.x | `script` | logical | SCRIPT TO / RUNSCRIPT FROM | 5.81 | 151.2 | 19.57 | ✅ | · | SQL script (DDL + INSERTs) replayed into a fresh database through the same connection type |
| MariaDB 11 | `mariadb_dump` | logical | mariadb-dump --single-transaction | 1.87 | 147.6 | 27.29 | ✅ | · | single-transaction snapshot with the GTID position recorded in the dump header |
| MariaDB 11 | `mariadb_backup` | physical | mariadb-backup full + incremental, --prepare | 3.91 | 726.9 | 4.78 | ✅ | ✅ | physical hot backup + incremental chain (--prepare, then --prepare --incremental-dir), started in a scratch container; PITR by GTID replicat |
| MariaDB 11 | `flashback` | flashback | mariadb-binlog --flashback | 0.087 | 0.0 | 0.142 | ✅ | ✅ | ROW-format binlog events of the bad transaction inverted with --flashback and replayed on the primary (no restore needed) |
| MonetDB | `msqldump` | logical | msqldump | mclient | 5.08 | 138.7 | 20.98 | ✅ | · | SQL dump with COPY INTO blocks; the new database is created in the same dbfarm with the monetdb control tool |
| MonetDB | `hot_snapshot` | physical | CALL sys.hot_snapshot(tar) | 0.48 | 107.1 | 0.19 | ✅ | · | server-side consistent tar of the database directory (no client round trips); restore = untar under a new name in the dbfarm |
| SQL Server 2025 | `full` | physical | BACKUP DATABASE WITH COMPRESSION, CHECKSUM + RESTORE VERIFYONLY | 16.34 | 689.8 | 26.0 | ✅ | · | compressed, checksummed full backup verified with RESTORE VERIFYONLY and restored under a new name (WITH MOVE); history in msdb.dbo.backupse |
| SQL Server 2025 | `differential` | incremental | BACKUP DATABASE WITH DIFFERENTIAL | 8.01 | 460.8 | 50.15 | ✅ | ✅ | extents changed since the last full backup (differential bitmap); restore = full WITH NORECOVERY then the differential |
| SQL Server 2025 | `log_pitr` | pitr | BACKUP LOG + RESTORE LOG WITH STOPAT | 11.09 | 460.0 | 66.2 | ✅ | ✅ | FULL recovery model: the log backup carries every transaction; RESTORE LOG ... WITH STOPAT replays up to the timestamp before the DELETE |
| MySQL 9 | `mysqlsh_dump` | logical | mysqlsh util dump-tables / load-dump | 4.67 | 31.5 | 20.14 | ✅ | · | MySQL Shell dump utility: 4 threads, zstd chunks, loaded with LOAD DATA LOCAL INFILE into a second schema |
| MySQL 9 | `mysqldump` | logical | mysqldump --single-transaction | 3.16 | 146.0 | 39.71 | ✅ | · | single-transaction consistent snapshot, plain SQL restored serially with the mysql client |
| MySQL 9 | `clone` | physical | CLONE LOCAL DATA DIRECTORY + START REPLICA UNTIL SQL_BEFORE_GTIDS | 3.38 | 426.3 | 2.17 | ✅ | ✅ | clone plugin physical hot copy (no external tool), scratch instance replays the source binlog with GTID auto-positioning and stops before th |
| Oracle 26ai Free | `datapump` | logical | expdp / impdp REMAP_SCHEMA | 80.42 | 115.2 | 46.82 | ✅ | · | Data Pump schema export through a directory object on the shared volume, imported into another schema (REMAP_SCHEMA) |
| Oracle 26ai Free | `rman` | physical | RMAN BACKUP DATABASE PLUS ARCHIVELOG | n/a | | | · | · | RMAN binary is not part of gvenzl/oracle-free:*-slim*; with the full image: `ALTER DATABASE ARCHIVELOG` (mount), `RMAN> CONFIGURE CONTROLFIL |
| Oracle 26ai Free | `flashback` | flashback | AS OF TIMESTAMP / FLASHBACK TABLE / TO BEFORE DROP | 0.0 | 0.0 | 0.633 | ✅ | ✅ | undo-based: Flashback Query reads the old version, FLASHBACK TABLE TO SCN rewrites the current one, the recycle bin brings a dropped table b |
| PostgreSQL 18 | `pg_dump` | logical | pg_dump -Fd -j4 --compress=zstd | pg_restore -j4 | 2.74 | 31.7 | 10.57 | ✅ | · | directory format, 4 parallel jobs, zstd; globals dumped separately with pg_dumpall |
| PostgreSQL 18 | `pg_basebackup` | physical | pg_basebackup -Fp -Xs | 2.49 | 270.0 | 3.75 | ✅ | · | plain-format full copy with streamed WAL (-Xs), verified with pg_verifybackup; restored by copying into a scratch container |
| PostgreSQL 18 | `incremental` | incremental | pg_basebackup --incremental + pg_combinebackup | 0.61 | 20.7 | 2.16 | ✅ | ✅ | block-level incremental (summarize_wal=on, PG17+), restored with pg_combinebackup full incr -o PGDATA |
| PostgreSQL 18 | `pitr` | pitr | archive_command + restore_command + recovery_target_name | 0.0 | 688.0 | 4.82 | ✅ | ✅ | WAL archive (archive_command -> /backups/wal) replayed on top of the full backup up to the named restore point; the DELETE that followed is  |
| Percona XtraDB Cluster 8.4 (Galera) | `mysqldump` | logical | mysqldump --single-transaction | 3.15 | 146.0 | 61.05 | ✅ | · | single-transaction consistent snapshot, plain SQL restored serially with the mysql client |
| Percona XtraDB Cluster 8.4 (Galera) | `xtrabackup` | physical | xtrabackup 8.4 full + incremental | 6.28 | 376.2 | 1.65 | ✅ | ✅ | XtraBackup 8.4 full + incremental (--apply-log-only on the base, final prepare with --incremental-dir), started standalone (wsrep off) in a  |
| QuestDB | `checkpoint` | physical | CHECKPOINT CREATE + tar + _restore | 5.05 | 1259.5 | 7.23 | ✅ | · | consistent copy (cp -a of db/, conf/, .checkpoint/) while CHECKPOINT holds the table versions; the _restore trigger file makes the new insta |
| SQLite 3 | `vacuum_into` | logical | VACUUM INTO 'file' | 1.404 | 143.9 | 0.734 | ✅ | · | single SQL statement, consistent snapshot, output is compacted; restore = open the file |
| SQLite 3 | `backup_api` | physical | sqlite3_backup API | 1.01 | 184.6 | 0.693 | ✅ | · | sqlite3_backup API (page copy in steps, safe with concurrent writers); the mechanism Litestream/LiteFS build on |
| TiDB 8.5 | `br_full` | physical | BACKUP DATABASE ... TO 'local://' | 5.21 | 57.8 | 1.61 | ✅ | · | BR embedded in tidb-server; SSTs written by every TiKV into the shared volume; restore is in place (BR cannot rename), so the drill drops an |
| TiDB 8.5 | `br_incremental` | incremental | BACKUP ... LAST_BACKUP = ts | 3.54 | 0.0 | 0.31 | ✅ | ✅ | BACKUP ... LAST_BACKUP = <BackupTS> ships only KV changes after the full backup |
| TiDB 8.5 | `flashback` | flashback | FLASHBACK TABLE | 0.0 | 0.0 | 0.134 | ✅ | ✅ | no backup needed: the dropped table's data is still in TiKV MVCC history until GC (tidb_gc_life_time) |
| TiDB 8.5 | `pitr_cluster` | pitr | AS OF TIMESTAMP + FLASHBACK CLUSTER TO TIMESTAMP | 0.0 | 0.0 | 41.81 | ✅ | ✅ | whole-cluster in-place PITR from MVCC history (blocks reads/writes while running; only within tidb_gc_life_time) |
| YugabyteDB | `ysql_dump` | logical | ysql_dump | ysqlsh | 9.58 | 118.0 | 140.6 | ✅ | · | plain SQL dump (ysql_dump keeps YB-specific DDL such as SPLIT INTO); restored serially with ysqlsh |
| YugabyteDB | `snapshot` | snapshot | yb-admin create_database_snapshot / restore_snapshot | 4.98 | 0.1 | 1.43 | ✅ | ✅ | cluster-wide consistent snapshot (hybrid-time), restored in place; off-cluster copy = export_snapshot metadata + tablet snapshot files |
| YugabyteDB | `pitr` | pitr | yb-admin create_snapshot_schedule / restore_snapshot_schedule | 4.42 | 0.0 | 3.28 | ✅ | ✅ | snapshot schedule (1 min interval, 10 min retention) keeps history; restore_snapshot_schedule rolls the database back to the timestamp in pl |

## Logging (slow-query capture, audit trail, cost of logging every statement; container logs = Docker json-file with rotation)

| engine | slow-query sink | slow query captured (after s) | fast query filtered | audit mechanism | audit event found | full statement logging: qps baseline -> logged (overhead %) | bytes / statement |
|---|---|:---:|:---:|---|:---:|---|---:|
| Citus 13 (PG 18) sharded, 5x2 | jsonlog file (logging_collector) via log_min_duration_statement | ✅ (0.08) | ✅ | log_statement=ddl (pgaudit not bundled in the image) | ✅ | 2460 -> 1337 (45.7 %) | 1473.2 |
| ClickHouse | system.query_log (every query; slow = WHERE query_duration_ms >= thres | ✅ (1.07) | ✅ | system.query_log (user, client, query_kind, tables) + system | ✅ | 1784 -> 1764 (1.1 %) | 292.9 |
| CockroachDB | slow_query events (SQL_EXEC/SQL_PERF channels -> logs/cockroach-sql-ex | ✅ (0.67) | ✅ | SENSITIVE_ACCESS channel: sql.log.admin_audit + ALTER TABLE  | ✅ | 5260 -> 6049 (-15.0 %) | 1550.6 |
| CrateDB | sys.jobs_log (+ stats.jobs_log_persistent_filter -> JSON lines in the  | ✅ (0.07) | ✅ | sys.jobs_log (username, stmt, started, ended, error) | ✅ | 6943 -> 7302 (-5.2 %) | 184.0 |
| IBM Db2 12.1 CE | MON_GET_PKG_CACHE_STMT (package cache) + activity event monitor RDLAB_ | ✅ (0.1) | ✅ | db2audit policy RDLAB_POL (EXECUTE) -> db2audit archive/extr | ✅ | 1481 -> 1285 (13.2 %) | 242.0 |
| DuckDB | duckdb_logs() QueryLog (every statement; in-memory or CSV file storage | ✅ (0.0) | ❌ (no threshold) | QueryLog records every statement (DDL included) | ✅ | 1318 -> 1026 (22.2 %) | 50.0 |
| Firebird 5 | system audit trace (fbtrace.conf, time_threshold=100) -> /var/log/fire | ✅ (0.09) | ✅ | audit trace: log_connections + log_statement_prepare (every  | ✅ | 1872 -> 513 (72.6 %) | 948.5 |
| H2 2.x | INFORMATION_SCHEMA.QUERY_STATISTICS (SET QUERY_STATISTICS TRUE) + trac | ✅ (0.04) | ✅ | trace file level 2 (every statement, DDL included) | ✅ | 196 -> 196 (0.0 %) | 140.9 |
| MariaDB 11 | slow query log file (long_query_time) | ✅ (0.17) | ✅ | server_audit plugin (CONNECT, QUERY_DDL) -> audit.log | ✅ | 21010 -> 19607 (6.7 %) | 462.5 |
| MonetDB | sys.querylog_calls (CALL sys.querylog_enable(threshold_ms)) | ✅ (0.0) | ✅ | sys.querylog_catalog (query text + owner) with threshold 0 | ✅ | 7682 -> 7540 (1.8 %) | 200.0 |
| SQL Server 2025 | Extended Events session rdlab_slow (sql_statement_completed, duration  | ✅ (2.31) | ✅ | SQL Server Audit rdlab_audit (SCHEMA_OBJECT_CHANGE_GROUP, DE | ✅ | 13496 -> 12450 (7.8 %) | 264.9 |
| MySQL 9 | slow query log file (long_query_time) | ✅ (0.26) | ✅ | general log (no audit plugin in MySQL Community) | ✅ | 21543 -> 21739 (-0.9 %) | 638.6 |
| Oracle 26ai Free | session SQL trace (DBMS_SESSION.SESSION_TRACE_ENABLE -> .trc) + V$SQL  | ✅ (0.32) | ✅ | unified auditing: AUDIT POLICY rdlab_pol (CREATE/DROP TABLE, | ✅ | 11987 -> 9087 (24.2 %) | 9.2 |
| PostgreSQL 18 | jsonlog file (logging_collector) via log_min_duration_statement | ✅ (0.08) | ✅ | log_statement=ddl (pgaudit not bundled in the image) | ✅ | 26770 -> 23346 (12.8 %) | 1465.6 |
| Percona XtraDB Cluster 8.4 (Galera) | slow query log file (long_query_time) | ✅ (0.27) | ✅ | Percona audit_log plugin, JSON, DDL commands only -> audit.j | ✅ | 18473 -> 14539 (21.3 %) | 1002.7 |
| QuestDB | _query_trace table (query.tracing.enabled; slow = WHERE execution_micr | ✅ (0.01) | ✅ | _query_trace records every statement with its principal | ✅ | 16926 -> 17044 (-0.7 %) | 344.9 |
| SQLite 3 | application-side JSONL slow log (driver wrapper, RDLAB_SQLITE_SLOW_MS) | ✅ (0.45) | ✅ | n/a (no server; use the same wrapper or SQLite's sqlite3_tra | n/a | 320261 -> 89011 (72.2 %) | 172.7 |
| TiDB 8.5 | tidb-slow.log (log.slow-threshold) exposed as INFORMATION_SCHEMA.SLOW_ | ✅ (0.01) | ✅ | DDL job history (ADMIN SHOW DDL JOBS) + [ddl] records in the | ✅ | 8566 -> 9120 (-6.5 %) | 2035.9 |
| YugabyteDB | postgresql-*.log of the tserver (log_min_duration_statement via ysql_p | ✅ (0.08) | ✅ | ysql_log_statement=ddl (+ pgaudit extension available) | ✅ | 15326 -> 15555 (-1.5 %) | 484.1 |

## Notes per engine

- **Citus 13 (PG 18) sharded, 5x2**: Citus coordinator + 5 worker primaries + 5 streaming replicas (registered as Citus secondary nodes), every PostgreSQL instance limited to 0.8 CPU / 1.5 GiB. Clients enter through HAProxy -> 3 PgBouncer instances (transaction pooling) -> coordinator. Tables are hash-distributed on their primary keys (Citus needs PK/UNIQUE to contain the distribution column); products and categories are reference tables. Manual worker failover = promote replica + citus_update_node(). 
- **ClickHouse**: Two replicas of one shard using ReplicatedMergeTree with ClickHouse Keeper; DDL runs ON CLUSTER. No transactions, no foreign keys; secondary "indexes" are data-skipping (bloom filter) indexes; UPDATE/DELETE are asynchronous mutations. 
- **CockroachDB**: 3-node insecure cluster; every range replicated 3x via Raft, any node serves reads and writes (no promotion needed). HAProxy round-robins connections across nodes (the recommended production pattern). Insecure mode: no passwords. 
- **CrateDB**: 3-node cluster; tables are sharded and replicated (number_of_replicas default 0-1), eventually consistent reads (REFRESH TABLE makes writes visible), no transactions, no foreign keys, no recursive CTEs. Uses the HTTP endpoint. 
- **IBM Db2 12.1 CE**: Db2 Community Edition (privileged container, ~5 minutes first start). HADR (primary/standby log shipping) exists but needs two instances with matching configuration and a takeover procedure; not automated in this stack. 
- **DuckDB**: Embedded columnar/vectorised OLAP engine; single process (multiple cursors share one database handle), parallel query execution, no replication. 
- **Firebird 5**: Firebird 5 SuperServer. MVCC with SNAPSHOT / READ COMMITTED isolation, UPDATE OR INSERT and MERGE, no LATERAL/JSON. Built-in asynchronous replication (replication.conf + nbackup copy) exists since Firebird 4 but is not automated here. Loaded at scale 0.3 because firebird-driver executemany sends one row per round trip (a few thousand rows/s). 
- **H2 2.x**: H2 in server mode using its PostgreSQL-protocol emulation (MODE=PostgreSQL); accessed with psycopg. The PG emulation is partial (no COPY, limited pg_catalog), so loading uses executemany. Single node; H2 has no replication (clustering mode is deprecated). 
- **MariaDB 11**: MariaDB 11.8 LTS. Replica configured by the official image (MARIADB_MASTER_HOST) using GTID slave_pos; MaxScale readwritesplit routes SELECTs to the replica and performs automatic failover (mariadbmon) when the primary dies. 
- **MonetDB**: Column-store with full SQL:2003 transactions (optimistic concurrency). Bulk load via COPY INTO ... ON CLIENT. Replication: no built-in physical replication in the open-source release (monetdb replication is transaction-log based, experimental). 
- **SQL Server 2025**: SQL Server 2025 Developer on Linux (container image has no Full-Text Search component: q13 uses LIKE). Two replicas in a read-scale Availability Group (CLUSTER_TYPE = NONE): synchronous commit, automatic seeding, readable secondary; failover is a manual FORCE_FAILOVER_ALLOW_DATA_LOSS on the secondary. 
- **MySQL 9**: MySQL 9.x (innovation track). GTID-based asynchronous replication with SOURCE_AUTO_POSITION, replica in super_read_only; ProxySQL 3 splits SELECTs to the reader hostgroup (source+replica) and everything else to the source. 
- **Oracle 26ai Free**: Oracle AI Database 26ai Free, release 23.26.3 (the `23-slim-faststart` image; slim: no Oracle Text, so full-text search falls back to LIKE) in FREEPDB1 (user lab). Free edition caps: 2 CPU threads, 2 GB SGA/PGA, 12 GB data. Data Guard (physical standby) is not available in Free, so no replica in this stack. 
- **PostgreSQL 18**: Official postgres:18 image plus pgvector. Physical streaming replication (async) with a replication slot; PgBouncer in transaction mode with max_prepared_statements; HAProxy write (5000) and round-robin read (5001) endpoints. 
- **Percona XtraDB Cluster 8.4 (Galera)**: 3-node Galera cluster (MySQL 8.4 base): synchronous replication by write-set certification, every node writable; cross-node write conflicts surface as deadlock errors at COMMIT. HAProxy round-robins connections over the nodes. 
- **QuestDB**: Append-optimised time-series engine with a PostgreSQL wire endpoint. Tables with a designated timestamp are partitioned by month (WAL). No primary keys / foreign keys / UPDATE-heavy workloads; replication is an Enterprise feature. 
- **SQLite 3**: Embedded, single-writer database file (WAL mode, synchronous=NORMAL). No server, no network, no replication in core; libSQL / rqlite / Litestream provide replication on top of SQLite (see docs). 
- **TiDB 8.5**: MySQL-protocol distributed SQL: 2 stateless TiDB servers in front of 3 TiKV nodes (Raft, 3 replicas per region) and one PD. Any TiDB node serves reads and writes; killing a TiDB node only affects its own connections. 
- **YugabyteDB**: 3 nodes via yugabyted, replication factor 3 (Raft per tablet), PostgreSQL-compatible YSQL layer on every node (any node serves reads and writes; no promotion). Auth disabled for the lab. 
