---
name: sql-benchmark-timing
description: Measure database query latency and throughput correctly - warmup, monotonic clocks, p50/p95/p99 instead of averages, client vs server time, cold vs warm cache, multi-process load generators (Python threads cap at ~3k QPS because of the GIL), result checksums, and the standard tools (rdlab harness, pgbench, sysbench, hyperfine, clickhouse-benchmark, HammerDB, BenchBase, go-tpc, TPC-H via DuckDB). Use when comparing engines, indexes, query rewrites, connection poolers or replicas, or when someone quotes a single-run timing.
---

# SQL benchmark timing

## Non-negotiables
1. **Warm up, then sample.** Discard at least 2 executions (plan cache, buffer cache, JIT), then take >= 10 timed runs; report p50 and p95, keep the raw samples. The lab's `rdlab.timing.measure()` does exactly this and stores `first_ms` (cold-ish) separately.
2. **Monotonic clock around the whole round trip** (`time.perf_counter_ns()`); wall-clock (`time.time()`) can jump.
3. **Checksum the result** so the "fast" variant is not returning fewer rows (`rdlab.util.checksum_rows`). Every lab query records `rows` + `checksum`; a rewrite must keep the checksum.
4. **Separate client time from server time** when they disagree: PostgreSQL `EXPLAIN ANALYZE` "Execution Time" vs client p50 (the difference is protocol + result transfer + driver decode); MySQL `performance_schema` `TIMER_WAIT`; ClickHouse `system.query_log.query_duration_ms`; SQL Server `SET STATISTICS TIME ON`.
5. **One engine at a time on a shared host.** The lab runs stacks sequentially; a concurrent DuckDB run will steal all 28 cores from a running MySQL benchmark.
6. **Load generators must be multi-process.** Python threads plateaued at 2.7k QPS against PostgreSQL; 16 forked processes reached 55k QPS and 64 processes 74k QPS on the same box (`rdlab/workers.py`). Use processes (or Go/Rust/pgbench) for throughput; threads are fine for measuring latency of one stream.
7. **State the cache condition.** Warm (default), or cold: restart the container, `echo 3 > /proc/sys/vm/drop_caches` (root), `pg_prewarm`/`pg_buffercache` to inspect; SQL Server `DBCC DROPCLEANBUFFERS`; MySQL restart (the InnoDB buffer pool is dumped/reloaded on restart unless disabled).
8. **Report the environment**: engine version, image tag, memory/CPU limits, storage (Docker volume on NVMe here), fsync settings (`synchronous_commit`, `innodb_flush_log_at_trx_commit`, `sync_binlog`), data scale, driver + protocol (text vs prepared).

## Interpreting the lab's numbers
- Single-row write p50 is dominated by commit durability: PostgreSQL 0.5 ms, MySQL 1.2 ms (double fsync: redo + binlog), CockroachDB 2.4 ms (Raft), MariaDB 0.5 ms, SQLite (WAL, synchronous=NORMAL) 0.15 ms.
- Batching: 1-row autocommit vs 1000-row multi-row VALUES in one transaction: PostgreSQL 1.8k -> 20k rows/s, MySQL 0.4k -> 30k, MariaDB 1.6k -> 34k, SQLite 6.7k -> 25k, CockroachDB 0.4k -> 6.3k, ClickHouse 12 -> 2.8k (never insert row-by-row into ClickHouse; each INSERT creates a part).
- Bulk load paths and measured rates for 1M rows: PostgreSQL `COPY` 129k rows/s (order_items with 2 FKs: 42k), MySQL `LOAD DATA LOCAL INFILE` 71-121k, DuckDB `read_csv` 411k, QuestDB HTTP `/imp` 640k, MonetDB `COPY INTO ... ON CLIENT` 388k, ClickHouse native insert 57k (HTTP client), CockroachDB `COPY` 13k, SQLite executemany 36k.
- Connection cost: PostgreSQL 11.8 ms (process fork + SCRAM-SHA-256), PgBouncer 8.8 ms, MySQL 1.9 ms, CockroachDB 2.8 ms, QuestDB 1.1 ms, ClickHouse via clickhouse-connect 33 ms (several handshake round trips). This is why pools matter.

## Tools (all verified to exist; pick by engine)
| tool | engines | what it measures | invocation sketch |
|---|---|---|---|
| **rdlab** (this repo) | 20 engines | capability matrix, 30 catalog queries p50/p95, optimisation experiments, connection storms, replication lag, failover | `uv run rdlab run postgres --failover` |
| **pgbench** | PostgreSQL, CockroachDB, Yugabyte (`-M prepared`) | TPC-B-like TPS, custom scripts `-f`, latency percentiles | `pgbench -i -s 50 db; pgbench -c 32 -j 8 -T 60 -M prepared -P 5 db` |
| **sysbench** | MySQL family, PostgreSQL | OLTP point select / read-write, per-interval QPS and p95 | `sysbench oltp_read_write --mysql-host=... --tables=16 --table-size=1000000 prepare; ... --threads=32 --time=60 --report-interval=5 run` |
| **hyperfine** | any CLI (psql/mysql/sqlite3/duckdb) | end-to-end command latency with warmup and exports | `hyperfine --warmup 3 --runs 20 --export-json out.json 'psql -f q.sql'` (installed at `~/.local/bin/hyperfine`) |
| **clickhouse-benchmark** | ClickHouse | QPS, percentiles for one query | `clickhouse-benchmark -c 8 -i 1000 --query "SELECT ..."` |
| **HammerDB** | Oracle, SQL Server, Db2, MySQL, MariaDB, PostgreSQL | TPROC-C (TPC-C like) NOPM/TPM, TPROC-H | CLI `hammerdbcli auto script.tcl` |
| **BenchBase** (CMU) | any JDBC engine | TPC-C, TPC-H, YCSB, Wikipedia, Twitter ... | `java -jar benchbase.jar -b tpcc -c config.xml --create=true --load=true --execute=true` |
| **go-tpc** | MySQL/TiDB, PostgreSQL | TPC-C / TPC-H / CH-benCHmark | `go-tpc tpcc -H 127.0.0.1 -P 4000 -D test -T 8 prepare / run` |
| **DuckDB tpch/tpcds** | any engine (data generator) | generate TPC-H/TPC-DS data at a chosen scale factor | `INSTALL tpch; LOAD tpch; CALL dbgen(sf=1); COPY lineitem TO 'lineitem.csv'` |
| **k6 + xk6-sql** | MySQL/PostgreSQL/SQLite | HTTP-style load tests over SQL | needs a custom k6 build |
| **pt-query-digest** | MySQL slow log | aggregate by digest | `pt-query-digest slow.log` |

## Output format recommendation
Store one JSON per run: `{engine, version, image, scale, started, phases: {bench: {qid: {p50, p95, p99, n, rows, checksum, plan}}}}` (see `results/<engine>/latest.json`) and derive Markdown tables from it (`uv run rdlab report`). Never hand-edit numbers into docs.

## Files
- `references/methodology.md` - the checklist expanded, with the pitfalls found while building the lab.
- `scripts/bench_query.py` - time one SQL statement against a running stack with warmup/percentiles/checksum.
