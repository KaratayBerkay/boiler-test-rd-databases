---
name: db-replication-scaling
description: Set up, verify and break database replication in Docker Compose - PostgreSQL streaming replicas (pg_basebackup -R, replication slots, pg_ctl promote), MySQL GTID replication and ProxySQL, MariaDB replication with MaxScale auto-failover, Galera/Percona XtraDB Cluster multi-master, SQL Server read-scale Availability Groups (CLUSTER_TYPE=NONE), CockroachDB/YugabyteDB/TiDB Raft clusters, ClickHouse ReplicatedMergeTree + Keeper, CrateDB shards; how to measure visibility lag, prove replicas reject writes, measure catch-up under load, read scaling, and failover downtime. Use when asked for "master/replica", "read replica", "HA", "failover", "replication lag" or "scale reads".
---

# Replication and scaling

## The test plan (implemented in `harness/rdlab/phases/replication.py`)
1. **Topology check**: each node reports its role (`pg_is_in_recovery()`, `@@read_only`, `SHOW REPLICA STATUS`, `sys.dm_hadr_*`, `SHOW node_id`, `system.replicas`).
2. **Visibility lag**: insert a row with a client timestamp on the primary, poll the replica until it appears; 30 samples -> p50/p95/max.
3. **Write rejection**: `INSERT` on the replica must fail (PostgreSQL `25006`, MySQL/MariaDB `1290`, SQL Server `3906`); multi-master systems accept it (record that).
4. **Lag under load**: 20k rows in 500-row batches on the primary while sampling replication status; measure catch-up time after the last commit.
5. **Read scaling**: 32 worker processes against primary only vs primary + replicas (on one host the gain is bounded by CPU, e.g. 1.02x; on separate hosts expect ~linear).
6. **Failover** (destructive, `--failover`): `docker kill` the primary, promote (`pg_ctl promote`; `STOP REPLICA; RESET REPLICA ALL; SET GLOBAL read_only=OFF`; `ALTER AVAILABILITY GROUP ... FORCE_FAILOVER_ALLOW_DATA_LOSS`; nothing for Raft/Galera/MaxScale-managed stacks), then poll writes on the new primary and report downtime.

## Measured (Sept 2026, same host)
| stack | mechanism | visibility lag p50 | replica rejects writes | failover downtime (first successful write) |
|---|---|---|---|---|
| PostgreSQL 18 | physical streaming, async, slot | 0.8 ms | yes (25006) | 512 ms (`pg_ctl promote`) |
| MySQL 9.7 | GTID async, row binlog | 2.6 ms | yes (1290) | 365 ms (manual promote via SQL) |
| MariaDB 11.8 + MaxScale | GTID async + mariadbmon auto-failover | 2.6 ms | yes (1290) | 2.5-3.0 s (automatic) |
| Percona XtraDB Cluster 8.4 | Galera multi-master | 0.7 ms | no | 6.3 s (node eviction) |
| SQL Server 2025 AG | sync commit, readable secondary | 0.76-0.94 s (redo lag) | yes (3906) | 1.5 s (forced failover) |
| CockroachDB 26.2 | Raft, RF=3 | 1.5 ms | no (multi-active) | 494 ms (no promotion) |
| TiDB 8.5 | Raft (TiKV), stateless SQL | 1.7 ms | no | 634 ms (use the other SQL node) |
| YugabyteDB 2026.1 | Raft, RF=3 | 1.7 ms | no | not recovered within 120 s in this lab (open issue) |
| ClickHouse 26.8 | ReplicatedMergeTree, Keeper | 29 ms | no | 1.55 s |
| Citus 13 worker + streaming replica | secondary node, `citus_update_node` repoint | 1.8 ms | yes (25006) | 1.59 s |

Citus worker failover recipe (verified): `pg_ctl promote` on the replica; on the coordinator `SET citus.enable_metadata_sync = off; SELECT citus_update_node(<secondary nodeid>, 'r1-stale', 5432); SELECT citus_update_node(<dead primary nodeid>, 'r1', 5432); SET citus.enable_metadata_sync = on; SELECT start_metadata_sync_to_all_nodes();` then `citus_remove_node('r1-stale', 5432)` once synced. With metadata sync on, the node functions try to contact the dead worker and fail.

## Setup recipes (all in `stacks/<name>/`, verified)
### PostgreSQL streaming replica with the official image (no Bitnami)
- Primary: `postgres -c wal_level=replica -c max_wal_senders=10 -c max_replication_slots=10 -c hot_standby=on`; init script creates `replicator` (`REPLICATION LOGIN`) and appends `host replication replicator all scram-sha-256` to `pg_hba.conf`.
- Replica command (runs as root only to fix ownership): `pg_basebackup -h primary -U replicator -D $PGDATA -Fp -Xs -R -C -S replica1` then `exec gosu postgres postgres -c hot_standby=on -c hot_standby_feedback=on`. `-R` writes `standby.signal` + `primary_conninfo`, `-C -S` creates the slot so WAL is retained.
- PostgreSQL 18 image: `PGDATA=/var/lib/postgresql/18/docker`, volume mount `/var/lib/postgresql` (changed from 17).
- Synchronous: `synchronous_standby_names = 'ANY 1 (walreceiver)'` + `synchronous_commit = on|remote_apply`; monitor `pg_stat_replication` (`write_lag`, `flush_lag`, `replay_lag`, `sync_state`) and on the standby `pg_stat_wal_receiver` (`flushed_lsn`, renamed from `received_lsn` in 13) and `pg_last_xact_replay_timestamp()`.
- Promote: `pg_ctl promote -D $PGDATA` or `SELECT pg_promote()`; the old primary must be re-cloned (`pg_rewind` or fresh `pg_basebackup`) - the lab recreates the stack.
- Logical replication for selective tables: `CREATE PUBLICATION`/`CREATE SUBSCRIPTION`, monitor `pg_stat_subscription`.

### MySQL 9 GTID replication (official image, init container)
- Both: `--gtid-mode=ON --enforce-gtid-consistency=ON`, distinct `--server-id`; replica additionally `--replica-parallel-workers=4 --replica-preserve-commit-order=ON`.
- Init container: create users on the source (binlogged -> arrive on the replica), then on the replica `CHANGE REPLICATION SOURCE TO SOURCE_HOST='source', SOURCE_USER='repl', SOURCE_PASSWORD='...', SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1; START REPLICA; SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;` (`GET_SOURCE_PUBLIC_KEY` because `caching_sha2_password` without TLS).
- Do not put `--read-only` in the replica's command line: the entrypoint's own init runs under it. `SET PERSIST` from the init container survives restarts.
- Health: `SHOW REPLICA STATUS` (`Replica_IO_Running`, `Replica_SQL_Running`, `Seconds_Behind_Source`, `Executed_Gtid_Set`), `performance_schema.replication_applier_status_by_worker`. Removed `innodb_log_file_size` in 8.4 -> `innodb_redo_log_capacity`. MySQL 9 removed `mysql_native_password`.
- Semi-sync (8.4 names): `rpl_semi_sync_source`/`rpl_semi_sync_replica` plugins; Group Replication for automatic failover (single-primary) + MySQL Router.

### MariaDB 11 replication + MaxScale
- Official image does the `CHANGE MASTER TO` from env (`MARIADB_MASTER_HOST`, `MARIADB_REPLICATION_USER/PASSWORD` on the replica; the same `MARIADB_REPLICATION_USER/PASSWORD` on the primary creates the user) but does **not** start replication: add `/docker-entrypoint-initdb.d/01-start-replica.sql` with `CHANGE MASTER TO MASTER_USE_GTID = slave_pos; START REPLICA;`.
- Replica health: `healthcheck.sh --connect --replication_io --replication_sql --replication` (flags must precede `--replication`).
- MaxScale mariadbmon `auto_failover=true auto_rejoin=true` promotes and re-points; `enforce_read_only_slaves=true`.

### Galera (Percona XtraDB Cluster 8.4)
- `CLUSTER_NAME`, first node bootstraps, others `CLUSTER_JOIN=pxc1`; PXC 8 encrypts cluster traffic by default -> either mount certs or `pxc_encrypt_cluster_traffic=OFF` in `/etc/percona-xtradb-cluster.conf.d/`. Status: `SHOW STATUS LIKE 'wsrep_cluster_size'`, `wsrep_local_state_comment = Synced`, `wsrep_flow_control_paused`.

### SQL Server 2025 read-scale AG (Linux, no Pacemaker)
- `MSSQL_ENABLE_HADR=1`, container `hostname` must equal the replica name; certificate-authenticated `Hadr_endpoint` on 5022 on both; `CREATE AVAILABILITY GROUP ag1 WITH (CLUSTER_TYPE = NONE) FOR REPLICA ON ... SEEDING_MODE = AUTOMATIC, SECONDARY_ROLE (ALLOW_CONNECTIONS = ALL)`; secondary `ALTER AVAILABILITY GROUP ag1 JOIN WITH (CLUSTER_TYPE = NONE); ... GRANT CREATE ANY DATABASE`; database must be FULL recovery with one full backup before `ADD DATABASE`.
- Failover is manual: `ALTER AVAILABILITY GROUP ag1 FORCE_FAILOVER_ALLOW_DATA_LOSS` on the secondary (no data loss when SYNCHRONIZED). Lag: `sys.dm_hadr_database_replica_states` (`log_send_queue_size`, `redo_queue_size`, `synchronization_state_desc`).

### Distributed SQL
- CockroachDB: `cockroach start --insecure --join=crdb1,crdb2,crdb3 --advertise-addr=<name>`, one-shot `cockroach init`; RF via `ALTER RANGE default CONFIGURE ZONE USING num_replicas = 3`; `SHOW RANGES FROM DATABASE lab WITH DETAILS`; follower reads `AS OF SYSTEM TIME follower_read_timestamp()`. HAProxy with `option httpchk GET /health?ready=1`.
- YugabyteDB: `yugabyted start --advertise_address=ybN --join=yb1 --cloud_location=... --fault_tolerance=zone`; RF becomes 3 when the third node joins; `yb-admin list_all_tablet_servers`; `SELECT * FROM yb_servers()`.
- TiDB: PD + 3 TiKV (`replication.max-replicas=3`) + N stateless TiDB servers; `SHOW PLACEMENT`, `INFORMATION_SCHEMA.TIKV_REGION_PEERS`.
- ClickHouse: `ReplicatedMergeTree('/clickhouse/tables/{shard}/{database}/{table}', '{replica}')` with `<macros>`, `<remote_servers>`, `<zookeeper>` (Keeper); `ON CLUSTER` DDL; `system.replicas.absolute_delay`; inserts on any replica; `SYSTEM SYNC REPLICA t`.
- CrateDB: `number_of_replicas`, `sys.health`, `sys.shards`; reads are eventually consistent (`REFRESH TABLE`).

## Files
- `references/per-engine-commands.md` - status queries, lag queries and promote commands per engine.
- `scripts/lag_probe.py` - measure visibility lag between any two targets of a running stack.
