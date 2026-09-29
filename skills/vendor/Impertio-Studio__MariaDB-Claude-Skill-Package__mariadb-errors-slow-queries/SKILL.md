---
name: mariadb-errors-slow-queries
description: >
  Use when a query is slow and you need to diagnose, enabling the slow query log, analyzing aggregated samples with mariadb-dumpslow or pt-query-digest, profiling via performance_schema, detecting missing indexes from EXPLAIN, or interpreting Using filesort and Using temporary signals.
  Prevents the common mistake of trusting random one-off timings, leaving the slow query log disabled in production, setting long_query_time too high (10s) and missing sub-second issues, treating Using filesort as harmless, ignoring file-sort vs index-sort distinction, or using deprecated SHOW PROFILE instead of performance_schema.
  Covers slow_query_log + long_query_time + log_slow_verbosity configuration, mariadb-dumpslow vs pt-query-digest aggregation, performance_schema events_statements_summary_by_digest and events_statements_history_long, the 10.11 variable-rename to log_slow_query_*, missing-index detection patterns, file-sort vs index-sort with descending indexes (10.8+), Using temporary signal, EXPLAIN combined with ANALYZE FORMAT=JSON, Galera-aware slow-log handling.
  Keywords: slow query, slow_query_log, log_slow_query, long_query_time, log_slow_query_time, log_slow_verbosity, log_slow_filter, log_slow_rate_limit, min_examined_row_limit, mariadb-dumpslow, pt-query-digest, performance_schema, events_statements_summary_by_digest, SHOW PROFILE deprecated, Using filesort, Using temporary, missing index, full scan, type=ALL, key=NULL, file-sort vs index-sort, descending index, ANALYZE FORMAT JSON, my query is slow, slow query log location, why is my query slow, how to find slow queries, my page is slow, database is slow, queries timing out
license: MIT
compatibility: "Designed for Claude Code. Requires MariaDB 10.6-LTS, 10.11-LTS, 11.x, 12.x."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# MariaDB Slow Query Diagnosis

How to enable, capture, aggregate, and act on slow-query data. Covers the slow query log, performance_schema digests, EXPLAIN / ANALYZE signals, and the file-sort versus index-sort distinction.

## Quick Reference

- ALWAYS enable the slow query log in production. `slow_query_log = ON` with `long_query_time = 0.5` (or smaller) catches sub-second issues that `10` (the default) hides.
- ALWAYS set `log_slow_verbosity = query_plan,explain` (10.5+) so each slow-log entry carries the optimizer plan and an inline EXPLAIN. Without this you see SQL text but cannot decide why it was slow.
- ALWAYS prefer `pt-query-digest` (Percona Toolkit) over `mariadb-dumpslow` for aggregation. `pt-query-digest` fingerprints by digest and gives 95th-percentile, mean, total time, and lock-time per query family. `mariadb-dumpslow` only counts.
- ALWAYS aggregate at the FINGERPRINT level. Two queries that differ only in literal values are the same slow-query family. `pt-query-digest` and `events_statements_summary_by_digest` normalise them already.
- NEVER rely on `SHOW PROFILE` / `SHOW PROFILES`. They are deprecated since MariaDB 5.6.7 / 10.0. Use `performance_schema` events tables instead.
- NEVER trust a single EXPLAIN timing as proof. EXPLAIN does not execute the query. ALWAYS use `ANALYZE FORMAT=JSON SELECT ...` (10.1+) to compare estimated `rows` against observed `r_rows`.
- NEVER treat `Using filesort` as a fatal signal on its own. It is fatal on millions of rows or hot OLTP paths ; it is fine on small result sets. ALWAYS combine the `Using filesort` flag with `rows` in EXPLAIN before deciding to redesign.
- NEVER log slow queries to `TABLE` in a production write-heavy workload. `log_output = TABLE` writes every entry to `mysql.slow_log`, doubling write amplification. Keep `log_output = FILE` (the default) and use file rotation.
- NEVER raise `long_query_time` to "reduce noise". Lower it instead, and use `log_slow_rate_limit` or `min_examined_row_limit` to thin the volume while still catching short-but-frequent killers.
- NOTE : From 10.11 the variables are renamed. `slow_query_log` is now `log_slow_query`, `long_query_time` is `log_slow_query_time`, `slow_query_log_file` is `log_slow_query_file`, `min_examined_row_limit` is `log_slow_min_examined_row_limit`. The old names remain as aliases. Use the new names in 10.11+ config ; both work.

## Decision Tree

```
A query / page / report is slow.
|
+-- Is the slow query log already enabled ?
|     SELECT @@slow_query_log, @@long_query_time, @@log_slow_verbosity, @@log_output;
|     |
|     NO  : enable it (see Configuration below) and wait for representative traffic.
|     YES : continue.
|
+-- Goal : find which queries are slow ?
|     |
|     +-- Have a slow-log file already ?
|     |     YES : run pt-query-digest /var/log/mysql/mariadb-slow.log
|     |           Look at the top 10 by Total Time.
|     |     NO  : enable + wait, then come back.
|     |
|     +-- No file (log_output=TABLE) ?
|           SELECT * FROM mysql.slow_log ORDER BY query_time DESC LIMIT 20;
|
+-- Goal : profile ONE specific query right now ?
|     |
|     +-- Run EXPLAIN first :
|     |     EXPLAIN SELECT ...
|     |     Look at type (ALL = full scan) and key (NULL = no index used).
|     |
|     +-- Run ANALYZE FORMAT=JSON for real numbers (10.1+, SELECT only) :
|     |     ANALYZE FORMAT=JSON SELECT ...
|     |     Compare rows (estimated) vs r_rows (actual).
|     |
|     +-- Want time per stage (parse, lock, send) ?
|           Query performance_schema.events_statements_history_long,
|           filtered on EVENT_NAME like 'statement/sql/select' and SQL_TEXT.
|
+-- Goal : long-term top-N across the whole workload ?
      SELECT digest_text, count_star, sum_timer_wait/1e12 AS total_s,
             avg_timer_wait/1e9 AS avg_ms
        FROM performance_schema.events_statements_summary_by_digest
        ORDER BY sum_timer_wait DESC LIMIT 20;
```

## Configuration

Minimum production-grade config (10.6+ and 10.11+ alias names) :

```ini
# my.cnf, [mariadb] section, MariaDB 10.6-LTS / 10.11-LTS / 11.x / 12.x
[mariadb]
# 10.6+ name (still works in 10.11 as alias)
slow_query_log               = 1
slow_query_log_file          = /var/log/mysql/mariadb-slow.log
long_query_time              = 0.5
log_output                   = FILE
log_slow_verbosity           = query_plan,explain
log_queries_not_using_indexes = 1
log_slow_admin_statements    = 1
min_examined_row_limit       = 100

# 10.11+ preferred names (use these in 10.11 / 11.x / 12.x configs)
# log_slow_query              = 1
# log_slow_query_file         = /var/log/mysql/mariadb-slow.log
# log_slow_query_time         = 0.5
# log_slow_min_examined_row_limit = 100
```

All slow-log variables are dynamic. ALWAYS use `SET GLOBAL` for runtime tuning so a restart is not required.

```sql
-- 10.6+ : flip on without restart
SET GLOBAL slow_query_log         = ON;
SET GLOBAL long_query_time        = 0.5;
SET GLOBAL log_slow_verbosity     = 'query_plan,explain';
SET GLOBAL log_queries_not_using_indexes = ON;

-- 10.6+ : flush + open new file (e.g. after rotation)
FLUSH SLOW LOGS;

-- 10.11+ : new names work side-by-side
SET GLOBAL log_slow_query         = ON;
SET GLOBAL log_slow_query_time    = 0.5;
```

`long_query_time` accepts fractional seconds (microsecond resolution). `0.5` is a safer baseline than `10` for any OLTP system.

Per-session tracing for ad-hoc debugging :

```sql
-- 10.6+ : trace ONE session only, do not pollute global log
SET SESSION long_query_time      = 0;
SET SESSION log_slow_verbosity   = 'query_plan,explain';
SET SESSION slow_query_log       = ON;
-- run the suspect query, then turn off
SET SESSION slow_query_log       = OFF;
```

See `references/methods.md` for the full variable matrix (defaults, scope, dynamic flag, version) and the `log_slow_filter` flag set.

## What the Slow Log Records per Entry

Each entry begins with `# User@Host:` and is followed by three header lines plus the SQL text. Per the KB :

```
# User@Host: app[app] @ localhost []
# Thread_id: 17  Schema: app  QC_hit: No
# Query_time: 1.234567  Lock_time: 0.000234  Rows_sent: 12  Rows_examined: 1245678
SET timestamp=1716123456;
SELECT ... FROM ... WHERE ...;
```

When `log_slow_verbosity` includes `explain`, the entry also carries an inline `# explain:` block with the optimizer's plan. This is only emitted when `log_output = FILE` (per KB : "EXPLAIN output will only be recorded if the slow query log is written to a file").

`Rows_examined` is the key number. A query reporting `Rows_sent: 12` but `Rows_examined: 1245678` is almost always a missing-index problem regardless of total time.

## Aggregating with pt-query-digest

`pt-query-digest` is the de-facto aggregator. It fingerprints queries (normalises literals to `?`), groups by digest, and ranks by total time, count, mean, and 95th percentile.

```bash
# Percona Toolkit installed separately ; works against a MariaDB slow-log
pt-query-digest /var/log/mysql/mariadb-slow.log > digest.txt
head -200 digest.txt
```

Top section : `Profile` table with rank, Response time, calls, R/Call (mean), V/M (variance-to-mean). Then per-query block with the canonical example, EXPLAIN-able SQL, histograms, and the literal sample.

`mariadb-dumpslow` is the bundled minimal alternative :

```bash
# 10.6+ : groups identical-after-normalisation queries, sorts by total time
mariadb-dumpslow -s t /var/log/mysql/mariadb-slow.log | head -50
```

Use `mariadb-dumpslow` when Percona Toolkit is not available. ALWAYS prefer `pt-query-digest` when it is.

## Aggregating with performance_schema (No External Tool)

`performance_schema` ships disabled by default. Enabling requires a restart :

```ini
# my.cnf
[mariadb]
performance_schema = ON
```

Once enabled, the digest table is the all-workload top-N source :

```sql
-- 10.6+ : top-20 by total time, normalised by digest
SELECT
    LEFT(digest_text, 80) AS query,
    count_star            AS calls,
    ROUND(sum_timer_wait/1e12, 2)  AS total_s,
    ROUND(avg_timer_wait/1e9, 2)   AS avg_ms,
    ROUND(max_timer_wait/1e9, 2)   AS max_ms,
    sum_rows_examined / NULLIF(count_star, 0) AS avg_rows_examined
  FROM performance_schema.events_statements_summary_by_digest
  ORDER BY sum_timer_wait DESC
  LIMIT 20;
```

`events_statements_history_long` keeps the most recent N completed statements (default 10000) with per-statement wait, lock-time, errors, and stage breakdown. Use it for the "what was happening at 14:32" question.

See `references/methods.md` for the relevant table list and the columns to project.

## EXPLAIN Signals That Predict Slow

| EXPLAIN column / value | Meaning | Action |
|------------------------|---------|--------|
| `type = ALL` | Full table scan | Add an index on the WHERE columns, or rewrite the predicate |
| `type = index` | Full index scan (better than ALL, still scans every index leaf) | Add a more selective index |
| `key = NULL` | No index used | Same as above |
| `rows` >> matched | Optimizer estimates many rows for few results | Bad statistics : run `ANALYZE TABLE`. Confirm with `ANALYZE FORMAT=JSON` (compare `rows` vs `r_rows`) |
| `Extra : Using filesort` | An extra sort pass after the join | Match ORDER BY to an existing index, or add a descending index (10.8+) |
| `Extra : Using temporary` | Intermediate temp table for GROUP BY / DISTINCT / ORDER BY on non-driver table | Add a composite index covering GROUP BY / ORDER BY, or rewrite |
| `Extra : Using index` | Covering index ; no row fetch | This is GOOD ; do not "fix" it |
| `Extra : Using index condition` | ICP pushdown | GOOD ; predicate pushed to engine |
| `Extra : Using join buffer (Block Nested Loop)` | No usable index for the join | Add an index on the join column of the inner table |

EXPLAIN gives ESTIMATES. `ANALYZE FORMAT=JSON SELECT ...` (10.1+) gives ACTUAL numbers. Use it to validate before refactoring :

```sql
-- 10.6+ : actual rows + actual time per node ; SELECT-safe
ANALYZE FORMAT=JSON SELECT ... FROM ... WHERE ...;
```

WARNING : `ANALYZE UPDATE` and `ANALYZE DELETE` actually execute the mutation (per KB). ALWAYS run `ANALYZE FORMAT=JSON SELECT` only, never `ANALYZE FORMAT=JSON UPDATE/DELETE` for a "dry run" : there is no dry run.

## File-Sort vs Index-Sort

`Using filesort` does NOT mean "writing to disk". It means the optimizer chose a sort step that is not satisfied by an index. The sort may be in-memory (small result) or on-disk (large result), but the EXPLAIN flag is the same.

ALWAYS distinguish three cases :

1. ORDER BY columns match a leading prefix of an existing index, in the same direction : NO `Using filesort` ; index-sort is used. Best.
2. ORDER BY columns match an index but in the OPPOSITE direction across columns : `Using filesort` appears UNTIL you add a descending index. From 10.8+, MariaDB supports descending indexes natively (`CREATE INDEX idx ON t (a ASC, b DESC)`).
3. ORDER BY columns are not in any index, or mix ASC/DESC in a way no index covers : `Using filesort` is unavoidable. The fix is to add a covering composite index, or to redesign the query.

Detection of "filesort going to disk" : enable `log_slow_verbosity` with `query_plan` and look for `Tmp_table_on_disk: Yes` in the slow-log block. Alternatively, query `Sort_merge_passes` from `SHOW STATUS` ; a steady non-zero rate means disk-spill is regular.

```sql
-- 10.8+ : descending index removes Using filesort on ORDER BY a, b DESC
CREATE INDEX idx_t_a_b_desc ON t (a ASC, b DESC);
```

See `references/examples.md` for a full filesort-fix walkthrough.

## Missing-Index Detection Pattern

A query is "missing an index" when EXPLAIN reports `type=ALL` or `key=NULL` against a table over a meaningful row threshold (say, 10k rows). Detection at workload scale :

```sql
-- 10.6+ : queries logged because they did not use an index
-- requires log_queries_not_using_indexes = ON
-- read directly from slow-log file
```

Or, via `performance_schema` :

```sql
SELECT
    LEFT(digest_text, 80) AS query,
    count_star            AS calls,
    sum_no_index_used     AS no_idx_count,
    sum_rows_examined     AS total_rows_scanned
  FROM performance_schema.events_statements_summary_by_digest
  WHERE sum_no_index_used > 0
  ORDER BY sum_rows_examined DESC
  LIMIT 20;
```

`sum_no_index_used` is the count of executions where no index was used. ALWAYS check selectivity BEFORE adding an index : an index on a column with 3 distinct values across 10M rows is worthless. See `references/anti-patterns.md` for the "index every slow column" anti-pattern.

## Galera-Aware Slow-Log Handling

On a Galera cluster, every node executes write-set replication for every committed transaction. The slow log on a REPLICA node will show writes that originated on a different node. ALWAYS aggregate slow-log data per-node, then deduplicate by digest across nodes : the same slow write-set will appear on all N nodes.

For read traffic, slow log on a node only reflects reads landing on that node, so per-node aggregation matters for read distribution analysis. See `mariadb-impl-galera-cluster` for cluster setup ; this skill only covers the slow-log slice.

## What This Skill Does NOT Cover

- General `EXPLAIN` semantics deep dive : see `mariadb-syntax-explain-and-optimizer`.
- Index design (when to use covering vs composite vs functional) : see `mariadb-impl-indexing`.
- InnoDB buffer-pool tuning, `innodb_io_capacity`, per-connection buffer sizing : see `mariadb-impl-performance-tuning`.
- Optimizer trace via `optimizer_trace=enabled=on` : see `mariadb-syntax-explain-and-optimizer`.
- Deadlock and lock-wait diagnosis : see `mariadb-errors-deadlocks`.

## Reference Links

- `references/methods.md` : complete slow-log variable matrix (defaults, scope, dynamic, version), `log_slow_verbosity` flag values with version notes, `log_slow_filter` flag values, performance_schema digest-and-history tables and key columns, `pt-query-digest` and `mariadb-dumpslow` CLI options.
- `references/examples.md` : 10+ working examples (enable slow log + verbosity, per-session trace, pt-query-digest run, top-10 from performance_schema, ANALYZE FORMAT=JSON for one query, detect missing index from digest, fix Using filesort with 10.8+ descending index, detect Using temporary on big GROUP BY, log_slow_rate_limit for noisy workloads, mariadb-dumpslow fallback).
- `references/anti-patterns.md` : 8+ real anti-patterns (slow log off in production, long_query_time=10 misses sub-second, trusting deprecated SHOW PROFILE, trusting EXPLAIN rows estimate as actual, indexing every slow column without selectivity, ignoring Using temporary on big GROUP BY, log_output=TABLE in production, forgetting slow-log rotation).

## Source Verification

All facts in this skill were verified via WebFetch against :
- `mariadb.com/kb/en/slow-query-log/` : variable list (`slow_query_log`, `slow_query_log_file`, `long_query_time`, `log_slow_verbosity`, `log_output`, `log_queries_not_using_indexes`, `log_slow_admin_statements`, `log_slow_filter`, `log_slow_rate_limit`, `min_examined_row_limit`, `log_slow_always_query_time`), entry format (`# User@Host:`, `Thread_id`, `Schema`, `QC_hit`, `Query_time`, `Lock_time`, `Rows_sent`, `Rows_examined`).
- `mariadb.com/kb/en/slow-query-log-overview/` : 10.11+ rename to `log_slow_query`, `log_slow_query_file`, `log_slow_query_time`, `log_slow_min_examined_row_limit` with old names retained as aliases ; `log_slow_admin_statements` deprecated from 11.0+ in favour of `log_slow_filter` ; `log_slow_filter` flag set includes `filesort`, `filesort_on_disk`, `tmp_table`, `tmp_table_on_disk`, `admin`, `not_using_index`.
- `mariadb.com/kb/en/explain-in-the-slow-query-log/` : `log_slow_verbosity` `explain` flag introduced in 10.1.0 ; EXPLAIN inline only when `log_output = FILE`.
- `mariadb.com/kb/en/analyze-statement/` : `ANALYZE FORMAT=JSON` semantics ; `r_rows` and `r_filtered` actual-observation columns ; warning that `ANALYZE UPDATE / DELETE` actually mutate.
- Vooronderzoek Cluster-2 §9 : EXPLAIN columns, `Using filesort` and `Using temporary` as red flags, type ranking `system` to `ALL`.
- Vooronderzoek Cluster-3 §4 : tuning context, per-connection buffer multiplication, query-cache nuance.
