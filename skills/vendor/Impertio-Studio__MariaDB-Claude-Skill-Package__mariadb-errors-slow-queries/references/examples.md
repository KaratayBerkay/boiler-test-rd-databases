# Examples : MariaDB Slow Query Diagnosis

Working, copy-paste examples for `mariadb-errors-slow-queries`. Every example is version-annotated. All SQL targets MariaDB 10.6-LTS / 10.11-LTS / 11.x / 12.x unless a tighter range is given.

---

## Example 1 : Enable the slow query log without a restart

```sql
-- 10.6+ : turn on at runtime, capture sub-second queries with full plan info
SET GLOBAL slow_query_log         = ON;
SET GLOBAL long_query_time        = 0.5;
SET GLOBAL log_slow_verbosity     = 'query_plan,explain';
SET GLOBAL log_queries_not_using_indexes = ON;

-- verify it took
SELECT @@slow_query_log, @@long_query_time, @@log_slow_verbosity,
       @@log_output, @@slow_query_log_file;
```

For 10.11+ the alias names also work :

```sql
-- 10.11+ : identical effect via the renamed variables
SET GLOBAL log_slow_query        = ON;
SET GLOBAL log_slow_query_time   = 0.5;
```

Persist in `my.cnf` so it survives restart :

```ini
# my.cnf, [mariadb] section
[mariadb]
slow_query_log               = 1
slow_query_log_file          = /var/log/mysql/mariadb-slow.log
long_query_time              = 0.5
log_slow_verbosity           = query_plan,explain
log_queries_not_using_indexes = 1
```

---

## Example 2 : Trace ONE session without polluting the global slow log

When debugging a single application connection, log every statement from that session only :

```sql
-- 10.6+ : run inside the session you want to trace
SET SESSION long_query_time      = 0;       -- 0 = log everything
SET SESSION log_slow_verbosity   = 'query_plan,explain';
SET SESSION slow_query_log       = ON;

-- ... run the suspect queries here ...

-- turn it back off when done
SET SESSION slow_query_log       = OFF;
```

The global slow log keeps its `long_query_time` threshold. Only this session logs everything.

---

## Example 3 : Aggregate a slow log with pt-query-digest

```bash
# Percona Toolkit installed separately. Reads MariaDB slow-log unchanged.
pt-query-digest /var/log/mysql/mariadb-slow.log > /tmp/digest.txt

# top 20 queries that make up 95% of total response time, last 24 hours
pt-query-digest --since '24h' --limit '95%:20' \
  /var/log/mysql/mariadb-slow.log > /tmp/digest-24h.txt

# read the ranked Profile table first
sed -n '/# Profile/,/^$/p' /tmp/digest-24h.txt
```

Always start with Rank 1-5 in the `# Profile` section. Those are where the time goes.

---

## Example 4 : mariadb-dumpslow fallback (no Percona Toolkit)

```bash
# 10.6+ : bundled tool, sort by total time, top 20
mariadb-dumpslow -s t -t 20 /var/log/mysql/mariadb-slow.log

# sort by average time instead, useful for "rare but brutal" queries
mariadb-dumpslow -s at -t 20 /var/log/mysql/mariadb-slow.log

# only queries touching the orders table
mariadb-dumpslow -s t -g 'orders' /var/log/mysql/mariadb-slow.log
```

`mariadb-dumpslow` normalises literals automatically, so `WHERE id = 1` and `WHERE id = 99` group as one family.

---

## Example 5 : Top-10 queries from performance_schema

```sql
-- 10.6+ : requires performance_schema = ON (set in my.cnf, needs restart)
SELECT
    LEFT(digest_text, 80)                       AS query,
    count_star                                  AS calls,
    ROUND(sum_timer_wait  / 1e12, 2)            AS total_s,
    ROUND(avg_timer_wait  / 1e9,  2)            AS avg_ms,
    ROUND(max_timer_wait  / 1e9,  2)            AS max_ms,
    sum_rows_examined / NULLIF(count_star, 0)   AS avg_rows_examined
  FROM performance_schema.events_statements_summary_by_digest
  ORDER BY sum_timer_wait DESC
  LIMIT 10;
```

`sum_timer_wait` is in picoseconds : divide by `1e12` for seconds, `1e9` for milliseconds.

---

## Example 6 : Profile one query with ANALYZE FORMAT=JSON

```sql
-- 10.1+ : runs the query, returns plan + observed numbers ; SELECT-safe
ANALYZE FORMAT=JSON
SELECT o.id, o.total, c.name
  FROM orders o
  JOIN customers c ON c.id = o.customer_id
  WHERE o.status = 'pending'
  ORDER BY o.created_at DESC
  LIMIT 50;
```

In the JSON output, for each table node compare :
- `rows` : optimizer estimate
- `r_rows` : actual rows read
- `r_filtered` : actual fraction surviving the WHERE
- `r_total_time_ms` : actual time spent at this node

If `rows` and `r_rows` diverge by 10x or more, statistics are stale : run `ANALYZE TABLE orders;`.

WARNING : never run `ANALYZE FORMAT=JSON UPDATE ...` or `ANALYZE FORMAT=JSON DELETE ...` as a "dry run". Per the KB those statements actually execute the mutation. Only `ANALYZE SELECT` discards results.

---

## Example 7 : Detect missing indexes from the digest table

```sql
-- 10.6+ : digests that ran at least once without using an index,
-- ranked by total rows scanned
SELECT
    LEFT(digest_text, 80)  AS query,
    count_star             AS calls,
    sum_no_index_used      AS no_index_executions,
    sum_rows_examined      AS total_rows_scanned,
    sum_rows_examined / NULLIF(sum_rows_sent, 0) AS scan_to_send_ratio
  FROM performance_schema.events_statements_summary_by_digest
  WHERE sum_no_index_used > 0
  ORDER BY sum_rows_examined DESC
  LIMIT 20;
```

A high `scan_to_send_ratio` (thousands-to-one) on a frequently-called digest is the strongest missing-index signal.

---

## Example 8 : Confirm a missing index with EXPLAIN, then add it

```sql
-- 10.6+ : EXPLAIN shows the problem
EXPLAIN
SELECT id, total FROM orders WHERE status = 'pending';
-- type = ALL, key = NULL, rows = 1245678  -> full table scan

-- check selectivity BEFORE adding the index
SELECT COUNT(DISTINCT status) AS distinct_values, COUNT(*) AS total_rows
  FROM orders;
-- distinct_values = 6, total_rows = 1245678  -> selective enough to help

-- 10.6+ : add the index
CREATE INDEX idx_orders_status ON orders (status);

-- re-check
EXPLAIN
SELECT id, total FROM orders WHERE status = 'pending';
-- type = ref, key = idx_orders_status  -> fixed
```

ALWAYS run the `COUNT(DISTINCT ...)` selectivity check first. An index on a near-constant column wastes write throughput for zero read benefit.

---

## Example 9 : Fix Using filesort with a descending index (10.8+)

A query sorting one column ascending and another descending cannot use a plain ascending index :

```sql
-- 10.6+ : EXPLAIN shows the sort problem
EXPLAIN
SELECT id, name FROM events
  WHERE region = 'EU'
  ORDER BY priority ASC, created_at DESC;
-- Extra : Using where ; Using filesort

-- 10.8+ : descending index supports the mixed ORDER BY direction
CREATE INDEX idx_events_region_prio_created
  ON events (region, priority ASC, created_at DESC);

-- re-check
EXPLAIN
SELECT id, name FROM events
  WHERE region = 'EU'
  ORDER BY priority ASC, created_at DESC;
-- Extra : Using where    (Using filesort is gone)
```

For 10.6-LTS / 10.11-LTS, descending indexes are NOT available. The fallback there is to reverse the query logic, or accept the filesort if the result set is small.

---

## Example 10 : Detect Using temporary on a big GROUP BY and fix it

```sql
-- 10.6+ : GROUP BY on an unindexed column forces a temp table
EXPLAIN
SELECT customer_id, COUNT(*) AS order_count
  FROM orders
  GROUP BY customer_id;
-- Extra : Using temporary    (intermediate temp table built for grouping)

-- 10.6+ : an index on the GROUP BY column lets the optimizer
-- group by reading the index in order, no temp table
CREATE INDEX idx_orders_customer ON orders (customer_id);

-- re-check
EXPLAIN
SELECT customer_id, COUNT(*) AS order_count
  FROM orders
  GROUP BY customer_id;
-- Extra : Using index    (temp table gone, covering index used)
```

`Using temporary` on a small result set is harmless. On millions of grouped rows it spills to disk : check `sum_created_tmp_disk_tables` in `events_statements_summary_by_digest`.

---

## Example 11 : Thin a noisy slow log with log_slow_rate_limit

When a workload generates thousands of marginally-slow queries, log a representative sample instead of all of them :

```sql
-- 10.6+ : log only 1 in every 100 otherwise-eligible slow queries
SET GLOBAL log_slow_rate_limit = 100;

-- but ALWAYS log the genuinely terrible ones regardless of the rate limit
SET GLOBAL log_slow_always_query_time = 5;
```

`log_slow_rate_limit` reduces slow-log I/O without losing the statistical shape of the workload. `pt-query-digest` corrects for the sampling rate.

---

## Example 12 : Restrict logging to bad-plan queries only

```sql
-- 10.6+ : log ONLY queries whose plan has a disk filesort,
-- a disk temp table, or a full scan
SET GLOBAL log_slow_filter = 'filesort_on_disk,tmp_table_on_disk,full_scan';
```

This skips sub-second indexed queries that crossed `long_query_time` only because of lock contention or a cold buffer pool, keeping the slow log focused on genuine plan defects.

---

## Example 13 : Per-stage timing as a SHOW PROFILE replacement

```sql
-- 10.6+ : requires performance_schema = ON
-- find the most recent execution of a query and break it into stages
SELECT
    s.EVENT_NAME,
    ROUND(s.TIMER_WAIT / 1e9, 3) AS stage_ms
  FROM performance_schema.events_stages_history_long s
  JOIN performance_schema.events_statements_history_long t
    ON s.NESTING_EVENT_ID = t.EVENT_ID
  WHERE t.SQL_TEXT LIKE 'SELECT%FROM orders%'
  ORDER BY t.EVENT_ID DESC, s.EVENT_ID ASC
  LIMIT 30;
```

Stage names like `stage/sql/Sending data`, `stage/sql/Creating sort index`, and `stage/sql/Copying to tmp table` pinpoint exactly where the time went. This replaces deprecated `SHOW PROFILE`.

---

## Example 14 : Galera-aware slow-log aggregation across nodes

On a 3-node Galera cluster, the same write-set appears in the slow log of every node. Aggregate per node, then deduplicate by digest :

```bash
# run on each node, tag the output with the hostname
for node in db1 db2 db3; do
  ssh "$node" 'pt-query-digest /var/log/mysql/mariadb-slow.log' \
    > "/tmp/digest-${node}.txt"
done
```

The Query IDs (digest hashes) in `pt-query-digest` output are stable across nodes : the same query family has the same `0x...` ID everywhere. For reads, per-node digests reveal read-distribution skew ; for writes, expect the same write digests on all nodes (replication, not extra load).

---

## Sources

- `mariadb.com/kb/en/slow-query-log/` : variable list, entry format
- `mariadb.com/kb/en/slow-query-log-overview/` : `log_slow_filter` flags, 10.11+ variable renames
- `mariadb.com/kb/en/explain-in-the-slow-query-log/` : `log_slow_verbosity=explain`, FILE-only constraint
- `mariadb.com/kb/en/analyze-statement/` : `ANALYZE FORMAT=JSON`, `r_rows`/`r_filtered`, mutation warning
- `mariadb.com/kb/en/explain/` : EXPLAIN columns and Extra flags
