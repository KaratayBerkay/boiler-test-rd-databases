---
name: docker-compose-databases
description: Author Docker Compose stacks for SQL databases that come up reliably - verified images and tags (Sept 2026) for 20+ engines, correct bootstrap env vars, healthchecks, one-shot init containers for cluster wiring (GTID replication, Availability Groups, cockroach init, Keeper), volume paths (PostgreSQL 18 moved PGDATA), resource settings, and how to pull images without hitting Docker Hub's anonymous rate limit (mirror.gcr.io). Use when writing or debugging compose files for PostgreSQL, MySQL, MariaDB, SQL Server, Oracle, Db2, ClickHouse, CockroachDB, YugabyteDB, TiDB, Galera, Firebird, MonetDB, CrateDB, QuestDB, H2, poolers and proxies.
---

# Docker Compose for databases

## Patterns that make stacks reliable
1. **Every service gets a real healthcheck** and dependents use `depends_on: {svc: {condition: service_healthy}}`; one-shot setup containers use `restart: "no"` and dependents wait with `condition: service_completed_successfully`. The lab harness (`rdlab up`) waits for healthy/exited-0 and prints logs on failure.
2. **Wire clusters from a one-shot init container**, not from entrypoint hacks: MySQL replication (`CHANGE REPLICATION SOURCE TO`), SQL Server AG (certificates, endpoints, `CREATE AVAILABILITY GROUP`), CockroachDB (`cockroach init`), YugabyteDB (`CREATE DATABASE`), TiDB (users). Idempotent SQL (`IF NOT EXISTS`) so re-running is safe.
3. **Pin the image tag** you tested and record the resolved version in `lab.yaml`/notes (e.g. `postgres:18` = 18.6, `mysql:9` = 9.7.2, `mariadb:11` = 11.8.9, `clickhouse/clickhouse-server:latest` = 26.8, `cockroachdb/cockroach:latest` = v26.2.6).
4. **Named volumes per node**, `down -v` between benchmark runs so every run starts from the same state.
5. **Set memory knobs explicitly** (`shared_buffers`, `innodb_buffer_pool_size`, `CRATE_HEAP_SIZE`, `MSSQL_MEMORY_LIMIT_MB`, `--cache` for CockroachDB) - defaults are tiny or unbounded.
6. **Avoid Docker Hub's anonymous limit** (100 manifest pulls / 6 h / IP; `toomanyrequests` on `docker manifest inspect` too): pull from `mirror.gcr.io/library/<image>` or `mirror.gcr.io/<org>/<image>` and retag (`scripts/pull-images.sh`). Registries that are not on Hub: `mcr.microsoft.com/mssql/server`, `icr.io/db2_community/db2`, `container-registry.oracle.com`, `ghcr.io`.
7. **Escape `$` in compose command scripts** as `$$` (compose interpolates `$VAR`; `$(...)` is fine).
8. **Backups and logs are part of the stack, not an afterthought**: a shared `backups` volume created by a one-shot init job with mode 1777 (any container uid can write; scratch restore containers mount the same volume), WAL/binlog/log archiving switched on from day one, the engine's slow-query/JSON/audit logs on their own volume, and a `x-logging` anchor (`json-file`, `max-size: 50m`, `max-file: 3`, `compress: true`, `tag: {{.Name}}`) on every service. The drills that verify them: `skills/db-backup-recovery`, `skills/db-logging-observability`.
9. **Keep the compose file the single source of truth for Kubernetes too**: `x-k8s:` hints (`port`, `startup_seconds`, `seed_from_image`, `shm_reset`) are ignored by compose and consumed by the manifest generator (`skills/db-on-kubernetes`); declare `hostname:` only where the software needs a stable identity.

## Verified images and bootstrap (Sept 2026)
| engine | image | key env / command | port | healthcheck |
|---|---|---|---|---|
| PostgreSQL 18 (+pgvector) | `pgvector/pgvector:pg18` (= `postgres:18` + extension) | `POSTGRES_USER/PASSWORD/DB`; `PGDATA=/var/lib/postgresql/18/docker`, volume `/var/lib/postgresql` | 5432 | `pg_isready -U lab -d lab` |
| PgBouncer | `edoburu/pgbouncer` | `DB_HOST/DB_USER/DB_PASSWORD/DB_NAME`, `AUTH_TYPE=scram-sha-256`, `POOL_MODE`, `MAX_PREPARED_STATEMENTS` | 5432 | `pg_isready -h 127.0.0.1` |
| MySQL 9 | `mysql:9` | `MYSQL_ROOT_PASSWORD`; `--gtid-mode=ON --enforce-gtid-consistency=ON`; `--innodb-redo-log-capacity` (not `innodb-log-file-size`) | 3306 | `mysqladmin ping -uroot -p...` |
| ProxySQL 3 | `proxysql/proxysql` | `/etc/proxysql.cnf`; `ulimits nofile 65536` | 6033 / 6032 admin | `mysql -h127.0.0.1 -P6032 -uadmin -padmin -e 'select 1'` |
| MariaDB 11 | `mariadb:11` | `MARIADB_ROOT_PASSWORD/DATABASE/USER/PASSWORD`, `MARIADB_REPLICATION_USER/PASSWORD`, replica: `MARIADB_MASTER_HOST` + init SQL `START REPLICA` | 3306 | `healthcheck.sh --connect --innodb_initialized` |
| MaxScale | `mariadb/maxscale` | `/etc/maxscale.cnf.d/*.cnf` | 4006 / 8989 | `maxctrl list servers` |
| Percona XtraDB Cluster 8.4 | `percona/percona-xtradb-cluster:8.4` | `MYSQL_ROOT_PASSWORD`, `CLUSTER_NAME`, `CLUSTER_JOIN=pxc1`, `XTRABACKUP_PASSWORD`; conf.d `pxc_encrypt_cluster_traffic=OFF` | 3306 | `SHOW STATUS LIKE 'wsrep_local_state_comment'` = Synced |
| TiDB 8.5 | `pingcap/pd:v8.5.8`, `pingcap/tikv:v8.5.8`, `pingcap/tidb:v8.5.8` (no `latest` for tikv) | pd `--initial-cluster`, tikv `--pd=pd:2379`, tidb `--store=tikv --path=pd:2379` | 4000 / 10080 status | `wget -qO- http://127.0.0.1:10080/status` |
| SQL Server 2025 | `mcr.microsoft.com/mssql/server:2025-latest` | `ACCEPT_EULA=Y`, `MSSQL_PID=Developer`, `MSSQL_SA_PASSWORD` (complexity rules), `MSSQL_ENABLE_HADR=1`, `hostname:` = replica name | 1433 (5022 AG) | `/opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P ... -Q 'SELECT 1'` |
| Oracle 26ai Free (23.26) | `gvenzl/oracle-free:23-slim-faststart` (6.5 GB; the `23` tag already ships the rebranded "Oracle AI Database 26ai Free" 23.26.3 build) | `ORACLE_PASSWORD`, `APP_USER`, `APP_USER_PASSWORD`; service `FREEPDB1` | 1521 | `healthcheck.sh` (in image) |
| Db2 12.1 CE | `icr.io/db2_community/db2` | `LICENSE=accept`, `DB2INST1_PASSWORD`, `DBNAME`; `privileged: true`; 3-5 min start | 50000 | `su - db2inst1 -c "db2 connect to lab"` |
| ClickHouse | `clickhouse/clickhouse-server`, `clickhouse/clickhouse-keeper` | `CLICKHOUSE_USER/PASSWORD/DB`, `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`; config.d XML for `<remote_servers>`, `<zookeeper>`, `<macros>`; `ulimits nofile 262144` | 8123 / 9000 / keeper 9181 | `wget -qO- http://127.0.0.1:8123/ping` |
| CockroachDB | `cockroachdb/cockroach:latest` | `start --insecure --join=... --advertise-addr=<hostname>`; one-shot `cockroach init`; `crdb_internal` restricted in 26.x | 26257 / 8080 | `curl -fs http://127.0.0.1:8080/health?ready=1` |
| YugabyteDB | `yugabytedb/yugabyte:latest` (2026.1) | `bin/yugabyted start --background=false --advertise_address=ybN [--join=yb1]` | 5433 / 7000 / 15433 | `postgres/bin/pg_isready -h 127.0.0.1 -p 5433` |
| CrateDB | `crate:latest` | `crate -Ccluster.name=lab -Cnode.name=... -Cdiscovery.seed_hosts=... -Ccluster.initial_master_nodes=...`, `CRATE_HEAP_SIZE=2g`, `memlock -1` | 4200 / 5432 | `curl -fs http://127.0.0.1:4200/` |
| QuestDB | `questdb/questdb` | `QDB_PG_USER/PASSWORD`, `QDB_PG_NET_CONNECTION_LIMIT`, `QDB_HTTP_QUERY_CACHE_ENABLED=false` | 8812 PG / 9000 HTTP / 9003 metrics | `curl -fs http://127.0.0.1:9003/status` |
| MonetDB | `monetdb/monetdb` | `MDB_DB_ADMIN_PASS`, `MDB_CREATE_DBS=lab`; mclient needs a `.monetdb` file for the password | 50000 | `DOTMONETDBFILE=... mclient -d lab -s 'SELECT 1'` |
| Firebird 5 | `firebirdsql/firebird:5` | `FIREBIRD_ROOT_PASSWORD`, `FIREBIRD_USER/PASSWORD`, `FIREBIRD_DATABASE=lab.fdb` -> `/var/lib/firebird/data/lab.fdb` (connect with the full path) | 3050 | `isql -u lab -p ... localhost:/var/lib/firebird/data/lab.fdb` |
| H2 2.3 | build from `eclipse-temurin:21-jre` + h2 jar (`oscarfonts/h2` is 1.4.x) | `org.h2.tools.Server -tcp -pg -web -ifNotExists -baseDir /data` | 9092 / 5435 (PG protocol) / 8082 | TCP connect to 5435 |
| HAProxy | `haproxy:lts` | `option pgsql-check user lab` / `option mysql-check user ...` / `option httpchk` | any | `haproxy -c -f cfg` |

Not included and why: Bitnami images (versioned tags moved to `bitnamilegacy`, `latest`-only free), Vitess (needs vtctld/etcd/vtgate topology, days of work), OceanBase CE (needs 8+ GB per node, slow start), SAP HANA Express (registration + 16 GB), Actian/Altibase/InterSystems (no public tags via mirror), Vertica CE / Exasol (privileged + licence key), SingleStore (licence key), Materialize/RisingWave (streaming SQL, not RDBMS), Spanner/Neon emulators (not the real engine).

## Files
- `references/stack-index.md` - what each lab stack contains, its ports and how to start it.
