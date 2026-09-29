#!/usr/bin/env bash
# Builds a two-replica read-scale Availability Group (CLUSTER_TYPE = NONE) between mssql1 and mssql2.
set -euo pipefail
SA='LabPass_2026!'
Q1() { /opt/mssql-tools18/bin/sqlcmd -C -S mssql1 -U sa -P "$SA" -b -Q "$1"; }
Q2() { /opt/mssql-tools18/bin/sqlcmd -C -S mssql2 -U sa -P "$SA" -b -Q "$1"; }
V2() { /opt/mssql-tools18/bin/sqlcmd -C -S mssql2 -U sa -P "$SA" -b -h -1 -W -Q "SET NOCOUNT ON; $1" | tr -d ' \r' | grep -v '^$' | tail -1; }
ok=0
for i in $(seq 1 60); do
  if Q1 "SELECT 1" >/dev/null 2>&1 && Q2 "SELECT 1" >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
[ "$ok" = "1" ] || { echo "SQL Server instances did not become reachable"; exit 1; }
exists=$(V2 "SELECT COUNT(*) FROM sys.availability_groups WHERE name = 'ag1'")
if [ "$exists" = "1" ]; then echo "AG already configured"; exit 0; fi
chmod 0777 /var/opt/mssql/shared 2>/dev/null || true     # named volume is root-owned; mssql runs as uid 10001
rm -f /var/opt/mssql/shared/dbm_certificate.* 2>/dev/null || true
echo "== certificates + endpoints"
Q1 "CREATE MASTER KEY ENCRYPTION BY PASSWORD = 'Ag_MasterKey_2026!';
CREATE CERTIFICATE dbm_certificate WITH SUBJECT = 'dbm';
BACKUP CERTIFICATE dbm_certificate TO FILE = '/var/opt/mssql/shared/dbm_certificate.cer'
  WITH PRIVATE KEY (FILE = '/var/opt/mssql/shared/dbm_certificate.pvk', ENCRYPTION BY PASSWORD = 'Ag_PvkKey_2026!');
CREATE ENDPOINT [Hadr_endpoint] AS TCP (LISTENER_PORT = 5022) FOR DATABASE_MIRRORING (ROLE = ALL, AUTHENTICATION = CERTIFICATE dbm_certificate, ENCRYPTION = REQUIRED ALGORITHM AES);
ALTER ENDPOINT [Hadr_endpoint] STATE = STARTED;"
Q2 "CREATE MASTER KEY ENCRYPTION BY PASSWORD = 'Ag_MasterKey_2026!';
CREATE CERTIFICATE dbm_certificate FROM FILE = '/var/opt/mssql/shared/dbm_certificate.cer'
  WITH PRIVATE KEY (FILE = '/var/opt/mssql/shared/dbm_certificate.pvk', DECRYPTION BY PASSWORD = 'Ag_PvkKey_2026!');
CREATE ENDPOINT [Hadr_endpoint] AS TCP (LISTENER_PORT = 5022) FOR DATABASE_MIRRORING (ROLE = ALL, AUTHENTICATION = CERTIFICATE dbm_certificate, ENCRYPTION = REQUIRED ALGORITHM AES);
ALTER ENDPOINT [Hadr_endpoint] STATE = STARTED;"
echo "== availability group"
Q1 "CREATE AVAILABILITY GROUP [ag1] WITH (CLUSTER_TYPE = NONE, DB_FAILOVER = ON) FOR REPLICA ON
  N'mssql1' WITH (ENDPOINT_URL = N'tcp://mssql1:5022', AVAILABILITY_MODE = SYNCHRONOUS_COMMIT, FAILOVER_MODE = MANUAL, SEEDING_MODE = AUTOMATIC, SECONDARY_ROLE (ALLOW_CONNECTIONS = ALL)),
  N'mssql2' WITH (ENDPOINT_URL = N'tcp://mssql2:5022', AVAILABILITY_MODE = SYNCHRONOUS_COMMIT, FAILOVER_MODE = MANUAL, SEEDING_MODE = AUTOMATIC, SECONDARY_ROLE (ALLOW_CONNECTIONS = ALL));
ALTER AVAILABILITY GROUP [ag1] GRANT CREATE ANY DATABASE;"
Q2 "ALTER AVAILABILITY GROUP [ag1] JOIN WITH (CLUSTER_TYPE = NONE);
ALTER AVAILABILITY GROUP [ag1] GRANT CREATE ANY DATABASE;"
echo "== database lab"
Q1 "CREATE DATABASE lab;
ALTER DATABASE lab SET RECOVERY FULL;
BACKUP DATABASE lab TO DISK = '/var/opt/mssql/data/lab.bak' WITH INIT;
ALTER AVAILABILITY GROUP [ag1] ADD DATABASE [lab];"
for i in $(seq 1 60); do
  st=$(V2 "SELECT synchronization_state_desc FROM sys.dm_hadr_database_replica_states drs JOIN sys.databases d ON d.database_id = drs.database_id WHERE d.name = 'lab' AND drs.is_local = 1" 2>/dev/null || true)
  echo "secondary state: $st"
  [ "$st" = "SYNCHRONIZED" ] && { echo "AG ready"; break; }
  sleep 3
done
[ "$st" = "SYNCHRONIZED" ] || { echo "secondary never synchronized"; exit 1; }
echo "== logging strategy: Extended Events, SQL Server Audit, Query Store"
Q1 "IF NOT EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = 'rdlab_slow')
CREATE EVENT SESSION [rdlab_slow] ON SERVER
  ADD EVENT sqlserver.sql_statement_completed (ACTION (sqlserver.sql_text, sqlserver.username, sqlserver.database_name, sqlserver.client_app_name, sqlserver.client_hostname) WHERE duration >= 100000)
  ADD TARGET package0.event_file (SET filename = N'/var/opt/mssql/log/rdlab_slow.xel', max_file_size = 100, max_rollover_files = 3)
  WITH (STARTUP_STATE = ON, MAX_DISPATCH_LATENCY = 2 SECONDS, EVENT_RETENTION_MODE = ALLOW_SINGLE_EVENT_LOSS);
IF NOT EXISTS (SELECT 1 FROM sys.dm_xe_sessions WHERE name = 'rdlab_slow') ALTER EVENT SESSION [rdlab_slow] ON SERVER STATE = START;
IF NOT EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = 'rdlab_all')
CREATE EVENT SESSION [rdlab_all] ON SERVER
  ADD EVENT sqlserver.sql_statement_completed (ACTION (sqlserver.username, sqlserver.database_name))
  ADD TARGET package0.event_file (SET filename = N'/var/opt/mssql/log/rdlab_all.xel', max_file_size = 100, max_rollover_files = 3)
  WITH (STARTUP_STATE = OFF, MAX_DISPATCH_LATENCY = 2 SECONDS, EVENT_RETENTION_MODE = ALLOW_MULTIPLE_EVENT_LOSS);"
Q1 "IF NOT EXISTS (SELECT 1 FROM sys.server_audits WHERE name = 'rdlab_audit')
CREATE SERVER AUDIT [rdlab_audit] TO FILE (FILEPATH = N'/var/opt/mssql/log/', MAXSIZE = 100 MB, MAX_ROLLOVER_FILES = 3) WITH (QUEUE_DELAY = 1000, ON_FAILURE = CONTINUE);
ALTER SERVER AUDIT [rdlab_audit] WITH (STATE = ON);
IF NOT EXISTS (SELECT 1 FROM sys.server_audit_specifications WHERE name = 'rdlab_server_spec')
CREATE SERVER AUDIT SPECIFICATION [rdlab_server_spec] FOR SERVER AUDIT [rdlab_audit]
  ADD (FAILED_LOGIN_GROUP), ADD (SUCCESSFUL_LOGIN_GROUP), ADD (SERVER_ROLE_MEMBER_CHANGE_GROUP), ADD (DATABASE_OBJECT_CHANGE_GROUP) WITH (STATE = ON);"
Q1 "USE lab;
IF NOT EXISTS (SELECT 1 FROM sys.database_audit_specifications WHERE name = 'rdlab_db_spec')
CREATE DATABASE AUDIT SPECIFICATION [rdlab_db_spec] FOR SERVER AUDIT [rdlab_audit]
  ADD (SCHEMA_OBJECT_CHANGE_GROUP), ADD (DELETE ON DATABASE::[lab] BY [public]) WITH (STATE = ON);"
Q1 "ALTER DATABASE [lab] SET QUERY_STORE = ON (OPERATION_MODE = READ_WRITE, QUERY_CAPTURE_MODE = ALL, MAX_STORAGE_SIZE_MB = 512, INTERVAL_LENGTH_MINUTES = 1);"
echo "logging strategy configured"
