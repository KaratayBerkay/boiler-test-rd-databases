---
name: sql-query-optimization
description: Diagnose and fix slow SQL on any relational engine - capture the right EXPLAIN variant (PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, Db2, SQLite, DuckDB, ClickHouse, CockroachDB, TiDB, Firebird, MonetDB), read the plan for the five classic red flags, choose composite/covering/partial index columns, rewrite anti-patterns (non-sargable predicates, OFFSET pagination, correlated subqueries, NOT IN, OR across columns), refresh statistics, and prove the win with before/after timings. Use for "query is slow", "which index", "reading EXPLAIN ANALYZE", "plan regressed", "p99 latency". Contains measured before/after numbers from the rd-databases lab.
---

# SQL query optimization

## Procedure (works on every engine)
1. **Reproduce with numbers.** Time the statement 10+ times after 2 warm-ups with a monotonic clock and keep p50/p95 (see `sql-benchmark-timing`). One run is noise. In the lab: `uv run rdlab phase <stack> bench` or `scripts/explain.py`.
2. **Capture the actual plan** — the variant that executes the query and shows real row counts (`references/explain-per-engine.md`). Estimated plans lie when statistics are stale.
3. **Scan for the five red flags** (in this order):
   1. *Estimate vs actual rows off by >10x* → statistics: `ANALYZE` / `ANALYZE TABLE` / `UPDATE STATISTICS` / `DBMS_STATS.GATHER_TABLE_STATS` / `RUNSTATS`; raise the column's statistics target for skewed columns.
   2. *Full scan on a large table with a selective predicate* ("Seq Scan", `type=ALL`, "Table Scan", "TABLE ACCESS FULL", "SCAN TABLE") → index, or the predicate is not sargable.
   3. *Rows removed by filter >> rows returned* (PG "Rows Removed by Filter", MySQL `rows` vs `filtered`, SQL Server "Actual Number of Rows" >> output) → index does not cover the predicate: extend/reorder the index.
   4. *Sort or hash spilling* (PG "external merge Disk", "Batches: >1"; MySQL "Using filesort/temporary"; SQL Server spill warnings; Oracle "TEMP" usage) → index for ORDER BY, raise `work_mem`/`sort_buffer_size`, reduce the row width.
   5. *Nested loop with high loop counts* or *Key/RID lookups* → covering index or hash join; check join order.
4. **Fix one thing, re-measure, keep the plan.** Never stack five changes and call it done. Store `plan_before`, `plan_after`, `p50_before`, `p50_after`.
5. **Regression guard**: keep the plan text in the repo (`results/<engine>/plans/`), and re-run the timed query in CI against the same data scale.

## Index strategy (cross-engine)
- **Composite column order: equality predicates first, then the range column, then columns needed for ORDER BY.** Measured on `events` (1M rows, `event_type = ? AND occurred_at BETWEEN`): PostgreSQL no index 46 ms -> `(occurred_at, event_type)` 4.6 ms -> `(event_type, occurred_at)` 2.3 ms; MySQL 267 -> 17 -> 12 ms; SQLite 105 -> 7.6 -> 2.3 ms; CockroachDB 781 -> 107 -> 65 ms.
- **Covering indexes** avoid table lookups: PostgreSQL/SQL Server `INCLUDE (cols)`; MySQL/SQLite/Oracle put the payload columns at the end of the key. CockroachDB measured 41 ms -> 9.4 ms with `INCLUDE`; PostgreSQL showed no gain when the heap was already cached (index-only scans need a recent VACUUM for the visibility map).
- **Partial / filtered indexes** for hot subsets (PostgreSQL, SQLite, SQL Server `WHERE`, CockroachDB). Elsewhere a composite index with the filter column first is the substitute (MySQL 65 ms -> 12 ms with `(status, ordered_at)`).
- **Expression indexes** when the predicate must apply a function (`lower(email)`, `date(ts)`): PostgreSQL, SQLite, Oracle (function-based), MySQL 8 (functional index), SQL Server (computed column + index).
- **Do not index everything**: every index costs on writes (the lab's `order_items` load runs at half speed because of two FK checks and two secondary indexes on PostgreSQL).
- **Index types**: B-tree default; hash (PG, equality only); BRIN (PG, huge append-only tables); GIN (PG JSONB/full-text/arrays); GiST (ranges/geo); ClickHouse has only the sorting key plus data-skipping indexes (minmax/set/bloom_filter) - measured: bloom filter index on `events` gave no gain because the sorting key already prunes.
- **Hypothetical indexes** (PG `hypopg`, SQL Server `AUTO_CREATE_STATISTICS`/DTA, MySQL invisible indexes `ALTER TABLE ... ALTER INDEX ... INVISIBLE`) let you test without building.

## Rewrites with measured effect (lab, scale 1, warm cache, p50)
| anti-pattern | rewrite | PostgreSQL | MySQL 9 | SQLite | CockroachDB |
|---|---|---|---|---|---|
| `YEAR(ts)=2025 AND MONTH(ts)=5` (non-sargable) with index on ts | `ts >= '2025-05-01' AND ts < '2025-06-01'` | 28 ms -> 1.4 ms | 40 -> 3.5 ms | 116 -> 0.42 ms | 84 -> 10 ms |
| `OFFSET 100000 LIMIT 50` | keyset `(ts, id) > (:ts, :id)` with index `(ts, id)` | 45 ms -> 0.64 ms (row-value); the OR-expanded form stayed at 44 ms | 111 -> 9.3 ms (OR form; row-value form is *slower* on MySQL: 166 ms) | 4.7 -> 0.11 ms | 215 -> 3.3 ms |
| correlated scalar subquery per row | join to a pre-aggregated derived table | 12.5 -> 5.1 ms | 30 -> 17 ms | 7.2 -> 3.2 ms | 66 -> 24 ms |
| `LEFT JOIN ... IS NULL` anti-join | `NOT EXISTS` | 81 -> 12 ms | equal (63 ms; optimizer converts) | 36 -> 2.7 ms | equal |
| `NOT IN (subquery)` | `NOT EXISTS` | 99 -> 12 ms (and NULL-safe) | equal | equal | equal |
| 3000 single-row autocommit INSERTs | one transaction, multi-row `VALUES` (1000 rows/stmt) | 1.8k -> 20k rows/s | 0.4k -> 30k rows/s (driver `executemany` gave only 1k rows/s: PyMySQL only rewrites to multi-row when VALUES has *only* placeholders) | 6.7k -> 25k rows/s | 0.4k -> 6.3k rows/s |
| text-protocol point lookup | server-side prepared statement | 0.85 -> 0.55 ms (psycopg `prepare=True`; note psycopg auto-prepares after 5 executions) | n/a in PyMySQL | n/a | 1.78 -> 1.68 ms |

## Engine-specific notes that bite
- **PostgreSQL**: `EXPLAIN (ANALYZE, BUFFERS)`; CTEs are inlined since 12 unless `MATERIALIZED`; `plan_cache_mode` for prepared statements; `jit` can slow short queries; `random_page_cost=1.1` on SSD; `pg_stat_statements` for the top offenders; index-only scans need the visibility map (VACUUM).
- **MySQL 8/9**: `EXPLAIN ANALYZE` (TREE) gives actual rows; `EXPLAIN FORMAT=JSON` shows cost; `optimizer_switch`, `/*+ INDEX(t idx) */` hints; `performance_schema.events_statements_summary_by_digest` for aggregation; row-value comparison is poorly optimised (see table); `max_execution_time` per session.
- **MariaDB**: `ANALYZE FORMAT=JSON` = actual stats; `innodb_snapshot_isolation` (11.6+) rejects lost updates at REPEATABLE READ with error 1020 (MySQL silently overwrites).
- **SQL Server**: `SET STATISTICS XML ON` for the actual plan, `SET STATISTICS IO, TIME ON`; watch `Key Lookup`, implicit conversion warnings, parameter sniffing (`OPTION (RECOMPILE)`, `OPTIMIZE FOR`); Query Store for regressions.
- **Oracle**: `EXPLAIN PLAN FOR` + `DBMS_XPLAN.DISPLAY`; actual rows need `/*+ GATHER_PLAN_STATISTICS */` + `DBMS_XPLAN.DISPLAY_CURSOR(NULL,NULL,'ALLSTATS LAST')`; adaptive plans; bind peeking.
- **SQLite**: `EXPLAIN QUERY PLAN`; `ANALYZE` builds `sqlite_stat1`; the query planner uses at most one index per table per FROM item (except OR-optimisation); partial indexes and expression indexes supported.
- **DuckDB**: `EXPLAIN ANALYZE` (profiling); vectorised scans make most indexes irrelevant except for point lookups (ART index); `PRAGMA enable_profiling`.
- **ClickHouse**: `EXPLAIN indexes = 1`, `EXPLAIN PIPELINE`, `system.query_log` (`read_rows`, `read_bytes`, `memory_usage`) - choose the ORDER BY (sorting key) for the main filter; `PREWHERE`; avoid `SELECT *`; mutations (`ALTER ... UPDATE/DELETE`) are asynchronous and expensive.
- **CockroachDB**: `EXPLAIN ANALYZE` includes KV round trips and network latency; every statement is a distributed transaction (2.4 ms single-row insert vs 0.44 ms on PostgreSQL); use `AS OF SYSTEM TIME follower_read_timestamp()` for stale reads; secondary indexes are separate ranges.
- **TiDB**: `EXPLAIN ANALYZE` shows TiKV cop tasks; index lookups cost extra RPCs; `tidb_enable_1pc` for single-region writes.

## Files
- `references/explain-per-engine.md` - exact commands, how to get plans for parameterised statements, statistics refresh.
- `references/red-flags.md` - what the warning signs look like in each engine's plan text.
- `scripts/explain.py` - run a query through the lab harness against a running stack and print estimated + actual plans.
