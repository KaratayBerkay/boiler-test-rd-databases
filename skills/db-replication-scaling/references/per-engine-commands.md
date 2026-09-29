# Replication status, lag and failover commands

## PostgreSQL
```sql
-- primary
SELECT application_name, client_addr, state, sync_state, write_lag, flush_lag, replay_lag,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes FROM pg_stat_replication;
SELECT slot_name, active, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes FROM pg_replication_slots;
-- standby
SELECT pg_is_in_recovery(), pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn(),
       EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp()) AS replay_delay_s;
SELECT status, sender_host, flushed_lsn, latest_end_lsn FROM pg_stat_wal_receiver;
-- promote
SELECT pg_promote(wait => true);   -- or: pg_ctl promote -D $PGDATA
```
Errors: write on standby -> `25006 read-only transaction`; conflict with recovery -> `40001`/`40P02` (tune `max_standby_streaming_delay`, `hot_standby_feedback`).

## MySQL 8.4 / 9
```sql
SHOW REPLICA STATUS\G      -- Replica_IO_Running, Replica_SQL_Running, Seconds_Behind_Source, Executed_Gtid_Set, Last_IO_Error
SELECT * FROM performance_schema.replication_applier_status_by_worker;
SELECT @@global.gtid_executed, @@read_only, @@super_read_only;
SHOW REPLICAS;             -- on the source
-- promote replica
STOP REPLICA; RESET REPLICA ALL; SET GLOBAL super_read_only = OFF; SET GLOBAL read_only = OFF;
-- repoint another replica to the new source
CHANGE REPLICATION SOURCE TO SOURCE_HOST='new', SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1; START REPLICA;
```
Heartbeat-based lag (more precise than Seconds_Behind_Source): write `UPDATE heartbeat SET ts = NOW(6)` on the source every second and compare on the replica (`pt-heartbeat`).

## MariaDB
```sql
SHOW REPLICA STATUS\G      -- Slave_IO_Running, Slave_SQL_Running, Seconds_Behind_Master, Gtid_IO_Pos, Using_Gtid
SELECT @@gtid_current_pos, @@gtid_slave_pos, @@read_only;
STOP REPLICA; RESET REPLICA ALL; SET GLOBAL read_only = OFF;   -- manual promote (MaxScale does it automatically)
```

## Galera (PXC / MariaDB Galera)
```sql
SHOW STATUS LIKE 'wsrep_%';   -- wsrep_cluster_size, wsrep_cluster_status=Primary, wsrep_local_state_comment=Synced,
                              -- wsrep_flow_control_paused, wsrep_local_recv_queue, wsrep_cert_deps_distance
```
Bootstrap after full outage: `mysqld --wsrep-new-cluster` (or `galera_new_cluster`) on the node with the highest `seqno` in `grastate.dat`.

## SQL Server AG
```sql
SELECT ag.name, ar.replica_server_name, rs.role_desc, drs.synchronization_state_desc, drs.log_send_queue_size, drs.redo_queue_size, drs.last_commit_time
FROM sys.dm_hadr_database_replica_states drs JOIN sys.availability_replicas ar ON ar.replica_id = drs.replica_id
JOIN sys.availability_groups ag ON ag.group_id = drs.group_id JOIN sys.dm_hadr_availability_replica_states rs ON rs.replica_id = drs.replica_id;
ALTER AVAILABILITY GROUP [ag1] FORCE_FAILOVER_ALLOW_DATA_LOSS;   -- on the secondary (CLUSTER_TYPE = NONE)
```
Write on secondary: error 3906 "Failed to update database because the database is read-only".

## CockroachDB
```sql
SELECT node_id FROM [SHOW node_id];
SHOW RANGES FROM DATABASE lab WITH DETAILS;          -- lease_holder, replicas per range
ALTER RANGE default CONFIGURE ZONE USING num_replicas = 3;
SELECT ... AS OF SYSTEM TIME follower_read_timestamp();
```
`cockroach node status --insecure --host=crdb1:26257`; `crdb_internal` tables need `SET allow_unsafe_internals = true` in 26.x.

## YugabyteDB
`bin/yb-admin -master_addresses yb1:7100,yb2:7100,yb3:7100 list_all_tablet_servers`, `SELECT * FROM yb_servers()`, `EXPLAIN (ANALYZE, DIST)`.

## TiDB
`SHOW CONFIG WHERE type='pd' AND name='replication.max-replicas'`, `SELECT * FROM INFORMATION_SCHEMA.TIKV_REGION_PEERS LIMIT 5`, `SHOW PLACEMENT`.

## ClickHouse
```sql
SELECT database, table, is_leader, is_readonly, absolute_delay, queue_size, inserts_in_queue, total_replicas, active_replicas FROM system.replicas;
SYSTEM SYNC REPLICA lab.events;   SYSTEM RESTART REPLICA lab.events;
SELECT * FROM system.clusters WHERE cluster = 'lab_cluster';
```
Keeper: `echo mntr | nc keeper 9181`.

## CrateDB
`SELECT table_name, health, missing_shards, underreplicated_shards FROM sys.health; SELECT name FROM sys.nodes;` - `ALTER TABLE t SET (number_of_replicas = '1-all')`.
