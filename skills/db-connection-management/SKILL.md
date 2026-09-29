---
name: db-connection-management
description: Size, pool, route and harden database connections - per-engine connection limits and costs (PostgreSQL process-per-connection + SCRAM ~12 ms, MySQL thread ~2 ms), PgBouncer/pgcat/PgPool, ProxySQL, MaxScale, HAProxy, client pools (psycopg_pool, SQLAlchemy, HikariCP), timeouts, connection storms and "too many connections" error codes, read/write splitting, transaction conflict and deadlock error codes (40001, 40P01, 1213, 1205, 1020, 1290, 25006) and retry policy. Use for pool sizing, "too many clients", proxy choice, connection latency, or designing retry logic.
---

# Database connection management

## Facts measured in the rd-databases lab (Sept 2026, same host, Docker)
| engine | default max connections | connect + `SELECT 1` p50 | storm: opened/failed at limit+10 | error at the limit |
|---|---|---|---|---|
| PostgreSQL 18 | 100 (lab: 300) | 11.8 ms (fork + SCRAM-SHA-256) | 299/11 at 310 | `FATAL: sorry, too many clients already` -> SQLSTATE 53300 |
| PgBouncer (transaction mode, pool 20) | 5000 clients | 8.8 ms | 310/0 | queues instead of failing |
| MySQL 9.7 | 151 (lab: 1000) | 1.9 ms | 998/12 at 1010 | error 1040 `Too many connections` |
| MariaDB 11.8 | 151 (lab: 1000) | ~2 ms | 998/12 | 1040 |
| ProxySQL 3 | 4096 (`mysql-max_connections`) | 1.9 ms | 200/0 | -- |
| MaxScale 24 | per service `max_connections` | ~2 ms | -- | -- |
| CockroachDB 26 | unlimited (-1) | 2.8 ms | -- | -- |
| ClickHouse 26 (HTTP) | 4096 | 33 ms (client handshakes) | 4106/0 | 202 `TOO_MANY_SIMULTANEOUS_QUERIES` only under query load |
| MonetDB | 64 (`max_clients`) | 2.9 ms | -- | `too many connections` |
| QuestDB (PG wire) | 64 default (lab: 512 `QDB_PG_NET_CONNECTION_LIMIT`) | 1.1 ms | -- | connection refused/reset |
| Oracle 26ai Free (23.26) | `sessions` 322 (`processes` 200) | 41 ms (dedicated server process per session) | -- | ORA-00018 / ORA-00020 at the limit |
| IBM Db2 12.1 CE | unlimited (`max_connections -1`, agents) | 4.7 ms | 200/0 | SQL1226N |
| H2 2.3 (PostgreSQL protocol) | JVM-bound | 102 ms; every statement costs ~41 ms round trip (emulation overhead) | -- | -- |
| SQLite / DuckDB | n/a (in-process) | 0 | -- | `database is locked` (SQLite, single writer) |

Throughput vs worker processes (PK lookup, one connection per process): PostgreSQL 2.1k (1) -> 55k (16) -> 74k (64) QPS; via PgBouncer 24k max (single-threaded pooler, 20 server connections); MySQL 1.7k -> 44k -> 57k; MariaDB 56k at 64; via MaxScale 22k; CockroachDB 22k; Oracle Free 18.7k (2-thread licence cap); Db2 CE 24k; ClickHouse 3.1k (per-query overhead, not a point-lookup engine); QuestDB 54k; MonetDB 20k; H2 1.6k (41 ms per statement); SQLite 637k (16 processes, WAL); DuckDB ~1k (single process, threads contend); Citus coordinator capped at 0.8 CPU: 1.9k.

## Procedure
1. **Set the server limit and memory budget first.** PostgreSQL: `max_connections` x (`work_mem` x sorts + ~10 MB) must fit in RAM; MySQL: `max_connections` x per-thread buffers (`sort_buffer_size`, `join_buffer_size`, `read_buffer_size`...). Leave `superuser_reserved_connections`.
2. **Pool on the client** (always): psycopg `ConnectionPool(min_size, max_size, max_waiting, max_lifetime, max_idle)`, asyncpg `create_pool`, SQLAlchemy `QueuePool(pool_size, max_overflow, pool_timeout, pool_recycle, pool_pre_ping)`, HikariCP (`maximumPoolSize`, formula `cores * 2 + spindles`), node-postgres `Pool`, pgx `pgxpool`. Start with `pool_size = 2 x cores` of the *database* host divided by number of app instances; more connections than cores lowers throughput (the lab: 64 workers gave only 1.35x the QPS of 16 on 28 cores, with p99 rising from 0.5 to 2 ms).
3. **Add a server-side pooler when clients > ~200 or connections churn** (serverless, PHP, many pods): PgBouncer (transaction mode; needs `max_prepared_statements` for prepared statements, no session state), pgcat (sharding + read/write split), PgPool-II (load balancing, needs care), Supavisor; ProxySQL / MySQL Router / MaxScale for MySQL-family. Measure: PgBouncer capped the lab at 24k QPS vs 74k direct - size `default_pool_size` and run several pooler processes (`so_reuseport`) if you need more.
4. **Route reads** with a SQL-aware proxy (ProxySQL query rules `^SELECT` -> reader hostgroup, MaxScale readwritesplit, pgcat/pgpool `read_write_split`) or in the application (separate read pool to replicas, honouring replication lag). A TCP balancer (HAProxy round-robin) cannot split by statement: in the lab 50% of writes through the PostgreSQL round-robin endpoint failed with `25006 cannot execute UPDATE in a read-only transaction`; ProxySQL and MaxScale routed 100% of writes to the primary while serving 39/40 and 40/40 reads from the replica.
5. **Set timeouts everywhere**: server statement timeout (PG `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`; MySQL `max_execution_time` (SELECT only), `innodb_lock_wait_timeout` (default 50 s - the lab's SERIALIZABLE test blocked for the full 50 s), `wait_timeout`; MariaDB `max_statement_time`; SQL Server `LOCK_TIMEOUT` + client `CommandTimeout`; CockroachDB `statement_timeout`; ClickHouse `max_execution_time`; Firebird `SET STATEMENT TIMEOUT`) plus client connect timeout and TCP keepalives (`keepalives_idle=30` in libpq; `tcp_keepalive` in PgBouncer).
6. **Retry only transient errors, idempotently**, with jittered exponential backoff and a cap:
   - serialization/write conflicts: PostgreSQL/CockroachDB `40001` (`restart transaction` in CockroachDB), MariaDB `1020` (record changed since last read, 11.6+), DuckDB `TransactionContext Error: Conflict on update`, SQLite `SQLITE_BUSY_SNAPSHOT` ("database is locked" right after a read in an open transaction), Oracle `ORA-08177`, SQL Server `3960` (snapshot update conflict).
   - deadlocks: PostgreSQL `40P01` (detected after `deadlock_timeout` = 1 s), MySQL/MariaDB `1213` (immediate, 1-2 ms), SQL Server `1205`, Oracle `ORA-00060`, CockroachDB `40001`.
   - lock waits: MySQL `1205` (`innodb_lock_wait_timeout`), PostgreSQL `55P03` (`lock_timeout`), SQL Server `1222`.
   - connection-level: PostgreSQL `08xxx`, `57P01` (admin shutdown), `53300`; MySQL `2006`/`2013` (server gone/lost), `1040`; write to a replica: PostgreSQL `25006`, MySQL `1290` (`--read-only`), SQL Server `3906`.
   Retry the *whole transaction*, never a single statement inside it.
7. **Prove it with a storm test** (`rdlab phase <stack> connections --storms 50,200,1000`): open N connections concurrently, record connect-time percentiles and the error code at the limit, then repeat through the pooler.

## Engine notes
- PostgreSQL forks a backend per connection: ~5-10 MB each plus `work_mem` per sort/hash node; SCRAM auth is CPU-heavy by design (4096 iterations) - poolers and long-lived connections pay it once.
- MySQL uses a thread per connection (`thread_cache_size` reuses them) - cheap connects, but each idle connection still holds buffers; 8.0+ has no query cache; `caching_sha2_password` needs TLS or `GET_SOURCE_PUBLIC_KEY` for replication users.
- SQL Server has a worker-thread pool (`max worker threads`), connection cost is TLS; timeouts are client-side (`CommandTimeout`).
- Oracle dedicated server = process per session (`processes`/`sessions` parameters); use DRCP or shared servers for many short connections.
- CockroachDB/YugabyteDB: connections are cheap but each SQL node has a per-node limit; put HAProxy/pgbouncer in front and spread across nodes; YugabyteDB ships a built-in connection manager.
- ClickHouse: use few connections with big batches; HTTP keep-alive; `max_concurrent_queries` guards the server.
- PgBouncer transaction mode breaks session features: `SET`, temp tables, `LISTEN`, advisory locks, and prepared statements unless `max_prepared_statements > 0` (1.21+).

## Files
- `references/poolers.md` - configuration snippets used in the lab (PgBouncer env, ProxySQL cnf, MaxScale cnf, HAProxy) and what each one can and cannot do.
- `scripts/conn_storm.py` - open N connections concurrently against a stack target and report connect-time percentiles and failures.
