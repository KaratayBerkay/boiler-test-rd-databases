# Step by step: testing everything in this lab

Every command below is run from this directory (`rd-databases/`). `uv run --project harness rdlab …` is the harness CLI;
`cd harness && uv run rdlab …` is the same thing.

## 0. What you need
- Docker Engine with Compose v2+ (`docker compose version`), ~30 GB free disk for images and volumes, and a machine with
  spare CPU: the numbers in `results/` came from a 28-core / 125 GB host with every container on that one machine.
- Python 3.12 and `uv` (https://docs.astral.sh/uv/).
- Optional: `psql`/`mysql` clients for poking at stacks by hand (the harness itself only needs Python).
- Optional, for the Kubernetes runtime (section 9): `k3d`, `kubectl`, and ~35 GB more disk for the local Harbor registry.

## 1. Install the harness (once)
```bash
uv sync --project harness --all-extras      # installs every database driver into harness/.venv
uv run --project harness rdlab --help       # sanity check
uv run --project harness rdlab list         # the 20 stacks and their drivers
```

## 2. Generate the dataset (once)
```bash
uv run --project harness rdlab gen --scale 1   # ~30 s, writes data/scale-1/*.csv (140 MB, seeded, identical every time)
```
Scale 1 = 20k customers, 5k products, 200k orders, 488k order items, 1M events, 20k inventory rows. `--scale 0.3`
makes a smaller set (Firebird uses that by default because its driver loads slowly).

## 3. Pull the images (once)
```bash
scripts/pull-images.sh                      # every image referenced by stacks/*/compose.yaml, via mirror.gcr.io
scripts/pull-images.sh postgres:18 mysql:9  # or just some
```
The script routes Docker Hub images through Google's mirror because anonymous Docker Hub access allows only
100 manifest pulls per 6 hours. H2 is built locally (`docker compose -f stacks/h2/compose.yaml build`), the runner does
that automatically.

Extra one-time steps for two engines:
- **Firebird**: the Python driver needs the Firebird client library on the host. Install `libfbclient2` (Debian/Ubuntu)
  and export `FIREBIRD_CLIENT_LIB=/usr/lib/x86_64-linux-gnu/libfbclient.so.2` before running it.
- **Db2**: the container must run privileged (already in its compose file); first start takes 3-5 minutes.

## 4. Run one engine end to end
```bash
uv run --project harness rdlab run postgres            # up -> all phases -> down (15-25 min)
uv run --project harness rdlab run postgres --failover # also kills the primary and measures failover (stack is torn down afterwards)
```
What happens, in order:

| phase | measures | typical time |
|---|---|---|
| `load` | schema creation + bulk load through the engine's fastest driver path | 10 s - 6 min |
| `capabilities` | ~22 DDL/DML feature probes (partitioning, RETURNING, vector type, timeouts, savepoints, temporal queries, isolation levels …) | 10 s |
| `bench` | 30 catalog queries, p50/p95 over 10 runs after 2 warm-ups, estimated + actual plans saved under `results/<engine>/plans/` | 1-5 min |
| `optimize` | 11 before/after experiments: index column order, covering and partial indexes, sargable predicates, keyset vs OFFSET pagination, subquery rewrites, anti-join forms, statistics refresh, insert batching, prepared statements | 3-8 min |
| `connections` | connect latency, connection storms up to the server limit, throughput vs 1/4/16/32/64 processes (direct and through the proxy), pool queueing, lost-update and deadlock behaviour, proxy read/write split | 5 min |
| `loadtest` | multi-connection bulk insert (N processes × 1000-row batches) with exact cgroup CPU/memory per container | 3-6 min |
| `backup` | every backup strategy of the engine as a drill: backup into the shared `/backups` volume -> restore into a second database / scratch container / in place -> fingerprint of all tables -> point-in-time or incremental probe (1000-row `backup_probe` table, a DELETE after the recovery point) | 1-5 min (Oracle Data Pump ~3 min, Db2 ~2 min) |
| `logging` | effective logging settings, slow-query capture proof (tagged fast + slow statement), audit-trail proof (tagged DDL), throughput with the 100 ms threshold vs logging every statement (8 workers x 4 s, bytes per statement), Docker log driver per container, copy of the engine's log files into `results/<engine>/logs/` | 1-2 min |
| `replication` | topology, visibility lag primary→replica, replica write rejection, catch-up after 20k rows, read scaling; `--failover` adds kill-promote-measure | 2-4 min |

Useful variations:
```bash
uv run --project harness rdlab run mysql --phases load,bench --keep     # subset of phases, leave the stack running
uv run --project harness rdlab run mysql --phases load,backup,logging   # the backup/recovery + logging drills
scripts/run-drills.sh postgres mysql mariadb                             # the same for several stacks, one after the other
uv run --project harness python skills/db-backup-recovery/scripts/backup_drill.py postgres pg_dump pitr   # one strategy by hand
uv run --project harness python skills/db-logging-observability/scripts/slow_query_probe.py mysql --audit
uv run --project harness rdlab up citus                                  # just start a stack
uv run --project harness rdlab phase citus loadtest                      # one phase against a running stack
uv run --project harness rdlab run pxc --no-up --keep --phases connections --storms 50,500,2000
uv run --project harness rdlab sql postgres "SELECT version()"           # ad-hoc SQL through the harness
uv run --project harness rdlab down citus                                # stop + remove volumes
```
Stack keys: `postgres citus mysql mariadb pxc tidb cockroach yugabyte mssql oracle db2 clickhouse crate questdb monetdb firebird h2 sqlite duckdb`
(SQLite and DuckDB are embedded: no containers, `up`/`down` are no-ops).

## 5. Run everything
```bash
scripts/run-all.sh --failover postgres mysql mariadb pxc cockroach yugabyte tidb citus clickhouse crate \
                   questdb monetdb mssql oracle db2 h2 firebird sqlite duckdb
```
Sequential on purpose: two benchmarks on one host distort each other. Budget 8-10 hours for the whole list; per-stack logs
land in `results/logs/<stack>.log`, and every `rdlab run`/`rdlab phase` also writes its own log to `results/logs/<stack>/<run_id>.log`
(+ `.jsonl` with `{ts, level, stack, phase, msg}` lines for log shippers; `results/logs/<stack>/latest.log` points at the newest). Db2 and MonetDB both publish host port 50000 and SQL Server's secondary uses 11435
because 11434 was taken on the original host; adjust `stacks/<key>/compose.yaml` + `lab.yaml` if a port clashes on yours.

## 6. Read the results
```bash
uv run --project harness rdlab report                       # rebuilds results/SUMMARY.md + results/summary.json
less results/SUMMARY.md                                     # engines, latency matrix, feature probes, optimisation, connections, replication, load test, backup drills, logging
python3 -m json.tool results/postgres/latest.json | less    # raw numbers, plans, errors for one engine
uv run --project harness python scripts/build-report.py     # regenerates docs/report.html (self-contained page with charts)
```
`docs/findings.md` is the written interpretation; `docs/engine-matrix.md` lists images/versions/drivers; the last published
copy of the report is `rd-databases-report.html`. Runs against the Kubernetes runtime write the same files under `results-k3s/`
(`RDLAB_RESULTS_DIR=results-k3s uv run --project harness rdlab report` rebuilds that summary).

Reading a result: `status: ok` means the query ran and its row checksum is recorded; `unsupported` means the engine has no
syntax for the construct; `error` means the syntax was accepted but the statement failed (the error text is stored).
Backup drills: `phases.backup.strategies.<name>` holds `backup_seconds`, `backup_bytes` (on disk), `restore_seconds`, `verify.match`
(fingerprint of the restored copy equals the source), `pitr.match` (the probe rows came back), every command as `steps[]` with its rc and
output tail. Logging: `phases.logging.slow_query` (captured? after how long? sample record), `.audit`, `.overhead` (qps with the
threshold vs logging everything, bytes per statement), `.container_logging` (Docker driver/rotation), `.collected` (copied log files).

## 7. Use the skills
`skills/` holds nine first-party Agent Skills (SKILL.md + references + scripts; `skills/README.md` is the index) and 40 vendored
ones. Install into Claude Code by copying or symlinking a directory into `~/.claude/skills/` or `.claude/skills/`. The scripts
also work on their own against a running stack:
```bash
uv run --project harness python skills/sql-query-optimization/scripts/explain.py postgres "SELECT * FROM orders WHERE id = ?" 42
uv run --project harness python skills/sql-benchmark-timing/scripts/bench_query.py mysql "SELECT COUNT(*) FROM events" --iters 30
uv run --project harness python skills/db-connection-management/scripts/conn_storm.py postgres --n 400
uv run --project harness python skills/db-replication-scaling/scripts/lag_probe.py postgres --replica replica
```

## 8. Add your own query, probe, engine or stack
- **Query**: append a `Query(...)` to `harness/rdlab/queries.py` using the portable macros (`{limit(n)}`, `{json_get(col,key)}`,
  `{date_trunc_month(col)}`, `{with_recursive}`, `{cross_lateral}`, `{fts(col,words)}`, `{now}`, `{ts(...)}`); per-dialect
  overrides go in `query_overrides()` in `harness/rdlab/dialects.py`.
- **Probe**: add a `Probe(...)` to the dialect's `probes()` list.
- **Stack**: create `stacks/<key>/compose.yaml` + `lab.yaml` (copy the closest one; `targets` lists what the harness
  connects to, `replication` how to promote, `features` toggles such as `bulk_load`, `loadtest_*`, `connection_storm`,
  `backup` (shared volume, restore port, strategy list) and `logging` (log directory, slow threshold)).
  A new wire protocol needs an adapter in `harness/rdlab/engines/` and a dialect class.
- **Backup strategy**: add a `Strategy(...)` to `strategies()` in `harness/rdlab/backup/<engine>.py` (or a new module mapped in
  `backup/__init__.py`); a strategy is a function `run(ctx)` that uses `ctx.sh()` (docker exec with timing), `ctx.sql_step()`,
  `ctx.size()`, `ctx.verify(target)` and the `backup_probe` helpers, and fills a `StrategyResult`.
- **Logging probe**: subclass `LogProbe` in `harness/rdlab/dblogs/<engine>.py` (`settings`, `slow_query`, `find_slow`,
  `full_logging`, `audit_*`, `log_files`).

## 9. Run it on Kubernetes (k3s)
The same stacks run on a k3d-managed k3s cluster that pulls from a local Harbor registry; the manifests are generated from the
compose files. Full guide: `docs/kubernetes.md`; backups, restore/PITR drills, replica promotion and log shipping on the cluster:
`docs/kubernetes-backup-logging.md`; the how-to knowledge: `skills/db-on-kubernetes`; the rendered verification report:
`docs/kubernetes-report.html` (standalone, works offline).
```bash
make -C k8s env                                     # k8s/.env from the template: set HARBOR_HOST to a LAN IP
make -C k8s harbor push gen up                      # Harbor + robots -> mirror the 24 images -> manifests -> k3d cluster
k8s/scenarios.sh deploy postgres                    # kubectl apply -k stacks/postgres/k8s + wait for rollout
make -C k8s run STACK=postgres PHASES=load,bench SCALE=0.3   # = RDLAB_PLATFORM=k3s rdlab run postgres --no-up --keep ...
k8s/scenarios.sh failover postgres                  # delete the primary pod, time the self-heal on its PVC
k8s/scenarios-backup.sh schedule postgres           # backup CronJob; backup-now / artifacts / restore-drill / logging-drill / promote
k8s/scenarios.sh verify-all                         # every stack: deploy -> Ready -> teardown (17/17 on the reference host)
```
Results of cluster runs go to `results-k3s/` (never mixed with the compose numbers). `kubectl exec` has no `-u`, so drill
steps that need the database OS user run through `gosu`/`su` inside the pod; per-container cgroup CPU/memory is not available
under k3d (rows/s still is).

## 10. Clean up
```bash
uv run --project harness rdlab down <stack>         # one stack, removes its volumes
docker ps -a --filter name=rdlab- -q | xargs -r docker rm -f   # anything left over
docker volume ls -q --filter name=rdlab | xargs -r docker volume rm
rm -rf data/ results/logs/                          # dataset and logs (results/*/latest.json are the deliverables, keep them)
k8s/scenarios.sh teardown <stack>                   # Kubernetes: one stack's namespace (PVCs included)
k8s/down.sh && make -C k8s harbor-down              # the k3d cluster, then Harbor (its data under k8s/harbor/data is kept)
```

## Troubleshooting
| symptom | cause / fix |
|---|---|
| `toomanyrequests` when pulling | Docker Hub anonymous limit: use `scripts/pull-images.sh` (mirror) or `docker login` |
| stack never becomes healthy | `docker compose -f stacks/<key>/compose.yaml -p rdlab-<key> logs --tail 50`; the runner prints the last 60 log lines on failure |
| `address already in use` on a host port | another service owns it (11434 = ollama on the original host); change the `ports:` mapping and the `port:` in `lab.yaml` |
| TiDB `no enough space` / CrateDB shards `RED` / TiKV `AlmostFull` | host disk above 90 %: free space (`docker system df`, `docker builder prune`) — the stacks already lower their reservations/watermarks for this |
| Firebird `I/O error during "open"` or driver import error | connect with the full path `/var/lib/firebird/data/lab.fdb` (already set) and export `FIREBIRD_CLIENT_LIB` |
| numbers look slow / noisy | something else is running on the host; run one stack at a time and check `docker stats` |
| failover run finished but the stack is "broken" | expected: the old primary is left out of the cluster; `rdlab down <stack>` and start fresh |
| `port is already allocated` when a restore drill starts | the scratch restore container publishes `backup.restore_port` from `lab.yaml` (15439 postgres, 13309 mysql, 13319 mariadb, 13339 pxc, 18813 questdb, 15571 citus); change it if taken (15439 is also CrateDB's PostgreSQL port - do not run `crate` while drilling `postgres`) |
| a pod is `CrashLoopBackOff` / never Ready on k3s | `kubectl -n rdlab-<stack> logs <pod> --previous` and the runbook in `skills/db-on-kubernetes/references/kubernetes-lab.md` (hostname collisions, empty PVCs masking image-baked data, stale `/dev/shm`, startupProbe budget, registry auth) |
| `ImagePullBackOff` on k3s | Harbor is down (`make -C k8s harbor`) or the image was never mirrored (`k8s/push-images.sh`); `k8s/images.lock.json` lists what the manifests expect |
| backup phase says `MISMATCH` | the restored copy differs from the source fingerprint: look at `verify.mismatch` in `latest.json` (table, counts, checksum) and the `steps[]` outputs |
| a `logging` overhead number is negative or wildly different between runs | run-to-run noise on a shared host (4 s workloads); rerun with nothing else running |
