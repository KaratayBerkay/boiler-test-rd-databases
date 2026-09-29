# Anti-Patterns : MariaDB Slow Query Diagnosis

Real anti-patterns for `mariadb-errors-slow-queries`. Each entry : the broken approach, why it fails, and the correct alternative.

---

## AP-1 : Slow query log disabled in production

### Broken

```ini
# my.cnf : the default, left untouched
[mariadb]
# slow_query_log not set  -> defaults to OFF
```

The team relies on application APM, occasional `SHOW PROCESSLIST` glances, or user complaints to find slow queries.

### Why it fails

`slow_query_log` defaults to `OFF`. With it off there is NO server-side record of which queries were slow, how often, or how slow. When a customer reports "the dashboard is slow" you have nothing to analyze : the offending query already finished and left no trace. APM samples ; the slow log is complete.

### Correct

```ini
# my.cnf, [mariadb]
slow_query_log               = 1
slow_query_log_file          = /var/log/mysql/mariadb-slow.log
long_query_time              = 0.5
log_slow_verbosity           = query_plan,explain
log_queries_not_using_indexes = 1
```

ALWAYS enable the slow query log in production. The overhead of logging a genuinely-slow query is negligible compared to the cost of the query itself.

---

## AP-2 : long_query_time left at 10 seconds

### Broken

```sql
-- accepting the default
SELECT @@long_query_time;   -- 10.000000
```

The slow log is "on", but nothing ever appears in it.

### Why it fails

`long_query_time` defaults to `10` seconds. Almost no OLTP query takes 10 seconds. The real damage in a web application comes from a 0.3-second query called 5000 times per minute, or a 1.2-second report query. With the threshold at 10, every one of those is invisible. The slow log looks "clean" while the database is the bottleneck.

### Correct

```sql
-- 10.6+ : fractional seconds, catch sub-second offenders
SET GLOBAL long_query_time = 0.5;
```

ALWAYS lower `long_query_time` to `0.5` or below for OLTP. If the resulting volume is too high, thin it with `log_slow_rate_limit` or `log_slow_filter`, NEVER by raising the threshold back up.

---

## AP-3 : Trusting deprecated SHOW PROFILE

### Broken

```sql
SET profiling = 1;
SELECT ... ;
SHOW PROFILE FOR QUERY 1;
SHOW PROFILES;
```

A diagnosis built on `SHOW PROFILE` output.

### Why it fails

`SHOW PROFILE` and `SHOW PROFILES` are deprecated. They were never accurate for I/O-bound work, are session-scoped (you cannot profile another connection's queries), and are scheduled for removal. Building a tuning workflow on them produces a workflow that breaks on the next upgrade and was never trustworthy in the first place.

### Correct

Use `performance_schema`. The stage breakdown via `events_stages_history_long` joined to `events_statements_history_long` gives the same "where did the time go" answer, accurately, and works across sessions :

```sql
-- 10.6+ : performance_schema = ON required
SELECT s.EVENT_NAME, ROUND(s.TIMER_WAIT/1e9, 3) AS stage_ms
  FROM performance_schema.events_stages_history_long s
  JOIN performance_schema.events_statements_history_long t
    ON s.NESTING_EVENT_ID = t.EVENT_ID
  WHERE t.SQL_TEXT LIKE 'SELECT%orders%'
  ORDER BY s.EVENT_ID DESC LIMIT 30;
```

---

## AP-4 : Treating EXPLAIN rows estimate as the actual count

### Broken

```sql
EXPLAIN SELECT ... ;
-- rows = 120 ; "only 120 rows, this query is fine"
```

A decision (ship it, do not index it) made on the `rows` column of plain EXPLAIN.

### Why it fails

`EXPLAIN` does NOT execute the query. The `rows` column is an OPTIMIZER ESTIMATE derived from index statistics. When statistics are stale (after a bulk load with no `ANALYZE TABLE`), the estimate can be off by orders of magnitude. A plan that "reads 120 rows" on paper can read 12 million in reality.

### Correct

```sql
-- 10.1+ : ANALYZE actually runs the query and reports observed numbers
ANALYZE FORMAT=JSON SELECT ... ;
-- compare rows (estimate) with r_rows (actual) per table node
```

ALWAYS validate an EXPLAIN-based decision with `ANALYZE FORMAT=JSON` before concluding a query is fine. If `rows` and `r_rows` diverge, run `ANALYZE TABLE` to refresh statistics, then re-plan.

---

## AP-5 : Adding an index to every column that appears in a slow query

### Broken

```sql
-- "this query is slow, the WHERE has these columns, index them all"
CREATE INDEX idx_a ON orders (is_active);     -- 2 distinct values
CREATE INDEX idx_b ON orders (order_type);    -- 3 distinct values
CREATE INDEX idx_c ON orders (region);        -- 4 distinct values
```

### Why it fails

An index on a low-cardinality column (a boolean, a 3-value enum) is almost never used by the optimizer : scanning the index plus fetching half the table costs more than a plain table scan. Each useless index still has to be maintained on every INSERT, UPDATE, and DELETE, so it directly slows down writes for zero read benefit. Indexing reflexively makes the database slower overall.

### Correct

```sql
-- 10.6+ : check selectivity FIRST
SELECT COUNT(DISTINCT region) AS distinct_vals, COUNT(*) AS total
  FROM orders;

-- index only when distinct_vals / total is high enough,
-- and prefer a composite index that matches WHERE + ORDER BY
CREATE INDEX idx_orders_status_created ON orders (status, created_at);
```

ALWAYS run a `COUNT(DISTINCT col)` selectivity check before adding an index. Prefer one well-chosen composite index over several single-column indexes.

---

## AP-6 : Ignoring Using temporary on a big GROUP BY

### Broken

```sql
EXPLAIN SELECT customer_id, SUM(total) FROM orders GROUP BY customer_id;
-- Extra : Using temporary
-- "it returns results, ship it"
```

### Why it fails

`Using temporary` means MariaDB built an internal temporary table to perform the GROUP BY. On a small result that is harmless. On millions of grouped rows the temp table spills from memory to disk (`Using temporary` plus a disk temp table), turning an aggregate query into a disk-I/O storm. Under concurrent load these disk temp tables compound and the whole server slows down.

### Correct

```sql
-- 10.6+ : an index on the GROUP BY column lets MariaDB
-- group by reading the index in order, no temp table
CREATE INDEX idx_orders_customer_total ON orders (customer_id, total);

EXPLAIN SELECT customer_id, SUM(total) FROM orders GROUP BY customer_id;
-- Extra : Using index    (temp table eliminated)
```

ALWAYS investigate `Using temporary` against the result-set size. Monitor `sum_created_tmp_disk_tables` in `events_statements_summary_by_digest` to catch the disk-spilling cases.

---

## AP-7 : log_output = TABLE in a write-heavy production system

### Broken

```sql
SET GLOBAL log_output = 'TABLE';
-- slow queries now go to mysql.slow_log
```

### Why it fails

`log_output = TABLE` writes every slow-log entry as a row in `mysql.slow_log`. On a write-heavy system this doubles write amplification : every slow query produces an extra INSERT, the slow-log table itself can become contended, and EXPLAIN-in-slow-log (`log_slow_verbosity=explain`) is silently dropped because the KB states EXPLAIN is recorded only for FILE output. The diagnostic tool becomes part of the performance problem.

### Correct

```sql
-- 10.6+ : keep the default, file-based slow log
SET GLOBAL log_output = 'FILE';
SET GLOBAL log_slow_verbosity = 'query_plan,explain';
```

ALWAYS keep `log_output = FILE` in production. File output is append-only, cheap, rotatable, and is the only mode that records the inline EXPLAIN block. Use `TABLE` only for short ad-hoc investigation on a non-production instance.

---

## AP-8 : Slow log file never rotated, disk fills

### Broken

```ini
# my.cnf : slow log enabled, no rotation configured anywhere
slow_query_log      = 1
slow_query_log_file = /var/log/mysql/mariadb-slow.log
long_query_time     = 0.5
```

No `logrotate` rule, no `FLUSH SLOW LOGS` job.

### Why it fails

With `long_query_time = 0.5` and `log_queries_not_using_indexes = ON`, a busy server can append gigabytes per day to one ever-growing file. Eventually the data partition fills, MariaDB cannot write the InnoDB redo log or the binlog, and the server stops accepting writes. A diagnostic feature took down production.

### Correct

Configure rotation. Example `logrotate` rule :

```
# /etc/logrotate.d/mariadb-slow
/var/log/mysql/mariadb-slow.log {
    daily
    rotate 14
    missingok
    compress
    delaycompress
    notifempty
    postrotate
        mysql -e 'FLUSH SLOW LOGS;'
    endscript
}
```

ALWAYS pair an enabled slow log with a rotation policy. The `postrotate` step runs `FLUSH SLOW LOGS` so MariaDB reopens a fresh file after rotation.

---

## AP-9 : Tuning one slow query in isolation, ignoring the workload top-N

### Broken

The team spends a day optimizing the single 8-second report query a manager complained about, while a 0.2-second query runs 2 million times per day.

### Why it fails

Total impact equals per-execution cost times execution count. The 8-second report run twice a day costs 16 seconds of server time daily. The 0.2-second query at 2 million calls costs 400000 seconds daily. Optimizing by anecdote instead of by aggregated total time spends effort where it barely matters.

### Correct

```sql
-- 10.6+ : rank by TOTAL time, not by any single observation
SELECT LEFT(digest_text, 80) AS query, count_star AS calls,
       ROUND(sum_timer_wait/1e12, 1) AS total_s
  FROM performance_schema.events_statements_summary_by_digest
  ORDER BY sum_timer_wait DESC LIMIT 20;
```

ALWAYS pick optimization targets from the workload top-N by total time (`pt-query-digest` Profile section or the digest table), never from a single user complaint.

---

## Sources

- `mariadb.com/kb/en/slow-query-log/` : `slow_query_log` default OFF, `log_output` modes
- `mariadb.com/kb/en/slow-query-log-overview/` : `long_query_time` default 10, `log_slow_rate_limit`, `log_slow_filter`
- `mariadb.com/kb/en/explain-in-the-slow-query-log/` : EXPLAIN recorded for FILE output only
- `mariadb.com/kb/en/analyze-statement/` : `ANALYZE FORMAT=JSON`, estimate vs observed `r_rows`
- `mariadb.com/kb/en/explain/` : EXPLAIN `rows` is an estimate, Extra flags
