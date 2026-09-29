# Portable alternatives when a construct is missing

| Missing construct | Engines lacking it | Portable rewrite |
|---|---|---|
| `LATERAL` / `CROSS APPLY` | MariaDB, SQLite, ClickHouse, Firebird, H2, MonetDB, CrateDB, QuestDB | `ROW_NUMBER() OVER (PARTITION BY k ORDER BY ...)` in a derived table, filter `rn <= N` (lab query q10b, all engines) |
| `GROUPING SETS` / `CUBE` | CockroachDB, SQLite (3.x), CrateDB, QuestDB, Firebird | `UNION ALL` of the individual GROUP BY levels with literal NULL columns; MySQL has `WITH ROLLUP` |
| `MERGE` | MySQL, MariaDB, TiDB, SQLite, CockroachDB, ClickHouse, CrateDB, QuestDB | engine UPSERT (`ON DUPLICATE KEY UPDATE`, `ON CONFLICT DO UPDATE`) or two statements in a transaction |
| `PERCENTILE_CONT` | MySQL, TiDB, SQLite, Firebird, H2 | window trick: `ROW_NUMBER()`/`COUNT()` over the partition and average the middle rows |
| `FILTER (WHERE ...)` | MySQL, MariaDB, SQL Server, Oracle, Db2, ClickHouse, Firebird, H2 | `SUM(CASE WHEN cond THEN 1 ELSE 0 END)` / `COUNT(CASE WHEN cond THEN 1 END)`; ClickHouse `countIf` |
| `DISTINCT ON` | everything except PG/CR/YB/DuckDB/ClickHouse | `ROW_NUMBER()` = 1 |
| `QUALIFY` | everything except DuckDB, ClickHouse, Snowflake/BigQuery-style engines | wrap in a derived table and filter |
| `RETURNING` | MySQL (only MariaDB has it), SQL Server (`OUTPUT`), Db2 (`SELECT ... FROM FINAL TABLE (INSERT ...)`), H2 (`FINAL TABLE`), ClickHouse, QuestDB | select back by key in the same transaction; MySQL `LAST_INSERT_ID()` |
| Recursive CTE | CrateDB, QuestDB | precompute closure table / nested sets; or iterate client-side |
| Statement timeout | Oracle (needs resource manager), SQLite/DuckDB (client interrupt only), QuestDB (server config) | client-side timeouts + cancellation |
| Partial (filtered) index | MySQL, MariaDB, TiDB, Oracle (use function-based index with NULLs), Db2, Firebird, H2, MonetDB, ClickHouse | composite index with the filter column first; generated column + index |
| `INCLUDE` covering index | MySQL family (add the columns to the key), Oracle, Db2 (`INCLUDE` on unique only), SQLite, DuckDB | wider composite index (key columns first, then payload) |
| Arrays | MySQL, MariaDB, TiDB, SQL Server, SQLite, Firebird, H2 (has ARRAY but limited), MonetDB | JSON arrays or child tables |
