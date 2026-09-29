#!/usr/bin/env bash
# One-shot: create lab users/db on the source (replicated to the replica via GTID), then point the replica at the source.
set -euo pipefail
S="mysql -h source -uroot -prootpass"
R="mysql -h replica -uroot -prootpass"
for i in $(seq 1 60); do $S -e 'SELECT 1' >/dev/null 2>&1 && $R -e 'SELECT 1' >/dev/null 2>&1 && break; sleep 2; done
$S <<'SQL'
CREATE DATABASE IF NOT EXISTS lab;
CREATE USER IF NOT EXISTS 'lab'@'%' IDENTIFIED BY 'labpass';
GRANT ALL PRIVILEGES ON lab.* TO 'lab'@'%';
GRANT REPLICATION CLIENT, REPLICATION SLAVE, PROCESS, SELECT ON *.* TO 'lab'@'%';
CREATE USER IF NOT EXISTS 'repl'@'%' IDENTIFIED BY 'replpass';
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';
CREATE USER IF NOT EXISTS 'monitor'@'%' IDENTIFIED BY 'monitorpass';
GRANT REPLICATION CLIENT, PROCESS, SELECT ON *.* TO 'monitor'@'%';
FLUSH PRIVILEGES;
SQL
# logging strategy: structured JSON error log next to the text one (component is persisted in mysql.component,
# the setting in mysqld-auto.cnf) -> /var/log/mysql/error.log.00.json
$S -e "INSTALL COMPONENT 'file://component_log_sink_json'" 2>/dev/null || true
$S -e "SET PERSIST log_error_services = 'log_filter_internal; log_sink_internal; log_sink_json'"
$R <<'SQL'
STOP REPLICA;
CHANGE REPLICATION SOURCE TO SOURCE_HOST='source', SOURCE_PORT=3306, SOURCE_USER='repl', SOURCE_PASSWORD='replpass',
  SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1, SOURCE_CONNECT_RETRY=5;
START REPLICA;
SET PERSIST read_only = ON;
SET PERSIST super_read_only = ON;
SQL
# wait until the replica has applied the user creation
for i in $(seq 1 60); do
  if $R -E -e "SHOW REPLICA STATUS" | grep -qE "Replica_SQL_Running: Yes" && mysql -h replica -ulab -plabpass -e 'SELECT 1' >/dev/null 2>&1; then
    echo "replica replicating and lab user visible"; $R -E -e "SHOW REPLICA STATUS" | grep -E "Running|Seconds_Behind|Executed_Gtid|Last_.*Error"; exit 0
  fi
  sleep 2
done
echo "replica did not come up"; $R -E -e "SHOW REPLICA STATUS"; exit 1
