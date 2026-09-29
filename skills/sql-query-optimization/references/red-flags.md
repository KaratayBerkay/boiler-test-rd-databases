# Red flags by engine (what to grep for in plan output)

## PostgreSQL
- `Seq Scan on <big table>` with `Filter:` and large `Rows Removed by Filter` - missing/unsuitable index.
- `Index Scan` with `Filter` (not `Index Cond`) - index used only for part of the predicate; the OR-expanded keyset predicate showed `Filter: ((ordered_at > ...) OR ...)` and `Rows Removed by Filter: 100044` vs `Index Cond: (ROW(ordered_at, id) > ROW(...))` for the row-value form.
- `rows=1` estimated vs `rows=50000` actual - stale statistics or correlated columns (`CREATE STATISTICS`).
- `Sort Method: external merge  Disk: NNNkB`, `Hash ... Batches: 8` - `work_mem` too small for this query.
- `Nested Loop ... loops=100000` - per-row index probes; fine for small outer sets, otherwise hash join.
- `Heap Fetches: N` on an Index Only Scan - visibility map stale (VACUUM).
- `SubPlan` (not `InitPlan`/hashed) - correlated subquery executed per row.
- `JIT:` with high `Generation`/`Inlining` time on short queries - set `jit = off`.

## MySQL / MariaDB
- `type: ALL` (full scan), `Extra: Using filesort`, `Using temporary`, `Using where` without `Using index`.
- `rows` large with `filtered` small - index does not cover the predicate.
- `Using index condition` (ICP) is good; `Range checked for each record` is bad.
- TREE plan: `Table scan on events (cost=...) (actual time=... rows=1e6 loops=1)` next to a tiny output - filter pushed too late.
- `DEPENDENT SUBQUERY` - correlated subquery per outer row.

## SQL Server
- `Table Scan` / `Clustered Index Scan` on large tables; `Key Lookup` / `RID Lookup` with high executions; `Sort` or `Hash Match` with spill warnings (`SpillToTempDb`); `Implicit conversion` warnings (`PlanAffectingConvert`); `Parallelism` on OLTP queries; big gap between `EstimateRows` and `ActualRows` (parameter sniffing or stale stats); `MissingIndex` element.

## Oracle
- `TABLE ACCESS FULL` on large tables; `E-Rows` vs `A-Rows` misestimates; `NESTED LOOPS` with large `Starts`; `TEMP` spills; `HASH JOIN` with `1 - filter` on the wrong side; `INDEX SKIP SCAN`/`FULL SCAN` where a range scan was expected.

## Db2
- `TBSCAN` on large tables; `FETCH` after `IXSCAN` (non-covering); `SORT` with large `TOTAL_COST`; `NLJOIN` with high inner cardinality.

## SQLite
- `SCAN TABLE t` (no index) vs `SEARCH TABLE t USING INDEX`; `USE TEMP B-TREE FOR ORDER BY` (missing ORDER BY index); `CORRELATED SCALAR SUBQUERY`.

## DuckDB
- `SEQ_SCAN` with a large filter is normal (vectorised); watch `HASH_JOIN` build side size, `ORDER_BY` on huge inputs, and `CROSS_PRODUCT`.

## ClickHouse
- `ReadFromMergeTree` reading all parts/granules (`Granules: N/N` after `PrimaryKey`) - filter not on the sorting key; `PREWHERE` absent; `Aggregating` with high `memory_usage`; `Distributed` queries without `GLOBAL IN`.

## CockroachDB / YugabyteDB / TiDB
- `full scan` / `FULL SCAN` on large tables; high `KV rows decoded` vs rows returned; many `KV round trips`; `index join` (CockroachDB's key lookup); TiDB `TableFullScan`, `IndexLookUp` with many `cop tasks`.
