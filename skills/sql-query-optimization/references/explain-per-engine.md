# EXPLAIN / plan capture per engine

| engine | estimated plan | actual plan (executes the query) | statistics refresh | plan cache / history |
|---|---|---|---|---|
| PostgreSQL / YugabyteDB | `EXPLAIN (FORMAT TEXT\|JSON) q` | `EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT TEXT) q` (Yugabyte: `EXPLAIN (ANALYZE, DIST)`) | `ANALYZE t`; `ALTER TABLE t ALTER COLUMN c SET STATISTICS 1000` | `pg_stat_statements` (total_exec_time, mean, calls, rows, shared_blks_*), `auto_explain` |
| CockroachDB | `EXPLAIN q`, `EXPLAIN (VERBOSE)` | `EXPLAIN ANALYZE q`, `EXPLAIN ANALYZE (DEBUG)` bundle | `ANALYZE t` / `CREATE STATISTICS` (automatic) | `crdb_internal.statement_statistics`, DB Console |
| MySQL 8/9 | `EXPLAIN q`, `EXPLAIN FORMAT=TREE q`, `EXPLAIN FORMAT=JSON q` | `EXPLAIN ANALYZE q` (TREE, actual rows and timings), `EXPLAIN ANALYZE FORMAT=JSON` (8.3+) | `ANALYZE TABLE t`; `ANALYZE TABLE t UPDATE HISTOGRAM ON c` | `performance_schema.events_statements_summary_by_digest`, slow log with `long_query_time=0` |
| MariaDB | `EXPLAIN q`, `EXPLAIN FORMAT=JSON` | `ANALYZE q`, `ANALYZE FORMAT=JSON q` (r_rows, r_filtered) | `ANALYZE TABLE t PERSISTENT FOR ALL` | same as MySQL |
| TiDB | `EXPLAIN q` | `EXPLAIN ANALYZE q` (execution info per operator, cop tasks) | `ANALYZE TABLE t` | `INFORMATION_SCHEMA.STATEMENTS_SUMMARY`, Dashboard |
| SQL Server | `SET SHOWPLAN_XML ON` then run | `SET STATISTICS XML ON` then run (actual plan as XML result set); `SET STATISTICS IO, TIME ON` for reads/CPU | `UPDATE STATISTICS t`; `sp_updatestats` | Query Store (`sys.query_store_runtime_stats`), `sys.dm_exec_query_stats` |
| Oracle | `EXPLAIN PLAN FOR q; SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY)` | run with `/*+ GATHER_PLAN_STATISTICS */` then `SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST'))`; `SET AUTOTRACE ON` in SQL*Plus | `DBMS_STATS.GATHER_TABLE_STATS(user, 'T')` | `v$sql` (elapsed_time/executions), AWR/ASH (Enterprise), SQL trace 10046 + tkprof |
| Db2 | `EXPLAIN PLAN FOR q` -> `EXPLAIN_OPERATOR`/`EXPLAIN_STREAM` tables, `db2exfmt` | activity event monitor (no simple per-statement actual plan) | `RUNSTATS ON TABLE s.t WITH DISTRIBUTION AND INDEXES ALL` | `MON_GET_PKG_CACHE_STMT` |
| SQLite | `EXPLAIN QUERY PLAN q` | (none; time it) `.timer on` in the shell | `ANALYZE` (`PRAGMA optimize`) | none |
| DuckDB | `EXPLAIN q` | `EXPLAIN ANALYZE q`; `PRAGMA enable_profiling='json'` | `ANALYZE` | none |
| ClickHouse | `EXPLAIN q`, `EXPLAIN indexes = 1 q`, `EXPLAIN ESTIMATE q`, `EXPLAIN PIPELINE q` | run, then `SYSTEM FLUSH LOGS` and read `system.query_log` (`query_duration_ms`, `read_rows`, `read_bytes`, `memory_usage`); `system.query_views_log` | none (statistics optional: `ALTER TABLE ... ADD STATISTICS`) | `system.query_log`, `clickhouse-benchmark` |
| Firebird 5 | `SET PLAN ON` / `SET EXPLAIN ON` in isql; driver `Statement.plan` / `detailed_plan` | `SET STATS ON` (isql) or `MON$` tables | none needed (index selectivity: `SET STATISTICS INDEX i`) | `MON$STATEMENTS`, trace API |
| H2 | `EXPLAIN q` | `EXPLAIN ANALYZE q` (scan counts) | `ANALYZE TABLE t` | none |
| MonetDB | `PLAN q` (logical), `EXPLAIN q` (MAL) | `TRACE q` (per-instruction timings) | `ANALYZE sys.t` | `sys.queue()`, `sys.querylog_catalog` |
| CrateDB | `EXPLAIN q` | `EXPLAIN ANALYZE q` (Lucene phase timings) | `ANALYZE` | `sys.jobs_log` |
| QuestDB | `EXPLAIN q` | (none; `query_log` in web console) | none | none |

## Plans for parameterised statements
- PostgreSQL: `PREPARE p AS SELECT ... $1; EXPLAIN ANALYZE EXECUTE p(42);` - after 5 executions a generic plan may be used (`plan_cache_mode = force_custom_plan` to compare).
- MySQL: `EXPLAIN FOR CONNECTION <id>` shows the plan of a running statement; otherwise substitute literals.
- SQL Server: run the parameterised batch with `SET STATISTICS XML ON`; the plan shows `ParameterCompiledValue` vs `ParameterRuntimeValue` (parameter sniffing).
- Oracle: bind variables are peeked; `DBMS_XPLAN.DISPLAY_CURSOR` with `+PEEKED_BINDS`.

## Reading the numbers
- Compare *estimated* vs *actual* rows at every node; a 10x gap anywhere upstream ruins join choices downstream.
- Buffers/IO: PG `shared hit` vs `read`; MySQL handler counters (`SHOW STATUS LIKE 'Handler%'`); SQL Server logical reads; Oracle consistent gets; ClickHouse `read_rows`/`read_bytes`.
- Time per node includes children (PostgreSQL, MySQL TREE); subtract to find the expensive operator.
