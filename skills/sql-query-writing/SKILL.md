---
name: sql-query-writing
description: Write correct, portable, dialect-aware SQL (PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, Db2, SQLite, DuckDB, ClickHouse, CockroachDB, YugabyteDB, TiDB, Firebird, H2, MonetDB, CrateDB, QuestDB). Use when authoring queries with window functions, recursive CTEs, LATERAL/CROSS APPLY, JSON extraction, full-text search, UPSERT/MERGE, GROUPING SETS, pagination, string/percentile aggregates, or when a query must run on several engines. Includes a verified per-dialect syntax cheat-sheet and the portable macro approach used by the rd-databases lab.
---

# SQL query writing (dialect-aware)

## When to use
- A query has to be written for a specific engine and you are not sure of the exact syntax (JSON access, date truncation, LIMIT/OFFSET form, UPSERT).
- A query has to run on more than one engine (tests, migrations, multi-tenant products).
- You need to know whether a construct exists at all on the target engine before promising it.

## Procedure
1. **Identify the dialect family** — PostgreSQL wire does not mean PostgreSQL syntax (CockroachDB, YugabyteDB, QuestDB, CrateDB, H2 -pg all differ). MySQL wire: MySQL 8/9, MariaDB, TiDB, Percona, Dolt.
2. **Look up the construct** in `references/dialect-cheatsheet.md` (verified against running engines in Sept 2026). If the cell says `--`, there is no syntax; pick the portable alternative listed in `references/portable-alternatives.md`.
3. **Write the query in portable form first**, then apply dialect-specific tokens. The lab's approach: write SQL once with `{macro(args)}` placeholders (`{limit(50)}`, `{json_get(attrs,color)}`, `{date_trunc_month(ordered_at)}`, `{with_recursive}`, `{cross_lateral}`, `{fts(description,words)}`, `{now}`, `{ts(2025-01-01 00:00:00)}`) and render per dialect (`harness/rdlab/dialects.py`). Steal that file if you need to support many engines.
4. **Parameters**: use the driver's paramstyle (`%s` psycopg/PyMySQL/pymssql, `?` sqlite3/DuckDB/ibm_db/firebird, `:1` python-oracledb, `%(name)s` pymonetdb). Never inline user values. With `%s`-style drivers a literal `%` must be doubled *only when parameters are passed* (PyMySQL does no substitution otherwise — the lab hit this bug with `DATE_FORMAT(x, '%%Y-%%m-01')`).
5. **Check the result shape**, not just "it ran": row counts, NULL handling (ClickHouse LEFT JOIN yields defaults, not NULLs, unless `join_use_nulls=1`), ordering (add a tiebreaker column to every ORDER BY that feeds pagination), decimal vs float types (CrateDB/QuestDB have no DECIMAL).
6. **Run EXPLAIN** before shipping anything that touches a large table — see the `sql-query-optimization` skill.

## Rules of thumb that hold on every engine
- `ORDER BY` + `LIMIT` needs a deterministic key (`ORDER BY ordered_at, id`).
- Keyset pagination: prefer the row-value form `(ordered_at, id) > (:ts, :id)` where supported (PostgreSQL, MySQL, MariaDB, SQLite, DuckDB, CockroachDB, TiDB, H2); expand to `ts > :ts OR (ts = :ts AND id > :id)` elsewhere (SQL Server, Oracle, Db2, Firebird). Measured: PostgreSQL row-value form 0.6 ms vs 44 ms for the expanded OR form on 200k rows; MySQL is the opposite (row-value form 165 ms vs 9 ms) — verify per engine.
- `NOT IN (subquery)` is NULL-unsafe; use `NOT EXISTS`.
- `COUNT(DISTINCT x)` is a full aggregate everywhere; approximate variants exist (`approx_count_distinct`, `uniq`, `APPROX_COUNT_DISTINCT`).
- Recursive CTE keyword: `WITH RECURSIVE` (PostgreSQL family, MySQL, MariaDB, SQLite, DuckDB, ClickHouse, MonetDB) vs plain `WITH` (SQL Server, Oracle, Db2, Firebird, H2). CrateDB and QuestDB have no recursive CTEs.
- Top-N per group: `LATERAL`/`CROSS APPLY` where available (PostgreSQL, MySQL 8.0.14+, SQL Server, Oracle 12c+, Db2, DuckDB, CockroachDB); portable fallback is `ROW_NUMBER() OVER (PARTITION BY ...)` in a derived table (works everywhere that has window functions — all engines in the lab).
- UPSERT: `INSERT ... ON CONFLICT (k) DO UPDATE SET c = EXCLUDED.c` (PostgreSQL, CockroachDB, YugabyteDB, SQLite, DuckDB, CrateDB); `INSERT ... AS new ON DUPLICATE KEY UPDATE c = new.c` (MySQL 8.0.20+; MariaDB uses `VALUES(c)`); `MERGE` (SQL Server, Oracle, Db2, Firebird, MonetDB, PostgreSQL 15+, H2 has its own `MERGE ... KEY`); Firebird also has `UPDATE OR INSERT ... MATCHING`. ClickHouse and QuestDB have no UPSERT (use ReplacingMergeTree / dedup on read).

## References
- `references/dialect-cheatsheet.md` — one table per construct, verified.
- `references/portable-alternatives.md` — what to do when a construct is missing.
- Feature probe results across 15 engines: `results/SUMMARY.md` (section "Feature probes") in the rd-databases lab.
