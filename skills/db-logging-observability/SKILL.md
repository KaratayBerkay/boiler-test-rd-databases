---
name: db-logging-observability
description: Configure, verify and cost database logging for 20 SQL engines in Docker - slow-query logs and their thresholds, structured (JSON) server logs, audit trails (pgaudit-style DDL/DML auditing, MariaDB server_audit, Percona audit_log, SQL Server Audit, Oracle unified auditing, db2audit, CockroachDB SENSITIVE_ACCESS channel), statement statistics (pg_stat_statements, performance_schema digests, Query Store, V$SQL, system.query_log, sys.jobs_log, _query_trace, querylog_calls), log rotation, Docker json-file driver options, log collection, plus the measured throughput cost of logging every statement. Use when asked to "enable slow query log", "find slow queries", "audit DDL/DML", "structured logs", "log rotation", "where are the database logs", "logging overhead" or "observability" for these engines.
---

# Logging and observability

## The strategy (what every stack in `stacks/<engine>/` now does)
1. **Two layers**: the container runtime keeps stdout/stderr (`x-logging` anchor: Docker `json-file` driver, `max-size: 50m`,
   `max-file: 3`, `compress: true`, `tag: {{.Name}}` on every service), and the engine writes its own files/tables for what
   the runtime cannot express (statement text, durations, plans, audit records). Both are rotated.
2. **Slow-query log with a 100 ms threshold** on every engine that has one; everything else is normal-verbosity server log.
   The "log everything" mode exists only for investigations - the lab measures what it costs (table below).
3. **Structured where the engine can**: PostgreSQL `jsonlog`, MySQL `log_sink_json`, ClickHouse JSON logger, CockroachDB
   `format: json` channels, TiDB `log.format = "json"`, Percona `audit_log_format=JSON`, SQL Server `.xel`/`.sqlaudit` (queried
   with `sys.fn_xe_file_target_read_file` / `sys.fn_get_audit_file`), DuckDB `duckdb_logs()`.
4. **Audit = who did what**: DDL and DELETE on a sensitive table are the audited actions in the drill; the mechanism differs
   per engine (see table). Logins/connections are logged everywhere it is cheap (PostgreSQL 18 `log_connections=all`, MariaDB
   `CONNECT`, CockroachDB `server.auth_log.*`, SQL Server login groups, Firebird `log_connections`).
5. **Runtime-tunable**: settings that an operator may need to change during an incident must not be frozen on the command
   line (PostgreSQL `-c` outranks `ALTER SYSTEM`; TiDB config file vs `SET GLOBAL tidb_slow_log_threshold`; CockroachDB cluster
   settings propagate asynchronously - allow a few seconds).
6. **Collect**: `rdlab run <stack> --phases logging` copies the tail of every engine log into `results/<engine>/logs/` and records the
   Docker log config of every container.

## The drill (`harness/rdlab/phases/dblogging.py` + `harness/rdlab/dblogs/<engine>.py`)
For each engine: report the effective settings; run a fast and a slow (>= 300 ms) statement each tagged with a unique alias
and prove that only the slow one reaches the slow-query sink (and how long that takes); switch auditing on, run a `CREATE TABLE
audit_<marker>` (+ DDL/DELETE), find it in the audit trail; measure 8 worker processes doing primary-key lookups for 4 s with the
configured threshold vs. logging every statement (qps drop, bytes written per statement); dump the Docker log driver/rotation;
copy the log files. Results: `results/<engine>/latest.json -> phases.logging`, "Logging" table in `results/SUMMARY.md`.

## Per-engine map (details, file paths and gotchas in `references/per-engine-logging.md`)
| engine | slow-query sink (threshold) | "log everything" switch | audit trail | statement statistics |
|---|---|---|---|---|
| PostgreSQL 18 / Citus | `log_min_duration_statement=100ms` -> `logging_collector` files, `log_destination=stderr,jsonlog` | `ALTER SYSTEM SET log_min_duration_statement = 0; SELECT pg_reload_conf()` | `log_statement=ddl` (pgaudit if the image has it: `pgaudit.log='ddl,write'`) | `pg_stat_statements`, `auto_explain` JSON plans >= 500 ms |
| MySQL 9.7 | `slow_query_log`, `long_query_time=0.1`, `log_slow_extra` -> slow.log | `SET GLOBAL long_query_time = 0` (new connections) | Community: `general_log` (Enterprise Audit is commercial); error log JSON via `component_log_sink_json` | `performance_schema.events_statements_summary_by_digest`, `performance_schema.error_log` |
| MariaDB 11.8 | `slow_query_log`, `log_slow_verbosity=query_plan,explain` | `SET GLOBAL long_query_time = 0` | `server_audit` plugin (`CONNECT,QUERY_DDL`) -> audit.log | performance_schema |
| Percona XtraDB Cluster 8.4 | slow log with `log_slow_verbosity=full` (`loose-` prefix for plugin vars at `--initialize`) | `SET GLOBAL long_query_time = 0` | `audit_log` plugin JSON, `audit_log_include_commands` = DDL/account commands | performance_schema |
| TiDB 8.5 | `log.slow-threshold=100` -> tidb-slow.log = `INFORMATION_SCHEMA.SLOW_QUERY` | `SET GLOBAL tidb_slow_log_threshold = 0` | `ADMIN SHOW DDL JOBS` (audit plugin is Enterprise) | `INFORMATION_SCHEMA.STATEMENTS_SUMMARY` |
| CockroachDB 26.2 | `sql.log.slow_query.latency_threshold='100ms'` -> `slow_query` events on the **SQL_EXEC** channel (docs say SQL_PERF) | `SET CLUSTER SETTING sql.log.slow_query.latency_threshold = '1us'` (wait ~5 s) | `sql.log.admin_audit.enabled` + `ALTER TABLE t EXPERIMENTAL_AUDIT SET READ WRITE` (unlock `schema_locked` first) -> SENSITIVE_ACCESS | `crdb_internal.statement_statistics` |
| YugabyteDB 2026.1 | PG settings via `--tserver_flags=ysql_pg_conf_csv={log_min_duration_statement=100ms,...}` -> `tserver/logs/postgresql-*.log` | `ALTER DATABASE lab SET log_min_duration_statement = 0` (no ALTER SYSTEM in YSQL) | `log_statement=ddl` (pgaudit bundled) | `pg_stat_statements` |
| SQL Server 2025 | Extended Events `sql_statement_completed WHERE duration >= 100000` -> `rdlab_slow*.xel` | second XE session without filter (`ALTER EVENT SESSION rdlab_all ... STATE = START`) | SQL Server Audit -> `.sqlaudit` (`SCHEMA_OBJECT_CHANGE_GROUP`, `DELETE ON DATABASE::lab`, login groups) | Query Store (`QUERY_CAPTURE_MODE = ALL`) |
| Oracle 26ai Free (23.26) | session SQL trace (`DBMS_SESSION.SESSION_TRACE_ENABLE`) -> `.trc` with `e=` micros; `V$SQL` | `DBMS_MONITOR.DATABASE_TRACE_ENABLE` | unified auditing: `CREATE AUDIT POLICY ... ACTIONS CREATE TABLE, DROP TABLE, DELETE ON lab.inventory; AUDIT POLICY` -> `UNIFIED_AUDIT_TRAIL` | `V$SQL`, alert log `alert_FREE.log` + `alert/log.xml` |
| Db2 12.1 | `MON_GET_PKG_CACHE_STMT` (`TOTAL_ACT_TIME`) + activity event monitor `WRITE TO TABLE` | `ALTER WORKLOAD SYSDEFAULTUSERWORKLOAD COLLECT ACTIVITY DATA ON ALL WITH DETAILS` | `CREATE AUDIT POLICY ... CATEGORIES EXECUTE, OBJMAINT; AUDIT DATABASE USING POLICY` (via the CLP) + `db2audit archive/extract` | package cache, `db2diag.log` |
| ClickHouse 26.8 | `system.query_log` (`query_duration_ms`), JSON server log (`<formatting><type>json`) file + console | `SET log_queries = 1/0` per session | `system.query_log` (user, client, query_kind) + `system.session_log` | `system.query_log` aggregates |
| CrateDB 6.4 | `sys.jobs_log` + `stats.jobs_log_persistent_filter = "ended::bigint - started::bigint > 100"` -> JSON lines in the server log | `SET GLOBAL TRANSIENT stats.jobs_log_persistent_filter = 'true'` | `sys.jobs_log` (username, stmt, error) | `sys.jobs_log` |
| QuestDB 10 | `query.tracing.enabled=true` -> `_query_trace` (`execution_micros`) | always on (restart-time switch) | `_query_trace` (principal) | `_query_trace` |
| MonetDB Dec2025 | `CALL sys.querylog_enable(100)` -> `sys.querylog_calls` | `sys.querylog_enable(0)` | `sys.querylog_catalog` (owner, query) | `sys.querylog_calls` |
| Firebird 5 | system audit trace (`AuditTraceConfigFile`, `time_threshold = 100`, `log_statement_finish`) -> audit.log | user trace session `fbtracemgr -start` with `time_threshold = 0` | same trace file: `log_statement_prepare` (every statement) + `log_connections` | trace `PLAN` + reads/fetches |
| H2 2.3 | `SET QUERY_STATISTICS TRUE` -> `INFORMATION_SCHEMA.QUERY_STATISTICS` (`MAX_EXECUTION_TIME`) | `SET TRACE_LEVEL_FILE 2` -> `lab.trace.db` with `t:<ms>` | trace file level 2 | `QUERY_STATISTICS` |
| SQLite 3.45 | application wrapper timing each statement -> `slow.jsonl` | env `RDLAB_SQLITE_SLOW_MS=0` | – | – |
| DuckDB 1.5 | `CALL enable_logging('QueryLog')` -> `duckdb_logs()` (no threshold; profiling adds latency) | `enable_logging` / `disable_logging` | `duckdb_logs()` | profiling (`enable_profiling='json'`) |

## On Kubernetes (k3s runtime, `docs/kubernetes-backup-logging.md`)
The native path is stdout/stderr -> kubelet container logs (`containerLogMaxSize`/`containerLogMaxFiles`) -> a node agent (Vector, Fluent Bit,
Promtail/Alloy). Engine log *files* on PVCs (jsonlog, slow logs, audit files) become container logs through a `logs` sidecar that tails them
(`k8s/scenarios-backup.sh logs-sidecar <stack>`, then `kubectl logs deploy/<primary> -c logs`); `logging-drill <stack>` runs this phase on the cluster.

## Measured (Sept 2026; "Logging" table in `results/SUMMARY.md`)
Every engine captured the slow statement (with its duration) and filtered the fast one, and every engine that has an audit
facility recorded the DDL. The cost of logging **every** statement (8 workers, primary-key lookups) is reported per engine
as `qps baseline -> qps logged` and bytes per statement; PostgreSQL text+JSON ~1.5 KB/statement and ~10 %, Firebird trace and
Db2 activity monitor 50-70 %, SQLite application logging 66 % (the statement is ~3 µs, the log line is not), ClickHouse and
QuestDB (async table sinks) within noise. Differences below ~10 % are run-to-run noise on a shared host.

## Anti-patterns the drill caught
- PostgreSQL settings on the `command:` line cannot be changed by `ALTER SYSTEM` (postgresql.auto.conf has lower priority).
- Percona `audit_log_*` variables must be `loose-` prefixed: `mysqld --initialize` runs without plugins and aborts on unknown variables.
- CockroachDB v26 routes `slow_query` events to SQL_EXEC, not SQL_PERF; tables are `schema_locked` by default (audit ALTER fails).
- CrateDB 6: `ended - started` is an interval; cast both to bigint in `stats.jobs_log_*_filter`.
- Firebird `time_threshold` filters `EXECUTE_STATEMENT_FINISH` only; `log_statement_prepare` is the threshold-free audit trail; the
  audit trace config is read at server start (restart after editing) and the `database =` line is a regex (`%` is not a wildcard).
- H2 closes the database when the last connection leaves and `QUERY_STATISTICS` is not persisted: keep a connection open.
- Db2 `AUDIT DATABASE USING POLICY` executed through ibm_db never appeared in `SYSCAT.AUDITUSE`; through the CLP it did.
- Measuring log growth by following the *current* file breaks on rotation (CockroachDB audit log rotated mid-test): measure the directory.
