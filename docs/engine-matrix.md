# Engine research matrix (Sept 2026)

Verified by pulling/running the image (R), by manifest check only (M), or documented but excluded (X). "version seen" is what the
engine reported in the last full run (2026-09-14, `results/SUMMARY.md` "Engines" table); tags without a pin (`latest`, `9`, `11`)
move, so re-check after a pull. Every R engine with a container also deploys on the k3s runtime (`docs/kubernetes.md`, 17/17 Ready).

| engine | image:tag | version seen | wire protocol | python driver | replication in lab | status |
|---|---|---|---|---|---|---|
| PostgreSQL | pgvector/pgvector:pg18 (= postgres:18) | 18.6 | PG | psycopg 3 | streaming physical, slot | R |
| Citus | citusdata/citus:latest | PG 18.4 + Citus 13 | PG | psycopg 3 | sharding + streaming replicas as secondaries | R |
| PgBouncer | edoburu/pgbouncer:latest | 1.24 | PG | - | pooler | R |
| MySQL | mysql:9 | 9.7.2 | MySQL | PyMySQL | GTID async | R |
| ProxySQL | proxysql/proxysql:latest | 3.0.11 | MySQL | - | read/write split | R |
| MariaDB | mariadb:11 | 11.8.9 | MySQL | PyMySQL | GTID async + MaxScale 24.02.9 auto-failover | R |
| Percona XtraDB Cluster | percona/percona-xtradb-cluster:8.4 | 8.4.10 | MySQL | PyMySQL | Galera 3-node | R |
| TiDB | pingcap/{pd,tikv,tidb}:v8.5.8 | 8.5.8 | MySQL | PyMySQL | Raft RF3 | R |
| CockroachDB | cockroachdb/cockroach:latest | v26.2.6 | PG | psycopg 3 | Raft RF3 | R |
| YugabyteDB | yugabytedb/yugabyte:latest | 2026.1.1.2 | PG | psycopg 3 | Raft RF3 | R |
| SQL Server | mcr.microsoft.com/mssql/server:2025-latest | 2025 RTM-CU8 (17.0.4085) | TDS | pymssql | read-scale AG | R |
| Oracle | gvenzl/oracle-free:23-slim-faststart | 26ai Free 23.26.3 (banner "Oracle AI Database 26ai Free"; the 23 tag ships the rebranded build) | TNS | python-oracledb thin | none (Free) | R |
| IBM Db2 | icr.io/db2_community/db2:latest | 12.1.5 CE | DRDA | ibm_db | none automated | R |
| ClickHouse | clickhouse/clickhouse-server:latest + clickhouse-keeper | 26.8.2 | HTTP/native | clickhouse-connect | ReplicatedMergeTree | R |
| CrateDB | crate:latest | 6.4.4 | HTTP / PG | crate | sharded replicas | R |
| QuestDB | questdb/questdb:latest | 10.0.1 | PG / HTTP / ILP | psycopg 3 + HTTP import | enterprise only | R |
| MonetDB | monetdb/monetdb:latest | Dec2025 (11.55.7) | MAPI | pymonetdb | none | R |
| Firebird | firebirdsql/firebird:5 | 5.0 (LI-V6.3.4.1812) | FB wire | firebird-driver (+libfbclient) | built-in async (manual) | R |
| H2 | built: eclipse-temurin:21-jre + h2 2.3.232 | 2.3.232 | PG emulation / TCP | psycopg 3 | none | R |
| SQLite | python stdlib | 3.45.1 | in-process | sqlite3 | none (libSQL/rqlite/Litestream external) | R |
| DuckDB | python package | 1.5.5 | in-process | duckdb | none | R |
| OrioleDB, Supabase Postgres (latest tag), Bitnami Galera/PgBouncer/PgPool | tags missing on mirror or moved to bitnamilegacy | | | | | M/X |
| Vertica CE, Exasol, SingleStore, SAP HANA Express, Actian Ingres, Altibase, InterSystems IRIS | licence key / registration / privileged / no public tag | | | | | X |
| Apache Doris, StarRocks, Databend, Apache Cloudberry (Greenplum) | analytical MPP; tags absent on mirror or multi-GB images; out of time budget | | | | | X |
| Materialize, RisingWave, TDengine, InfluxDB 3, YDB, OceanBase, MatrixOne, Dolt, Vitess, libSQL, rqlite | not relational-OLTP focus or too heavy for this pass; images exist (see docs/image-availability-mirror.txt) | | | | | X |
