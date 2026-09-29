# Sources consulted (Sept 2026)

Image documentation (Docker Hub / GitHub READMEs, downloaded during research; excerpts summarised in `docs/engine-matrix.md`):
- https://hub.docker.com/_/postgres (PGDATA change in 18), https://hub.docker.com/r/pgvector/pgvector, https://hub.docker.com/r/edoburu/pgbouncer, https://github.com/edoburu/docker-pgbouncer (entrypoint env vars)
- https://hub.docker.com/_/mysql, https://dev.mysql.com/doc/refman/9.0/en/replication-gtids.html, https://github.com/sysown/proxysql (3.0 README), https://proxysql.com/documentation/
- https://hub.docker.com/_/mariadb, https://github.com/MariaDB/mariadb-docker (docker-entrypoint.sh replication env vars), https://mariadb.com/kb/en/mariadb-maxscale-2402-readwritesplit/
- https://github.com/percona/percona-docker (percona-xtradb-cluster-8.4 entrypoint + node.cnf), https://docs.percona.com/percona-xtradb-cluster/8.4/docker.html
- https://github.com/pingcap/tidb-docker-compose, https://docs.pingcap.com/tidb/stable/mysql-compatibility
- https://www.cockroachlabs.com/docs/stable/orchestrate-a-local-cluster-with-docker-compose, https://hub.docker.com/r/cockroachdb/cockroach
- https://docs.yugabyte.com/stable/reference/configuration/yugabyted/, https://hub.docker.com/r/yugabytedb/yugabyte
- https://learn.microsoft.com/en-us/sql/linux/sql-server-linux-availability-group-configure-rs, https://mcr.microsoft.com/v2/mssql/server/tags/list
- https://github.com/gvenzl/oci-oracle-free (env vars, healthcheck.sh), https://hub.docker.com/r/gvenzl/oracle-free
- https://www.ibm.com/docs/en/db2/12.1?topic=deployments-db2-community-edition-docker, https://github.com/IBM/db2-community-docker
- https://hub.docker.com/_/clickhouse, https://github.com/ClickHouse/examples (docker-compose-recipes cluster_1S_2R), https://clickhouse.com/docs/en/architecture/replication
- https://hub.docker.com/_/crate, https://cratedb.com/docs/crate/reference/en/latest/config/cluster.html
- https://hub.docker.com/r/questdb/questdb, https://questdb.io/docs/reference/sql/overview/
- https://hub.docker.com/r/monetdb/monetdb, https://www.monetdb.org/documentation/user-guide/
- https://github.com/FirebirdSQL/firebird-docker, https://hub.docker.com/r/firebirdsql/firebird
- https://h2database.com/html/advanced.html (PostgreSQL ODBC/pg server mode), https://repo1.maven.org/maven2/com/h2database/h2/
- https://docs.citusdata.com/en/stable/admin_guide/cluster_management.html (secondary nodes, citus_update_node), https://hub.docker.com/r/citusdata/citus
- https://hub.docker.com/_/haproxy (pgsql-check / mysql-check / httpchk)

Skill sources (see `skills/README.md` for licenses): github.com/cockroachlabs/cockroachdb-skills, github.com/duckdb/duckdb-skills, github.com/ClickHouse/agent-skills, github.com/neondatabase/agent-skills, github.com/supabase/agent-skills, github.com/pingcap/agent-rules, github.com/chaterm/terminal-skills, github.com/fisker086/AIOps, github.com/ericrisco/rsc-harness, github.com/curiositech/port-daddy, github.com/StarRocks/starrocks-debug-skills; surveyed but not vendored: github.com/majiayu000/claude-skill-registry, github.com/anubhavg-icpl/vibe, github.com/alshawai/PBTune, github.com/hung-phan/system-skills, github.com/davidcastagnetoa/skills, github.com/anthropics/skills, github.com/VoltAgent/awesome-agent-skills, github.com/travisvn/awesome-claude-skills.

Benchmark tools referenced: pgbench (PostgreSQL contrib), sysbench (github.com/akopytov/sysbench), hyperfine (github.com/sharkdp/hyperfine), HammerDB (github.com/TPC-Council/HammerDB), BenchBase (github.com/cmu-db/benchbase), go-tpc (github.com/pingcap/go-tpc), clickhouse-benchmark, DuckDB tpch/tpcds extensions.

## Backup and logging (2026-09-14)
Skills vendored with `scripts/vendor-skill.sh` (licenses in each `VENDORED.txt`): github.com/Impertio-Studio/PostgreSQL-Claude-Skill-Package
(postgres-impl-backup-restore, MIT), github.com/Impertio-Studio/MariaDB-Claude-Skill-Package (mariadb-impl-backup-restore,
mariadb-errors-slow-queries, MIT), github.com/arjunprabhulal/devops-skills (backup-and-restore, database-operations, log-management,
disaster-recovery, MIT), github.com/cockroachlabs/cockroachdb-skills (configuring-audit-logging, configuring-log-export,
monitoring-background-jobs, Apache-2.0), github.com/chaterm/terminal-skills (backup-strategy, disaster-recovery, log-analysis, Apache-2.0).
Surveyed and not vendored: mihailtd/skills and mhbzhy-lost/claude-personal-config (no license), ArieGoldkin/ai-agent-hub and
terrylica/cc-skills (generic observability / Python logging, not database specific), microsoft/skills (Azure SDK only).

Product documentation consulted while implementing the drills:
- https://www.postgresql.org/docs/18/continuous-archiving.html, https://www.postgresql.org/docs/18/app-pgbasebackup.html (`--incremental`),
  https://www.postgresql.org/docs/18/app-pgcombinebackup.html, https://www.postgresql.org/docs/18/runtime-config-logging.html (PG18 `log_connections` list)
- https://dev.mysql.com/doc/mysql-shell/9.0/en/mysql-shell-utilities-dump-instance-schema.html, https://dev.mysql.com/doc/refman/9.0/en/clone-plugin.html,
  https://dev.mysql.com/doc/refman/9.0/en/start-replica.html (`UNTIL SQL_BEFORE_GTIDS`), https://dev.mysql.com/doc/refman/9.0/en/error-log-json.html
- https://mariadb.com/kb/en/mariabackup-overview/, https://mariadb.com/kb/en/incremental-backup-and-restore-with-mariabackup/,
  https://mariadb.com/kb/en/flashback/, https://mariadb.com/kb/en/mariadb-audit-plugin/
- https://docs.percona.com/percona-xtrabackup/8.4/, https://docs.percona.com/percona-server/8.4/audit-log-plugin.html
- https://docs.pingcap.com/tidb/stable/sql-statement-backup, https://docs.pingcap.com/tidb/stable/sql-statement-flashback-cluster,
  https://docs.pingcap.com/tidb/stable/identify-slow-queries, https://docs.pingcap.com/tidb/stable/tidb-configuration-file#log
- https://www.cockroachlabs.com/docs/stable/backup-and-restore-overview, https://www.cockroachlabs.com/docs/stable/restore#point-in-time-restore,
  https://www.cockroachlabs.com/docs/stable/configure-logs, https://www.cockroachlabs.com/docs/stable/logging-use-cases#sql-slow-query-log
- https://docs.yugabyte.com/stable/manage/backup-restore/snapshot-ysql/, https://docs.yugabyte.com/stable/manage/backup-restore/point-in-time-recovery/,
  https://docs.yugabyte.com/stable/reference/configuration/yugabyted/ (`{}` grouping for comma lists in `--tserver_flags`)
- https://learn.microsoft.com/en-us/sql/relational-databases/backup-restore/restore-a-sql-server-database-to-a-point-in-time-full-recovery-model,
  https://learn.microsoft.com/en-us/sql/relational-databases/extended-events/, https://learn.microsoft.com/en-us/sql/relational-databases/security/auditing/sql-server-audit-database-engine
- https://docs.oracle.com/en/database/oracle/oracle-database/23/sutil/ (Data Pump), https://docs.oracle.com/en/database/oracle/oracle-database/23/adfns/flashback.html,
  https://docs.oracle.com/en/database/oracle/oracle-database/23/dbseg/administering-the-audit-trail.html
- https://www.ibm.com/docs/en/db2/12.1?topic=recovery-backup (incremental, INCLUDE LOGS, LOGTARGET), https://www.ibm.com/docs/en/db2/12.1?topic=commands-rollforward-database,
  https://www.ibm.com/docs/en/db2/12.1?topic=facility-db2audit-audit-facility-administrator-tool-command, https://www.ibm.com/docs/en/db2/12.1?topic=monitors-activities-event-monitor
- https://clickhouse.com/docs/operations/backup, https://clickhouse.com/docs/operations/system-tables/query_log, https://clickhouse.com/docs/operations/server-configuration-parameters/settings#logger
- https://cratedb.com/docs/crate/reference/en/latest/sql/statements/restore-snapshot.html (schema/table rename options), https://cratedb.com/docs/crate/reference/en/latest/admin/system-information.html#jobs-log
- https://questdb.com/docs/operations/backup/ (CHECKPOINT, `_restore`), https://questdb.com/docs/concept/query-tracing/ (`_query_trace`)
- https://www.monetdb.org/documentation-Dec2025/user-guide/server-administration/hot-snapshot/, https://www.monetdb.org/documentation-Dec2025/user-guide/sql-manual/query-log/
- https://firebirdsql.org/file/documentation/html/en/refdocs/fblangref50/firebird-50-language-reference.html (nbackup, gbak), https://firebirdsql.org/file/documentation/html/en/firebirddocs/nbackup/firebird-nbackup.html,
  https://www.firebirdsql.org/file/documentation/release_notes/html/en/3_0/rlsnotes30.html#rnfb30-trace (trace / audit configuration)
- https://h2database.com/html/commands.html#backup (BACKUP TO, SCRIPT, RUNSCRIPT), https://h2database.com/html/features.html#trace_options
- https://www.sqlite.org/lang_vacuum.html#vacuuminto, https://www.sqlite.org/backup.html, https://litestream.io/ (continuous WAL replication for SQLite)
- https://duckdb.org/docs/stable/sql/statements/export.html, https://duckdb.org/docs/stable/sql/statements/copy#copy-from-database--to,
  https://duckdb.org/docs/stable/configuration/logging (`enable_logging`, `duckdb_logs()`)
- https://docs.citusdata.com/en/stable/admin_guide/cluster_management.html#backup-and-restore (`citus_create_restore_point`)
- https://docs.docker.com/engine/logging/drivers/json-file/ (`max-size`, `max-file`, `compress`, `tag`)

## Kubernetes runtime (2026-09-14; `docs/kubernetes.md`, `docs/kubernetes-backup-logging.md`)
- https://k3d.io/stable/usage/configfile/ (config file schema, `nodeFilters`, `server:0:direct`), https://k3d.io/stable/usage/registries/ (`--registry-config`, mirrors + auth)
- https://docs.k3s.io/installation/private-registry (`registries.yaml`: mirrors, `configs.<host>.auth`, `tls.ca_file`), https://docs.k3s.io/cli/server (`--disable`, `--kubelet-arg`, `--kube-apiserver-arg`)
- https://goharbor.io/docs/latest/install-config/ (offline installer, `harbor.yml`, HTTPS), https://goharbor.io/docs/latest/administration/robot-accounts/ (project robots, one-time secrets), https://github.com/google/go-containerregistry/blob/main/cmd/crane/README.md (`crane copy`)
- https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/ (schedule, `successfulJobsHistoryLimit`), https://kubernetes.io/docs/concepts/workloads/pods/init-containers/, https://kubernetes.io/docs/concepts/services-networking/service/#type-nodeport, https://kubernetes.io/docs/concepts/services-networking/service/#headless-services (`publishNotReadyAddresses`)
- https://kubernetes.io/docs/concepts/cluster-administration/logging/ (kubelet log rotation `containerLogMaxSize`/`containerLogMaxFiles`, sidecar patterns), https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/ (startup probes restart the container)
- https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/ (`kubectl apply -k`), https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes (RWO vs RWX for shared backup volumes)
