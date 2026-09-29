# rd-databases — relational database lab

## Kubernetes report sections

- [Overview](docs/explanation-overview.md) — hero tiles (17/17 engines, 25 images, ~77 services, 5s self-heal) and the `RDLAB_PLATFORM=k3s` switch
- [Pipeline](docs/explanation-pipeline.md) — how a stack gets to the cluster: Manifests and Images pipelines
- [Time to Ready](docs/explanation-time-to-ready.md) — all 17 engines with topology, pods, and ready time grouped by tier
- [Eight failures](docs/explanation-failures.md) — eight Kubernetes-only gotchas with fixes
- [Frictions](docs/explanation-frictions.md) — registry/rootless facts, x-k8s hints, and the failover callout

A reproducible lab that stands up **20 SQL engines in Docker Compose** (single nodes, primary/replica pairs, multi-master and sharded
clusters, poolers and proxies), loads the same deterministic dataset into each, and measures six things per engine:

1. **SQL capabilities** — 30 catalog queries (window functions, recursive CTEs, LATERAL, JSON, full-text, UPSERT/MERGE, GROUPING SETS,
   pagination, percentiles, transactions) plus ~22 DDL/DML feature probes (partitioning, generated/identity columns, RETURNING,
   vector types, statement timeouts, savepoints, temporal queries, isolation levels...).
2. **Optimisation** — 11 experiments that measure a query, apply an index / rewrite / statistics refresh / batching / prepared
   statements, measure again, and keep both plans.
3. **Connection management** — connect latency, connection storms up to the server limit (with error codes), throughput vs worker
   processes, pool queueing, write-conflict and deadlock behaviour, read/write splitting through PgBouncer / ProxySQL / MaxScale / HAProxy.
4. **Replication / scaling** — topology, visibility lag, replica write rejection, catch-up under load, read scaling, failover downtime,
   and a multi-connection bulk-insert load test with per-container CPU/memory (`docker stats`).
5. **Backup & recovery** — every engine's current backup strategies run as a drill: backup -> restore elsewhere -> fingerprint of
   every table -> point-in-time / incremental probe (WAL/binlog/log replay, snapshots, flashback), with sizes and timings.
6. **Logging** — slow-query capture with a threshold, structured server logs, audit trails, statement statistics, Docker log
   rotation, log collection, and the measured throughput cost of logging every statement.

Everything is driven by one Python harness (`harness/`, `uv run rdlab ...`) and summarised in `results/SUMMARY.md`
(rendered with charts in `docs/report.html`, interpreted in `docs/findings.md`).

## Layout
```
stacks/<engine>/compose.yaml + lab.yaml   one directory per engine: compose file, init scripts, proxy configs, harness metadata
harness/rdlab/                            the harness (dialects, engine adapters, phases, CLI)
results/<engine>/latest.json              raw results (timings, plans, errors) + results/SUMMARY.md cross-engine tables
skills/                                   Agent Skills: 9 first-party (written from these results) + 40 vendored third-party
docs/                                     findings, engine matrix, sources, the Kubernetes guides, report.html + kubernetes-report.html (rendered)
k8s/                                      k3s runtime: Harbor registry, compose -> manifest generator, cluster, scenarios, backup CronJobs
stacks/<engine>/k8s/                      generated Kubernetes manifests (kustomize; do not edit, `make -C k8s gen`)
results-k3s/                              the same result files for runs against the cluster (RDLAB_RESULTS_DIR)
scripts/pull-images.sh                    pulls images through mirror.gcr.io (avoids Docker Hub's anonymous rate limit)
scripts/run-all.sh, scripts/run-drills.sh full runs / backup + logging drills for a list of stacks; scripts/vendor-skill.sh vendors a skill
scripts/build-report.py                   renders docs/report.html from results/
results/<engine>/logs/, results/logs/     collected engine log files; harness run logs (<stack>/<run_id>.log + .jsonl)
data/scale-<n>/                           generated CSV dataset (seeded; 1M events / 200k orders at scale 1)
```

## Engines (stack key -> what runs)
| key | engine | topology |
|---|---|---|
| postgres | PostgreSQL 18 (+pgvector) | primary + streaming replica + PgBouncer + HAProxy |
| citus | Citus 13 on PostgreSQL 18 | coordinator + 5 worker primaries + 5 streaming replicas (secondary nodes) + 3 PgBouncer + HAProxy, 0.8 CPU / 1.5 GiB per instance |
| mysql | MySQL 9.7 | source + GTID replica + ProxySQL 3 |
| mariadb | MariaDB 11.8 | primary + GTID replica + MaxScale 24 (auto-failover) |
| pxc | Percona XtraDB Cluster 8.4 (Galera) | 3 multi-master nodes + HAProxy |
| tidb | TiDB 8.5 | PD + 3 TiKV + 2 TiDB |
| cockroach | CockroachDB 26.2 | 3 nodes + HAProxy |
| yugabyte | YugabyteDB 2026.1 | 3 nodes (RF3) |
| mssql | SQL Server 2025 Developer | 2 replicas in a read-scale Availability Group |
| oracle | Oracle AI Database 26ai Free (23.26.3, `gvenzl/oracle-free:23-slim-faststart`) | single (Data Guard not in Free) |
| db2 | IBM Db2 12.1.5 Community | single (privileged) |
| clickhouse | ClickHouse 26.8 | 2 replicas + Keeper (ReplicatedMergeTree) |
| crate | CrateDB 6.4 | 3 nodes |
| questdb | QuestDB 10 | single |
| monetdb | MonetDB Dec2025 | single |
| firebird | Firebird 5 | single |
| h2 | H2 2.3 (PostgreSQL protocol) | single |
| sqlite / duckdb | embedded | in-process |

## Quick start
Full walkthrough (setup, per-phase explanation, running everything, reading results, adding engines, troubleshooting): **[STEP-BY-STEP.md](STEP-BY-STEP.md)**.

```bash
cd harness && uv sync --all-extras          # Python 3.12; installs all drivers
uv run rdlab gen --scale 1                  # deterministic dataset (~140 MB CSV)
../scripts/pull-images.sh                   # pull every image referenced by stacks/*/compose.yaml via mirror.gcr.io
uv run rdlab run postgres --failover        # up -> load, capabilities, bench, optimize, connections, loadtest, backup, logging, replication(+failover) -> down
uv run rdlab run citus --phases load,loadtest --keep
uv run rdlab run mysql --phases load,backup,logging   # the backup/recovery + logging drills only
uv run rdlab phase postgres bench           # one phase against an already-running stack
uv run rdlab report                         # rebuild results/SUMMARY.md
```
Each stack's `compose.yaml` header documents its backup and logging strategy (shared `/backups` volume, WAL/binlog archiving,
slow-query/JSON/audit logging, Docker `json-file` rotation); `skills/db-backup-recovery` and `skills/db-logging-observability`
hold the per-engine commands.
Firebird needs `libfbclient` on the host: `export FIREBIRD_CLIENT_LIB=/path/to/libfbclient.so.2` (see docs/findings.md).
Db2 and MonetDB both publish port 50000: run one at a time.

## Kubernetes (k3s) runtime
The same stacks also run on a **k3s cluster** (via k3d), pulling from a **local Harbor registry**. The Kubernetes
manifests are generated from the compose files, so compose stays the source of truth. All 17 containerized stacks reach
Ready on the cluster (`k8s/scenarios.sh verify-all`), and the harness phases - including the backup and logging drills -
run against it with `RDLAB_PLATFORM=k3s` (results in `results-k3s/`).
```bash
cp k8s/.env.example k8s/.env && chmod 600 k8s/.env   # set HARBOR_HOST to a LAN IP
k8s/harbor/up.sh          # local Harbor + rdlab project + push/pull robots
k8s/push-images.sh        # mirror all 24 images into Harbor
make -C k8s gen && k8s/up.sh # generate manifests + create the k3d cluster
k8s/scenarios.sh deploy postgres && k8s/scenarios.sh failover postgres
RDLAB_PLATFORM=k3s uv run --project harness rdlab run postgres --no-up --keep --phases load,bench --scale 0.3
k8s/scenarios-backup.sh schedule postgres && k8s/scenarios-backup.sh restore-drill postgres
k8s/scenarios.sh teardown postgres; k8s/down.sh; make -C k8s harbor-down   # stack -> cluster -> registry (data kept)
```
Full guide: **[docs/kubernetes.md](docs/kubernetes.md)** (`make -C k8s help` lists the targets; `docs/kubernetes-report.html` is the
rendered report of the cluster verification). Backups, restore/PITR drills,
replica promotion and log shipping on the cluster: **[docs/kubernetes-backup-logging.md](docs/kubernetes-backup-logging.md)**
(`k8s/gen-backup-addons.py` -> CronJobs in `k8s/addons/backup/<stack>/`, `k8s/scenarios-backup.sh schedule|backup-now|artifacts|restore-drill|logging-drill|logs-sidecar|promote|verify-backups`).
The distilled how-to is `skills/db-on-kubernetes`.

## Notes
- Numbers are from one 28-core host with all containers on the same machine; cross-node "scaling" is bounded by that.
- `--failover` kills the primary container; the stack must be recreated afterwards (`rdlab run` does `down -v`).
- See `skills/` for the distilled how-to knowledge and `docs/findings.md` for the surprising results.
