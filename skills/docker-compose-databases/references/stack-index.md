# Lab stacks (`stacks/<key>/compose.yaml` + `lab.yaml`)

| key | services | host ports | start | notes |
|---|---|---|---|---|
| postgres | primary, replica (pg_basebackup), pgbouncer, haproxy | 15432, 15433, 16432, 15000/15001, 18404 stats | `uv run rdlab up postgres` | pgvector, pg_stat_statements, pg_prewarm |
| citus | coordinator, w1..w5 (worker primaries), r1..r5 (streaming replicas registered as secondaries), init, pgbouncer1..3, haproxy | 15531 coordinator, 15532/15533 haproxy, 15541-15545 workers, 15551-15555 replicas, 15561-15563 pgbouncers | `uv run rdlab up citus` | 0.8 CPU / 1.5 GiB per instance; 16 containers |
| mysql | source, replica (GTID), init, proxysql | 13306, 13307, 16033, 16032 admin | `uv run rdlab up mysql` | init container wires replication |
| mariadb | primary, replica, maxscale | 13316, 13317, 14006, 18989 | `uv run rdlab up mariadb` | MaxScale auto-failover |
| pxc | pxc1..3 (Galera), haproxy | 13326-13328, 13329 | `uv run rdlab up pxc` | multi-master |
| tidb | pd, tikv1..3, tidb1..2, init | 14000, 14001, 12379, 10080 | `uv run rdlab up tidb` | MySQL protocol |
| cockroach | crdb1..3, init, haproxy | 26257-26259, 26260, 18080 UI | `uv run rdlab up cockroach` | insecure mode |
| yugabyte | yb1..3, init | 15433, 15435, 15436, 17000, 15434 UI | `uv run rdlab up yugabyte` | yugabyted RF3 |
| clickhouse | keeper, ch1, ch2 | 18123/19000, 18124/19001 | `uv run rdlab up clickhouse` | ReplicatedMergeTree |
| mssql | mssql1, mssql2, init (AG) | 11433, 11434 | `uv run rdlab up mssql` | read-scale AG |
| oracle | oracle | 11521 | `uv run rdlab up oracle` | FREEPDB1, user lab |
| db2 | db2 (privileged) | 50000 | `uv run rdlab up db2` | 3-5 min start |
| firebird | firebird | 13050 | `uv run rdlab up firebird` | needs libfbclient on the host for the Python driver (`FIREBIRD_CLIENT_LIB`) |
| h2 | h2 (built image) | 19092, 15435, 18082 | `uv run rdlab up h2` | PG protocol |
| monetdb | monetdb | 50000 (conflicts with db2: run one at a time) | `uv run rdlab up monetdb` | |
| crate | crate1..3 | 14200-14202, 15439 | `uv run rdlab up crate` | |
| questdb | questdb | 18812, 19009, 19003 | `uv run rdlab up questdb` | |
| sqlite, duckdb | (embedded) | - | - | files under `data/` |

Every stack also has a `backups` volume (created 1777 by a one-shot `volumes-init`/`backup-init` job) that the backup drills and the
Kubernetes CronJobs write into, and a log volume for the engine's slow-query/JSON/audit files (`results/<engine>/logs/` after a run).

`uv run rdlab run <key> [--failover] [--phases ...]` = up + phases + down. `uv run rdlab report` rebuilds `results/SUMMARY.md`.
The same stacks on k3s: `k8s/scenarios.sh deploy <key>` (generated `stacks/<key>/k8s/`, see `docs/kubernetes.md`).
