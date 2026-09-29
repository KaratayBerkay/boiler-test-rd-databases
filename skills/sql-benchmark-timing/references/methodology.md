# Benchmark methodology checklist (expanded)

## Before measuring
- [ ] Data scale documented (rows per table, generator seed). The lab uses a deterministic generator (`rdlab gen --scale N`; seed 20260912) so every engine sees identical data.
- [ ] Statistics refreshed after load (`ANALYZE` etc.) unless "stale stats" is the experiment.
- [ ] Engine settings pinned in compose (buffer pool / shared_buffers, fsync mode, max connections). The lab uses shared_buffers=2GB / innodb_buffer_pool_size=2G so the 1M-row `events` table fits in memory: numbers are CPU-bound, not IO-bound.
- [ ] Nothing else heavy running on the host (`docker stats`, `uptime` load).

## While measuring
- [ ] 2 warm-ups + >= 10 samples (or a time budget, >= 3 samples).
- [ ] Parameters cycled per iteration so the engine cannot return a cached result (`params` callables in `rdlab/queries.py`); MySQL 8+ has no query cache, but ClickHouse has (`query_cache`, off by default) and QuestDB's HTTP endpoint has one (disabled in the lab config).
- [ ] Prepared vs text protocol noted. psycopg auto-prepares after 5 executions (`prepare_threshold`); set it to `None` for an honest "unprepared" number.
- [ ] Connection reuse: one connection per worker, opened before the timer starts (connection setup is 1-30 ms - see the connection skill).
- [ ] Multi-process workers for throughput; record `queries`, `errors`, and the first error text.

## After measuring
- [ ] Checksum/row count identical between variants.
- [ ] Percentiles reported (p50, p95, p99), plus min/max; the mean only as a sanity check.
- [ ] Keep raw samples (`samples_ms`) for later re-analysis.
- [ ] Plans stored next to timings.

## Pitfalls seen in this lab
- PyMySQL `executemany` only rewrites `INSERT ... VALUES (%s, %s)` into a multi-row statement when the VALUES clause contains *nothing but placeholders*; a literal or function inside VALUES silently falls back to one statement per row (1k rows/s instead of 30k).
- PyMySQL/pymssql expose `autocommit` as a *method*; assigning `conn.autocommit = False` silently does nothing and every statement autocommits.
- Python `sqlite3`: with the legacy `isolation_level` API a SELECT does not open a transaction; use `autocommit=False` (3.12+) to get a snapshot for isolation tests.
- DuckDB's `connection.cursor()` is a *new connection* with its own transaction; statements meant for one transaction must run on the same connection object.
- libpq conninfo: `password= dbname=lab` makes the password `"dbname=lab"`; omit empty keys.
- ClickHouse HTTP sessions are stateless: `SET x` on one request does not affect the next; use `SETTINGS x = y` on the query.
- ClickHouse LEFT JOIN returns default values (0, '') for non-matching rows unless `join_use_nulls = 1` - it changed a cohort query's row count from 22 to 23.
- GROUP BY on `DATE_FORMAT(ts, '%Y-%m-01')` through a `%`-paramstyle driver needs `%%` only when parameters are bound.
- Docker Hub anonymous pulls are limited to 100 manifests per 6 hours per IP; `mirror.gcr.io` serves the same images without the limit (`scripts/pull-images.sh`).
