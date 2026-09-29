# Methods : MariaDB Slow Query Log + Profiling

Reference content for `mariadb-errors-slow-queries`. Complete variable matrices, flag sets, and tool options.

---

## 1. Slow Query Log : System Variable Matrix

All variables below are global+session and dynamic unless noted. Defaults apply to MariaDB 10.6+ unless a version is given.

| Variable (10.6+) | 10.11+ alias | Default | Scope | Dynamic | Meaning |
|------------------|--------------|---------|-------|---------|---------|
| `slow_query_log` | `log_slow_query` | `OFF` | Global | Yes | Master switch. `ON` enables the slow log. |
| `slow_query_log_file` | `log_slow_query_file` | `${hostname}-slow.log` (in datadir) | Global | Yes | Path to slow log when `log_output` includes `FILE`. |
| `long_query_time` | `log_slow_query_time` | `10` (seconds, fractional OK) | Global, Session | Yes | Threshold above which a query is "slow". |
| `log_slow_verbosity` | (same) | `''` (empty) | Global, Session | Yes | Comma-separated flag set ; controls extra info per slow-log entry. |
| `log_output` | (same) | `FILE` | Global | Yes | Where slow-log goes : `FILE`, `TABLE`, or `NONE`. Comma-separated for both. |
| `log_queries_not_using_indexes` | (same) | `OFF` | Global | Yes | If `ON`, log any query whose plan does not use an index, regardless of time. |
| `log_slow_admin_statements` | (same) | `ON` | Global | Yes | If `ON`, admin statements (ALTER, ANALYZE, OPTIMIZE, REPAIR, etc.) participate in slow-log. Deprecated from 11.0+ : use `log_slow_filter` instead. |
| `log_slow_disabled_statements` | (same) | `sp` (stored procedures excluded) | Global | Config only | Statement types EXCLUDED from logging. Set in config, not at runtime. |
| `log_slow_filter` | (same) | `''` (empty = no filter) | Global, Session | Yes | Restrict logging to specific plan-features. See flag set below. |
| `log_slow_rate_limit` | (same) | `1` | Global, Session | Yes | Log only 1-in-N otherwise-eligible slow queries. Used to thin high-volume slow workloads. |
| `min_examined_row_limit` | `log_slow_min_examined_row_limit` | `0` | Global, Session | Yes | Skip the slow log unless the query examined at least N rows. |
| `log_slow_always_query_time` | (same) | depends on version | Global | Yes | Query-time threshold above which queries are ALWAYS logged regardless of `log_slow_rate_limit`. |

### Rename note (10.11+)

From 10.11, MariaDB renamed the slow-log variables to be prefix-consistent with other `log_*` variables. The old names work as aliases in 10.11+ and 11.x. For new config files in 10.11+ ALWAYS use the new names. For configs targeting 10.6-LTS keep the old names.

---

## 2. `log_slow_verbosity` Flag Values

`log_slow_verbosity` is a SET-type variable. Combine flags with comma.

| Flag | Effect | Version |
|------|--------|---------|
| `query_plan` | Adds plan info (filesort, temp table, full scan markers) per entry | 10.5+ |
| `explain` | Inserts an EXPLAIN block (`# explain:` prefix) per slow query | 10.1.0+ ; FILE output only |
| `innodb` | Adds InnoDB-level statistics (pages accessed, IO, etc.) | older versions |
| `full` | Shortcut : enables all available verbosity flags | older versions |
| `engine` | Adds storage-engine specific stats | 10.5+ |
| `warnings` | Adds query warnings to the slow-log entry | 10.5+ |

Recommended baseline : `query_plan,explain`. ALWAYS set this in any production-grade `my.cnf` so the slow log is self-explanatory without re-running EXPLAIN.

---

## 3. `log_slow_filter` Flag Values

`log_slow_filter` is a SET-type filter. If non-empty, ONLY queries whose plan matches at least one flag are logged.

| Flag | Matches queries that |
|------|---------------------|
| `admin` | are admin statements (ALTER TABLE, ANALYZE, OPTIMIZE, REPAIR, etc.) |
| `filesort` | trigger `Using filesort` |
| `filesort_on_disk` | trigger a disk-spilling filesort |
| `full_join` | join without indexes (Block Nested Loop) |
| `full_scan` | trigger `type=ALL` |
| `query_cache` | hit the query cache |
| `query_cache_miss` | miss the query cache |
| `tmp_table` | create an internal temporary table |
| `tmp_table_on_disk` | spill the internal temp table to disk |
| `not_using_index` | execute without using an index |

Example : `log_slow_filter = filesort_on_disk,tmp_table_on_disk,full_scan` logs only the genuinely-bad plans, ignoring sub-second indexed queries that exceed `long_query_time` due to lock contention or cold cache.

---

## 4. Slow-Log Entry Format

Each entry per the KB :

```
# User@Host: app[app] @ localhost [127.0.0.1]
# Thread_id: 17  Schema: app_db  QC_hit: No
# Query_time: 1.234567  Lock_time: 0.000234  Rows_sent: 12  Rows_examined: 1245678
# explain: type ref ; key idx_status ; key_len 5 ; rows 1245678 ; Extra Using where ; Using filesort
SET timestamp=1716123456;
SELECT id, name FROM orders WHERE status = 'pending' ORDER BY created_at;
```

| Field | Meaning |
|-------|---------|
| `User@Host` | Connection identity ; useful for blaming an application user |
| `Thread_id` | Internal connection thread id ; cross-reference with `PROCESSLIST` |
| `Schema` | Default database for this connection at execution |
| `QC_hit` | `Yes` if served from query cache (`query_cache_type=ON`) ; `No` otherwise |
| `Query_time` | Wall-clock seconds for the statement |
| `Lock_time` | Seconds spent waiting for table/row locks (subset of Query_time) |
| `Rows_sent` | Rows returned to client |
| `Rows_examined` | Rows the server had to inspect to produce that result |
| `# explain:` | (10.1+ with `log_slow_verbosity` including `explain`) inline EXPLAIN line |

The single most actionable metric is `Rows_examined / Rows_sent`. A ratio of 100000:1 is a missing index.

---

## 5. performance_schema : Relevant Tables

Enable via `my.cnf` :

```ini
[mariadb]
performance_schema = ON
```

Requires server restart to enable (the variable is `--performance_schema`, read-only after startup).

### 5.1 `events_statements_summary_by_digest`

Pre-aggregated per-digest summary across the server's uptime. Best source for "top-N queries".

| Column | Meaning |
|--------|---------|
| `schema_name` | Default schema at execution (NULL for cross-schema) |
| `digest` | MD5-style hash of the normalised SQL text |
| `digest_text` | Normalised SQL with `?` instead of literals |
| `count_star` | Number of executions |
| `sum_timer_wait` | Total cumulative wait time in picoseconds |
| `min_timer_wait` / `avg_timer_wait` / `max_timer_wait` | Per-execution wait times in picoseconds |
| `sum_lock_time` | Cumulative lock-wait time in picoseconds |
| `sum_errors`, `sum_warnings` | Error and warning counts |
| `sum_rows_affected` | DML row impact |
| `sum_rows_sent` | Rows returned to client |
| `sum_rows_examined` | Rows the server scanned |
| `sum_created_tmp_disk_tables` | Internal disk temp tables created |
| `sum_created_tmp_tables` | Internal temp tables (memory + disk) |
| `sum_sort_merge_passes` | Disk-spilling filesort passes |
| `sum_no_index_used` | Executions where no index was used |
| `sum_no_good_index_used` | Executions where an index was used but the optimizer flagged it as suboptimal |
| `first_seen`, `last_seen` | Timestamps of first and last execution |

Unit conversion : picoseconds to milliseconds = `/ 1e9`, picoseconds to seconds = `/ 1e12`.

### 5.2 `events_statements_history_long`

Ring buffer of recent completed statements. Default size 10000.

| Column | Meaning |
|--------|---------|
| `EVENT_ID` | Monotonic id |
| `THREAD_ID` | Internal thread id |
| `SQL_TEXT` | Original, NON-normalised SQL |
| `DIGEST_TEXT` | Normalised SQL |
| `TIMER_START`, `TIMER_END`, `TIMER_WAIT` | Timestamps and total wait, picoseconds |
| `LOCK_TIME` | Lock-wait subset, picoseconds |
| `ERRORS`, `WARNINGS` | Per-statement error and warning counts |
| `ROWS_AFFECTED`, `ROWS_SENT`, `ROWS_EXAMINED` | Per-statement row impact |
| `CREATED_TMP_DISK_TABLES`, `CREATED_TMP_TABLES` | Per-statement temp-table counts |
| `NO_INDEX_USED`, `NO_GOOD_INDEX_USED` | Boolean (0/1) flags |
| `END_EVENT_ID`, `NESTING_EVENT_ID` | Linkage for nested statements (stored proc bodies) |

Use for "what happened in this 30-second window" forensic queries.

### 5.3 Replacement for SHOW PROFILE / SHOW PROFILES

`SHOW PROFILE` / `SHOW PROFILES` are deprecated. The replacement is `events_statements_history_long` joined with `events_stages_history_long` (per-stage breakdown : parsing, optimizing, executing, sending data, closing tables).

```sql
SELECT
    s.EVENT_NAME,
    s.TIMER_WAIT / 1e9 AS stage_ms
  FROM performance_schema.events_stages_history_long s
  JOIN performance_schema.events_statements_history_long t
    ON s.NESTING_EVENT_ID = t.EVENT_ID
  WHERE t.SQL_TEXT LIKE 'SELECT%FROM orders%'
  ORDER BY s.EVENT_ID DESC
  LIMIT 50;
```

---

## 6. `pt-query-digest` (Percona Toolkit)

External tool ; install Percona Toolkit separately. Reads MariaDB slow-log files unchanged.

### 6.1 Basic invocation

```bash
pt-query-digest /var/log/mysql/mariadb-slow.log > digest.txt
```

### 6.2 Useful options

| Option | Purpose |
|--------|---------|
| `--limit 95%:20` | Top 20 queries that make up 95% of total time |
| `--type slowlog` | Force slowlog parser (default for slowlog files) |
| `--since '24h'` | Filter to last 24 hours |
| `--since '2026-05-19 00:00:00'` | Filter from absolute timestamp |
| `--filter '$event->{user} eq "app"'` | Per-user filter |
| `--review h=localhost,D=meta,t=query_review` | Persist digest data to a tracking table |
| `--history h=localhost,D=meta,t=query_review_history` | Time-series digest tracking |

### 6.3 Output sections

1. `# Overall` : totals across the log
2. `# Profile` : ranked table : Rank, Query ID, Response time, calls, R/Call (mean), V/M, Item
3. Per-query block : `# Query N: ... ID 0xABCDEF... at byte M` with histograms, fingerprint, copy-paste SQL example, and EXPLAIN-ready SQL

ALWAYS focus on Rank 1-5 first. Long tail is usually noise unless you have many small offenders.

---

## 7. `mariadb-dumpslow` (Bundled Minimal Tool)

Ships with the server. Less capable than `pt-query-digest` but always available.

### 7.1 Basic invocation

```bash
mariadb-dumpslow /var/log/mysql/mariadb-slow.log
```

### 7.2 Useful options

| Option | Purpose |
|--------|---------|
| `-s t` | Sort by total query time (default) |
| `-s at` | Sort by average time |
| `-s c` | Sort by count |
| `-s l` | Sort by total lock time |
| `-s al` | Sort by average lock time |
| `-s r` | Sort by total rows sent |
| `-s ar` | Sort by average rows sent |
| `-t N` | Show top N queries |
| `-r` | Reverse sort order |
| `-a` | Do not abstract numbers (keep literals) |
| `-g 'regex'` | Only consider lines matching regex |

ALWAYS start with `mariadb-dumpslow -s t -t 20 /var/log/mysql/mariadb-slow.log` when Percona Toolkit is not installed.

---

## 8. EXPLAIN Quick Reference (Slow-Query Subset)

Columns (most-actionable subset) :

| Column | Bad value | Good value |
|--------|-----------|------------|
| `type` | `ALL`, `index` | `range`, `ref`, `eq_ref`, `const`, `system` |
| `key` | `NULL` | a real index name |
| `rows` | very large vs result | close to result size |
| `Extra : Using filesort` | present without descending index | absent, or `Using index` |
| `Extra : Using temporary` | present with millions of rows | absent |
| `Extra : Using index` | (this is GOOD) | present = covering index |
| `Extra : Using index condition` | (GOOD) | present = ICP pushdown |
| `Extra : Using join buffer` | present without join index | absent |

Use `ANALYZE FORMAT=JSON SELECT ...` to validate. Compare `rows` (estimated) to `r_rows` (observed). If they differ by 10x+ : run `ANALYZE TABLE` to refresh statistics.

---

## 9. Variable-Aliasing Truth Table (10.6 vs 10.11+)

| 10.6-LTS name | 10.11+ alias |
|---------------|--------------|
| `slow_query_log` | `log_slow_query` |
| `slow_query_log_file` | `log_slow_query_file` |
| `long_query_time` | `log_slow_query_time` |
| `min_examined_row_limit` | `log_slow_min_examined_row_limit` |
| `log_slow_verbosity` | (unchanged) |
| `log_slow_filter` | (unchanged) |
| `log_slow_rate_limit` | (unchanged) |
| `log_slow_admin_statements` | deprecated 11.0+, use `log_slow_filter=admin` |
| `log_queries_not_using_indexes` | (unchanged) |
| `log_output` | (unchanged) |

Both old and new names work in 10.11+ and 11.x. Pick one style per config file ; mixing creates review confusion.

---

## Sources

- `mariadb.com/kb/en/slow-query-log/` : variable list, entry format, `# User@Host:` literal prefix
- `mariadb.com/kb/en/slow-query-log-overview/` : variable rename 10.11+, `log_slow_filter` flag set, `log_slow_admin_statements` deprecation 11.0+
- `mariadb.com/kb/en/explain-in-the-slow-query-log/` : `log_slow_verbosity=explain` introduced 10.1.0, FILE-only constraint
- `mariadb.com/kb/en/analyze-statement/` : `ANALYZE FORMAT=JSON`, `r_rows`, `r_filtered`, mutation warning for `ANALYZE UPDATE/DELETE`
- `mariadb.com/kb/en/explain/` : EXPLAIN columns, type ranking, Extra flag semantics
