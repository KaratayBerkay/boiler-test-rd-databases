# Skills

Two groups of Agent Skills (the open `SKILL.md` format: YAML frontmatter with `name` + `description`, optional `references/` and `scripts/`).
Install into Claude Code by copying or symlinking a skill directory into `~/.claude/skills/` (user-wide) or `.claude/skills/` (project).

## First-party skills (written from this lab's verified results)
| skill | use it for | extras |
|---|---|---|
| `sql-query-writing` | dialect-aware SQL for 17 dialects (the 20 lab engines), portable macros, what is missing where | `references/dialect-cheatsheet.md`, `references/portable-alternatives.md` |
| `sql-query-optimization` | reading plans, index strategy, rewrites with measured before/after numbers | `references/explain-per-engine.md`, `references/red-flags.md`, `scripts/explain.py` |
| `sql-benchmark-timing` | measuring latency/throughput honestly; tools (pgbench, sysbench, hyperfine, HammerDB, BenchBase, go-tpc) | `references/methodology.md`, `scripts/bench_query.py` |
| `db-connection-management` | limits, connect cost, poolers/proxies, storms, error codes, retry policy | `references/poolers.md`, `scripts/conn_storm.py` |
| `db-replication-scaling` | streaming/GTID/AG/Galera/Raft/ReplicatedMergeTree setup, lag, failover measurement, Citus sharding | `references/per-engine-commands.md`, `scripts/lag_probe.py` |
| `docker-compose-databases` | verified images, env vars, healthchecks, init containers, resource limits, registry mirror | `references/stack-index.md` |
| `db-backup-recovery` | backup / restore / PITR drills for all 20 engines: logical, physical, incremental, WAL/binlog/log replay, flashback, snapshots; every command verified by an automated restore + fingerprint | `references/per-engine-commands.md`, `scripts/backup_drill.py` |
| `db-logging-observability` | slow-query logs and thresholds, JSON server logs, audit trails, statement statistics, Docker log rotation, the measured cost of logging every statement | `references/per-engine-logging.md`, `scripts/slow_query_probe.py` |
| `db-on-kubernetes` | running the stacks on k3s/k3d with a local Harbor registry: generating manifests from compose, mirroring images, and the k8s-only failures (pod hostname collisions, empty PVCs masking image-baked data, /dev/shm across restarts, startup probes killing slow boots, k3d registry auth) | `references/kubernetes-lab.md` |

## Vendored third-party skills (`vendor/`)
Each directory carries a `VENDORED.txt` with source URL and license. Only permissive licenses (MIT/Apache-2.0; the two port-daddy skills are
FSL-1.1-MIT at repo level with Apache-2.0 declared in the skill).

| directory | category | quality | why |
|---|---|---|---|
| `curiositech__port-daddy__postgres-explain-analyzer` | optimization (PostgreSQL) | 5 | decision diagram, node-by-node diagnosis, quality gates, audit script |
| `curiositech__port-daddy__postgres-connection-pooling` | connections (PostgreSQL) | 5 | PgBouncer/pgcat sizing, transaction-mode caveats, checklists |
| `cockroachlabs__cockroachdb-skills__benchmarking-transaction-patterns` | benchmarking (CockroachDB) | 5 | vendor-authored, contention/retry patterns with measurements |
| `cockroachlabs__cockroachdb-skills__designing-application-transactions` | connections/transactions | 5 | retry loops, idempotency, isolation |
| `cockroachlabs__cockroachdb-skills__cockroachdb-sql` | query writing | 4 | dialect differences from PostgreSQL |
| `cockroachlabs__cockroachdb-skills__profiling-statement-fingerprints` | optimization | 4 | statement statistics workflow |
| `cockroachlabs__cockroachdb-skills__auditing-table-statistics` | optimization | 4 | statistics staleness |
| `cockroachlabs__cockroachdb-skills__setting-up-local-cluster` | compose/HA | 4 | local multi-node clusters |
| `ClickHouse__agent-skills__clickhouse-best-practices` | optimization (ClickHouse) | 5 | vendor rules: sorting keys, inserts, joins, materialized views |
| `ClickHouse__agent-skills__infra-clickhouse` / `infra-postgres` | operations | 3 | infra checklists |
| `neondatabase__agent-skills__neon-postgres` | query writing/optimization (PostgreSQL) | 4 | PostgreSQL best-practice references |
| `supabase__agent-skills__supabase-postgres-best-practices` | optimization (PostgreSQL) | 4 | indexing, RLS-aware query patterns, references |
| `pingcap__agent-rules__tidb-query-tuning` | optimization (TiDB) | 5 | vendor query-tuning playbook with EXPLAIN ANALYZE guidance |
| `pingcap__agent-rules__tidb-sql` / `mysql` | query writing | 4 | dialect rules |
| `duckdb__duckdb-skills__query` / `duckdb-docs` | query writing (DuckDB) | 4 | vendor-authored |
| `ericrisco__rsc-harness__sql` / `mysql` / `duckdb` / `clickhouse-analytics` | query writing | 4 | references on CTEs, windows, joins, portability; eval scripts |
| `chaterm__terminal-skills__postgresql` / `mysql` / `benchmarking` | operations/benchmarking | 3 | command cheat-sheets (Chinese/English) |
| `fisker086__AIOps__mysql_explain` | optimization (MySQL) | 2 | short EXPLAIN checklist |
| `StarRocks__starrocks-debug-skills__query` | optimization (StarRocks) | 4 | slow-query triage for an MPP engine |
| `Impertio-Studio__PostgreSQL-Claude-Skill-Package__postgres-impl-backup-restore` | backup (PostgreSQL) | 5 | logical vs physical decision tree, pg_dump formats/--jobs, WAL archiving + PITR, v17 incremental backup (pg_combinebackup); references with option tables, examples, anti-patterns |
| `Impertio-Studio__MariaDB-Claude-Skill-Package__mariadb-impl-backup-restore` | backup (MariaDB) | 5 | mariadb-backup incremental chains, --prepare rules, binlog PITR, single-table restore, Galera donor selection, RPO/RTO matrix |
| `Impertio-Studio__MariaDB-Claude-Skill-Package__mariadb-errors-slow-queries` | logging (MariaDB) | 4 | slow query log setup and triage |
| `arjunprabhulal__devops-skills__backup-and-restore` | backup (generic) | 4 | RPO-driven schedules, rehearsed restores, immutable offsite copies, done-when criteria |
| `arjunprabhulal__devops-skills__database-operations` | operations (generic) | 3 | day-2 database operations checklist |
| `arjunprabhulal__devops-skills__log-management` | logging (generic) | 4 | structured logging, levels, sampling, retention, correlation ids, secrets out of logs |
| `arjunprabhulal__devops-skills__disaster-recovery` | recovery (generic) | 4 | DR planning, RTO/RPO, runbooks and failover drills |
| `cockroachlabs__cockroachdb-skills__configuring-audit-logging` | logging/audit (CockroachDB) | 5 | vendor-authored: admin/role-based audit settings, slow query threshold, performance impact table, rollback |
| `cockroachlabs__cockroachdb-skills__configuring-log-export` | logging (CockroachDB) | 4 | log export sinks (cloud) |
| `cockroachlabs__cockroachdb-skills__monitoring-background-jobs` | operations (CockroachDB) | 4 | BACKUP/RESTORE and other jobs: states, SHOW JOBS queries, permissions |
| `chaterm__terminal-skills__backup-strategy` | backup (generic) | 3 | 3-2-1(-1-0) strategy, verification, retention (Chinese) |
| `chaterm__terminal-skills__disaster-recovery` | recovery (generic) | 3 | DR command cheat-sheet (Chinese) |
| `chaterm__terminal-skills__log-analysis` | logging (generic) | 3 | log analysis commands (Chinese) |

Looked at and not vendored: `majiayu000/claude-skill-registry` (mirror of other people's skills, provenance unclear), `anubhavg-icpl/vibe` (CC BY-NC-SA),
`alshawai/PBTune` (GPL-3.0), `hung-phan/system-skills` and `davidcastagnetoa/skills` (no license), Bitnami/Azure/BigQuery-specific skills (out of scope).
Gaps found: no existing skill covered cross-engine benchmarking methodology, replication testing, or SQL Server/Oracle/Db2/Firebird tuning in SKILL.md form; the first-party skills above fill those.
Backup/logging survey (2026-09-14, `scripts/vendor-skill.sh <owner/repo> <path> <license>`): only PostgreSQL, MariaDB and CockroachDB had engine-specific
backup or audit-logging skills; nothing existed for SQL Server, Oracle, Db2, ClickHouse, TiDB, YugabyteDB, CrateDB, QuestDB, MonetDB, Firebird, H2, SQLite or DuckDB
backup/PITR, nor for the cost of logging - `db-backup-recovery` and `db-logging-observability` were written from the drills to cover them.
