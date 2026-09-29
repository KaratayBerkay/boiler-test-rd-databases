# Per-engine logging configuration (as deployed in `stacks/<engine>/`, verified by `harness/rdlab/dblogs/<engine>.py`)

Container layer (every service in every compose file):
```yaml
x-logging: &logging
  driver: json-file
  options: { max-size: "50m", max-file: "3", compress: "true", tag: "{{.Name}}" }
```
`docker inspect -f '{{.HostConfig.LogConfig}}'` shows it; `docker logs` still works (`local` driver would too, but the Loki/Promtail
docker plugins and most tutorials expect json-file). Engine log files live on named volumes so they survive container recreation.

## PostgreSQL 18 (`stacks/postgres/init/02-logging.sh`, appended to postgresql.conf at initdb; replica inherits via pg_basebackup)
```
logging_collector = on
log_destination = 'stderr,jsonlog'            # postgresql-<date>.log (text) + .json (one JSON object per line)
log_directory = '/var/lib/postgresql/log'     # outside PGDATA: base backups stay free of logs
log_filename = 'postgresql-%Y-%m-%d.log'  log_rotation_age = 1d  log_rotation_size = 100MB  log_truncate_on_rotation = on
log_min_duration_statement = 100ms  log_lock_waits = on  log_temp_files = 0  log_checkpoints = on  log_autovacuum_min_duration = 1s
log_connections = all                         # PG18 list form: receipt, authentication, authorization, setup_durations
log_disconnections = on  log_line_prefix = '%m [%p] %q%u@%d app=%a '
auto_explain.log_min_duration = 500ms  auto_explain.log_format = json  auto_explain.log_nested_statements = on
```
`shared_preload_libraries=pg_stat_statements,auto_explain` stays on the command line (restart-only). Runtime: `ALTER SYSTEM SET ...;
SELECT pg_reload_conf();` `pg_ls_logdir()`, `pg_current_logfile('jsonlog')`. JSON record fields: `timestamp, user, dbname,
application_name, pid, session_id, error_severity, message ("duration: 301.131 ms  statement: ..."), query_id`.
pgaudit (not in the pgvector image): `shared_preload_libraries=pgaudit`, `pgaudit.log = 'ddl, write'`, `pgaudit.log_relation = on`.
With `logging_collector=on`, `docker logs` only shows the collector's startup lines - that is the trade-off for jsonlog.

## MySQL 9.7 (`stacks/mysql/compose.yaml`, `init/setup.sh`)
```
--slow-query-log=ON --long-query-time=0.1 --log-slow-extra=ON --slow-query-log-file=/var/log/mysql/slow.log
--log-error=/var/log/mysql/error.log --log-error-verbosity=3 --log-timestamps=SYSTEM --general-log=OFF --innodb-print-all-deadlocks=ON
INSTALL COMPONENT 'file://component_log_sink_json';  SET PERSIST log_error_services = 'log_filter_internal; log_sink_internal; log_sink_json';
```
-> `error.log` + `error.log.00.json`. Slow log entry (`log_slow_extra`): `# Query_time: 0.300205 Lock_time: ... Rows_sent: 1 Rows_examined: 1
Thread_id: 78 Errno: 0 Killed: 0 Bytes_received: 45 Bytes_sent: 74 Read_first... Created_tmp_tables... Start: ... End: ...`.
`SET GLOBAL long_query_time` applies to new sessions. `performance_schema.events_statements_summary_by_digest`, `performance_schema.error_log`.

## MariaDB 11.8 (`stacks/mariadb/compose.yaml`)
```
--slow-query-log=ON --long-query-time=0.1 --log-slow-verbosity=query_plan,explain --slow-query-log-file=/var/log/mysql/slow.log
--log-error=/var/log/mysql/error.log --log-warnings=3
--plugin-load-add=server_audit --server-audit-logging=ON --server-audit-events=CONNECT,QUERY_DDL --server-audit-output-type=file
--server-audit-file-path=/var/log/mysql/audit.log --server-audit-file-rotate-size=100000000 --server-audit-file-rotations=3
```
audit.log line: `20260914 08:20:47,primary,lab,172.24.0.1,86,1234,QUERY,lab,'CREATE TABLE audit_x (id INTEGER)',0`.

## Percona XtraDB Cluster 8.4 (`stacks/pxc/conf/lab.cnf`, one log volume per node)
```
log_error=/var/log/mysql/error.log  slow_query_log=ON  long_query_time=0.1  log_slow_extra=ON  log_slow_verbosity=full  log_slow_rate_limit=1
plugin-load-add=audit_log=audit_log.so
loose-audit_log_format=JSON  loose-audit_log_file=/var/log/mysql/audit.json  loose-audit_log_policy=QUERIES
loose-audit_log_include_commands=create_table,drop_table,alter_table,create_db,drop_db,truncate,grant,revoke,create_user,drop_user,alter_user
loose-audit_log_rotate_on_size=100M  loose-audit_log_rotations=3
```

## TiDB 8.5 (`stacks/tidb/conf/tidb.toml`)
```toml
[log]  level = "info"  format = "json"  slow-threshold = 100  expensive-threshold = 10000  slow-query-file = "/var/log/tidb/tidb-slow.log"
[log.file]  max-size = 100  max-days = 3  max-backups = 3
```
Main log on stdout (docker), slow log in a file because `INFORMATION_SCHEMA.SLOW_QUERY` / `CLUSTER_SLOW_QUERY` read it:
`SELECT time, query_time, user, db, query FROM information_schema.slow_query WHERE query LIKE '%x%'`. Runtime: `SET GLOBAL tidb_slow_log_threshold = 0`.
DDL audit: `ADMIN SHOW DDL JOBS 30`. Statement statistics: `information_schema.statements_summary`.

## CockroachDB 26.2 (`stacks/cockroach/conf/log.yaml`, `--log-config-file`)
```yaml
file-defaults: { dir: /cockroach/cockroach-data/logs, format: json, max-file-size: 100MiB, max-group-size: 300MiB }
sinks:
  file-groups:
    default:   { channels: [DEV, OPS, HEALTH, STORAGE, SESSIONS, SQL_SCHEMA, USER_ADMIN, PRIVILEGES, TELEMETRY, KV_DISTRIBUTION] }
    sql-audit: { channels: [SENSITIVE_ACCESS], auditable: true }
    sql-perf:  { channels: [SQL_PERF, SQL_INTERNAL_PERF] }
    sql-exec:  { channels: [SQL_EXEC] }
  stderr: { channels: [OPS, HEALTH], format: json }
```
Cluster settings (init job, one per statement): `sql.log.slow_query.latency_threshold = '100ms'`, `sql.log.admin_audit.enabled = true`,
`server.auth_log.sql_connections.enabled`, `server.auth_log.sql_sessions.enabled`. v26.2 writes the `slow_query` event to
`cockroach-sql-exec.log` (`{"channel":"SQL_EXEC","event":{"EventType":"slow_query","Statement":"SELECT pg_sleep(‹0.3›) AS x","Age":301.19,...}}`).
Table audit: `ALTER TABLE inventory SET (schema_locked = false); ALTER TABLE inventory EXPERIMENTAL_AUDIT SET READ WRITE;` -> `cockroach-sql-audit.log`.
Admin audit with an admin application user logs *every* statement (73 MB in a 4 s load test): use a non-admin app role.

## YugabyteDB 2026.1 (`stacks/yugabyte/compose.yaml`)
`--tserver_flags="ysql_max_connections=500,ysql_pg_conf_csv={log_min_duration_statement=100ms,log_lock_waits=on,log_temp_files=0,log_checkpoints=on,log_connections=on,log_disconnections=on,log_line_prefix='%m [%p] %u@%d app=%a '}"`
(braces group the comma list). Logs: `/home/yugabyte/yb_data/data/yb-data/tserver/logs/postgresql-*.log`, `yb-tserver.INFO`, `master/logs/yb-master.INFO`.
Runtime: `ALTER DATABASE lab SET log_min_duration_statement = 0` (database-level GUC; YSQL has no ALTER SYSTEM). pgaudit is bundled (`CREATE EXTENSION pgaudit`).

## SQL Server 2025 (`stacks/mssql/init/setup.sh`)
```sql
CREATE EVENT SESSION [rdlab_slow] ON SERVER ADD EVENT sqlserver.sql_statement_completed (ACTION (sqlserver.sql_text, sqlserver.username, sqlserver.database_name, sqlserver.client_app_name, sqlserver.client_hostname) WHERE duration >= 100000)
  ADD TARGET package0.event_file (SET filename = N'/var/opt/mssql/log/rdlab_slow.xel', max_file_size = 100, max_rollover_files = 3) WITH (STARTUP_STATE = ON, MAX_DISPATCH_LATENCY = 2 SECONDS);
CREATE SERVER AUDIT [rdlab_audit] TO FILE (FILEPATH = N'/var/opt/mssql/log/', MAXSIZE = 100 MB, MAX_ROLLOVER_FILES = 3) WITH (QUEUE_DELAY = 1000, ON_FAILURE = CONTINUE);
CREATE SERVER AUDIT SPECIFICATION [rdlab_server_spec] FOR SERVER AUDIT [rdlab_audit] ADD (FAILED_LOGIN_GROUP), ADD (SUCCESSFUL_LOGIN_GROUP), ADD (SERVER_ROLE_MEMBER_CHANGE_GROUP), ADD (DATABASE_OBJECT_CHANGE_GROUP) WITH (STATE = ON);
CREATE DATABASE AUDIT SPECIFICATION [rdlab_db_spec] FOR SERVER AUDIT [rdlab_audit] ADD (SCHEMA_OBJECT_CHANGE_GROUP), ADD (DELETE ON DATABASE::[lab] BY [public]) WITH (STATE = ON);
ALTER DATABASE [lab] SET QUERY_STORE = ON (OPERATION_MODE = READ_WRITE, QUERY_CAPTURE_MODE = ALL, MAX_STORAGE_SIZE_MB = 512, INTERVAL_LENGTH_MINUTES = 1);
```
Read: `sys.fn_xe_file_target_read_file(N'/var/opt/mssql/log/rdlab_slow*.xel', NULL, NULL, NULL)` (XML `event_data`),
`sys.fn_get_audit_file(N'/var/opt/mssql/log/rdlab_audit*.sqlaudit', DEFAULT, DEFAULT)`, `sys.query_store_runtime_stats`; errorlog `/var/opt/mssql/log/errorlog`.

## Oracle 26ai Free (23.26)
```sql
GRANT ALTER SESSION, SELECT_CATALOG_ROLE TO lab;                          -- as SYSDBA in FREEPDB1
ALTER SESSION SET TRACEFILE_IDENTIFIER = 'rdlab'; EXEC DBMS_SESSION.SESSION_TRACE_ENABLE(waits => TRUE, binds => FALSE);
SELECT value FROM v$diag_info WHERE name = 'Default Trace File';        -- ...FREE_ora_1234_rdlab.trc: PARSE/EXEC/FETCH #cursor:c=,e=micros
EXEC DBMS_MONITOR.DATABASE_TRACE_ENABLE(waits => FALSE, binds => FALSE); -- every session (investigation only)
CREATE AUDIT POLICY rdlab_pol ACTIONS CREATE TABLE, DROP TABLE, DELETE ON lab.inventory;  AUDIT POLICY rdlab_pol;
SELECT event_timestamp, dbusername, action_name, object_name, sql_text FROM unified_audit_trail WHERE object_name = 'AUDIT_X';
SELECT executions, elapsed_time/1000 ms, sql_text FROM v$sql WHERE parsing_schema_name = 'LAB' ORDER BY elapsed_time DESC;
```
Alert log: `SELECT value FROM v$diag_info WHERE name = 'Diag Trace'` -> `alert_FREE.log`; XML `alert/log.xml`.

## Db2 12.1 (`su - db2inst1 -c "db2 ..."`)
```
db2 get dbm cfg | grep DIAG                         # DIAGLEVEL 3, DIAGPATH -> /database/config/db2inst1/sqllib/db2dump/DIAG0000/db2diag.log
CREATE EVENT MONITOR RDLAB_ACT FOR ACTIVITIES WRITE TO TABLE AUTOSTART; SET EVENT MONITOR RDLAB_ACT STATE 1;
ALTER WORKLOAD SYSDEFAULTUSERWORKLOAD COLLECT ACTIVITY DATA ON ALL DATABASE PARTITIONS WITH DETAILS;   -- statement log -> ACTIVITYSTMT_RDLAB_ACT
FLUSH EVENT MONITOR RDLAB_ACT;
SELECT NUM_EXECUTIONS, TOTAL_ACT_TIME, VARCHAR(STMT_TEXT, 100) FROM TABLE(MON_GET_PKG_CACHE_STMT(NULL, NULL, NULL, -2)) WHERE TOTAL_ACT_TIME >= 100;
CREATE AUDIT POLICY RDLAB_POL CATEGORIES EXECUTE STATUS BOTH, OBJMAINT STATUS BOTH ERROR TYPE NORMAL;  AUDIT DATABASE USING POLICY RDLAB_POL;   -- CLP
db2audit flush; db2audit archive database lab to /backups/audit; db2audit extract delasc to /backups/audit/out from files /backups/audit/db2audit.db.*.log.*
grep -a 'AUDIT_X' /backups/audit/out/objmaint.del      # statement text lives in auditlobs / objmaint.del
```

## ClickHouse 26.8 (`stacks/clickhouse/config/logging.xml`)
```xml
<logger><level>information</level><log>/var/log/clickhouse-server/clickhouse-server.log</log><errorlog>...err.log</errorlog>
        <size>100M</size><count>3</count><console>1</console><formatting><type>json</type></formatting></logger>
<query_log><flush_interval_milliseconds>1000</flush_interval_milliseconds><ttl>event_date + INTERVAL 7 DAY</ttl></query_log>
<session_log>...</session_log> <text_log><level>information</level>...</text_log> <part_log>...</part_log>
```
`SYSTEM FLUSH LOGS; SELECT event_time, query_duration_ms, user, query_kind, read_rows, memory_usage, query FROM system.query_log WHERE type = 'QueryFinish' AND query_duration_ms >= 100`.
Per-session `SET log_queries = 0` switches the query_log off (`log_queries_min_query_duration_ms` keeps only slow ones).

## CrateDB 6.4 (node settings)
`-Cstats.enabled=true -Cstats.jobs_log_size=20000 -Cstats.jobs_log_expiration=1h "-Cstats.jobs_log_persistent_filter=ended::bigint - started::bigint > 100"`
-> slow jobs as JSON lines in the server log (docker logs); `SELECT started, ended, username, stmt, error FROM sys.jobs_log`.
Runtime: `SET GLOBAL TRANSIENT stats.jobs_log_persistent_filter = 'true'`.

## QuestDB 10
`QDB_QUERY_TRACING_ENABLED=true` -> `SELECT ts, principal, execution_micros, query_text FROM _query_trace WHERE execution_micros >= 100000` (`SET query.trace = true` per session).

## MonetDB Dec2025
`CALL sys.querylog_enable(100)` (ms) -> `SELECT c."start", c."stop", c.run, c.tuples, q.owner, q.query FROM sys.querylog_calls c JOIN sys.querylog_catalog q ON c.id = q.id`; `sys.querylog_disable()`; server log `/var/monetdb5/dbfarm/merovingian.log`.

## Firebird 5 (`stacks/firebird/conf/fbtrace.conf`, `FIREBIRD_CONF_AuditTraceConfigFile=/etc/firebird/fbtrace.conf`)
```
database = /var/lib/firebird/data/lab.fdb      # a regex, not a glob (the classic %[\\/]lab\.fdb example fails to compile in 5.x)
{ enabled = true  log_filename = /var/log/firebird/audit.log  max_log_size = 100
  log_connections = true  log_statement_prepare = true  log_statement_finish = true  log_errors = true
  time_threshold = 100  max_sql_length = 2000  print_plan = true }
```
`EXECUTE_STATEMENT_FINISH` events carry `<n> ms, <reads> read(s), <fetches> fetch(es)` and are the ones `time_threshold` filters;
`PREPARE_STATEMENT` logs every statement (audit). On-demand full trace: `fbtracemgr -se localhost:service_mgr -user SYSDBA -password x -start -name full -config /tmp/full.conf` (`-list`, `-stop -id N`).
`firebird.log` = server messages. The server reads the audit config at start.

## H2 2.3
`SET QUERY_STATISTICS TRUE; SET QUERY_STATISTICS_MAX_ENTRIES 1000;` -> `INFORMATION_SCHEMA.QUERY_STATISTICS (SQL_STATEMENT, EXECUTION_COUNT, MIN/MAX/AVERAGE_EXECUTION_TIME, ...)`;
`SET TRACE_LEVEL_FILE 2` (+ `SET TRACE_MAX_FILE_SIZE 64`) -> `/data/lab.trace.db` with `/*SQL #:1 t:120*/SELECT ...` and `command: slow query: 120 ms` lines. Keep one connection open.

## SQLite / DuckDB
SQLite: the application wraps `execute()` (`harness/rdlab/engines/sqlite.py`): `RDLAB_SQLITE_SLOW_MS=100`, `RDLAB_SQLITE_SLOW_LOG=results/sqlite/logs/slow.jsonl`
(one line-buffered handle per process; `sqlite3_trace_v2` / `Connection.set_trace_callback` is the C-level hook).
DuckDB: `CALL enable_logging('QueryLog')` (or `storage='file', storage_path='dir'` -> `duckdb_log_entries.csv`), `SELECT timestamp, type, log_level, message FROM duckdb_logs()`,
`CALL disable_logging()`; `PRAGMA enable_profiling='json'; PRAGMA profiling_output='...'` for per-query latency/operator profiles.
