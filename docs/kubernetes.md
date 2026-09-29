# Running the lab on k3s (Kubernetes)

The lab was built as Docker Compose stacks. This adds a **second runtime**: the same stacks, the same seeded dataset
and the same harness, running on a **k3s cluster** (via k3d), pulling every image from a **local Harbor registry**.
Compose stays the source of truth — the Kubernetes manifests are generated from `stacks/<engine>/compose.yaml`, so a
change to a compose file flows into both runtimes. A rendered summary of this page (17/17 verdicts, the architecture and the
k8s-only failures) is `kubernetes-report.html` next to it.

```
stacks/<engine>/compose.yaml ──(k8s/gen.py)──▶ stacks/<engine>/k8s/*.yaml ──(kubectl apply -k)──▶ k3d cluster
        │                                                                                            │
        └── images ──(k8s/push-images.sh)──▶ Harbor (harbor.<host>:8443/rdlab/…) ──(robot pull)─────┘
```

## Why this shape

| decision | reason |
|---|---|
| **k3d** (k3s in Docker), not the host k3s service | no root needed, disposable, reproducible from one config file; the host already runs its other labs this way |
| **1 server node, 0 agents** | every compose number was measured on one host; shared RWO volumes (backup drills, replica bootstrap) need co-located pods anyway |
| **local Harbor**, not Docker Hub directly | one private mirror of all 24 images (34 GB) → no Hub rate limits, no per-node pulls of the same 7 GB Db2 image, and it is the piece a real deployment needs anyway |
| **generate manifests from compose** | 17 containerized stacks, 78 services; hand-writing and drift-maintaining them is untenable. `gen.py` reads `docker compose config` (anchors/env resolved) so what runs on k8s is what the compose file says |
| **NodePort per published port**, published on `127.0.0.1:<nodePort>` | the harness connects to `127.0.0.1:<port>`; a fixed host→nodePort table (`k8s/ports.json`) lets `lab.yaml` targets work unchanged, and loopback keeps the self-signed registry cert trusted without touching the Docker daemon |
| **Deployment + headless Service**, `publishNotReadyAddresses: true` | cluster bootstrap traffic (a replica running `pg_basebackup` against a not-yet-ready primary, Citus `citus_add_node`) must flow before readiness, exactly like a compose network |
| **no livenessProbe** | the failover scenario stops a primary on purpose; a liveness probe would fight it. Readiness + startup probes only |

## One-time setup

```bash
cd rd-databases
cp k8s/.env.example k8s/.env && chmod 600 k8s/.env     # set HARBOR_HOST to a LAN IP both host and nodes reach
k8s/harbor/up.sh          # install + start Harbor (docker compose), make the rdlab project + push/pull robots
k8s/push-images.sh        # mirror all 24 stack images (+busybox) into Harbor; writes k8s/images.lock.json
make -C k8s gen             # generate stacks/*/k8s/ + k8s/ports.json + k8s/k3d.yaml   (docker compose config -> manifests)
k8s/up.sh                 # create the k3d cluster wired to Harbor; smoke-tests a pull with the pull-only robot
```

`k8s/.env` holds the admin password and the two robot secrets (Harbor shows a robot secret once; `setup.py` records
it). It is gitignored, mode 600. So are `k8s/harbor/{certs,data,logs}`, `k8s/.docker-robot`, `k8s/registries.yaml`.

## Deploy and drive a stack

```bash
kubectl config use-context k3d-rdlab

# raw kubectl
kubectl apply -k stacks/postgres/k8s
kubectl -n rdlab-postgres get pods,svc,pvc

# the harness, against the cluster (RDLAB_PLATFORM=k3s routes exec/logs/lifecycle to kubectl and remaps ports)
RDLAB_PLATFORM=k3s uv run --project harness rdlab run postgres --no-up --keep \
    --phases load,capabilities,bench,connections --scale 0.3
```

`RDLAB_PLATFORM=k3s` (see `harness/rdlab/platform.py`) is the whole switch:

- container ops (`exec_in`, `logs`, `read_file`, `dir_size_bytes`, stop/start, up/down) resolve the compose container
  name to a pod through the `rdlab.io/container` label and run `kubectl` instead of `docker`;
- `lab.yaml` target ports are translated to their nodePort via `k8s/ports.json`, so `engine.connect()` reaches the DB
  on `127.0.0.1:<nodePort>`.

**All 17 containerized stacks deploy and reach Ready on k3s** (`k8s/scenarios.sh verify-all`, deploy -> wait -> teardown,
one at a time). Verdicts from the last run (time to Ready):

| stack | | stack | | stack | |
|---|---|---|---|---|---|
| h2 | 15s | postgres | 28s | tidb | 86s |
| questdb | 20s | mysql | 69s | citus (16 pods) | 122s |
| monetdb | 26s | clickhouse | 51s | mssql | 69s |
| firebird | 22s | crate | 30s | oracle | 30s* |
| mariadb | ~40s* | cockroach | 28s | db2 | 159s |
| | | yugabyte | 65s | pxc | 128s |

`*` mariadb (MaxScale) and oracle needed generator fixes — see *Two fixes worth knowing* below. The harness phases
(**load, capabilities, bench, optimize, connections, loadtest, backup, logging, replication**) run against the cluster too,
verified end to end on PostgreSQL (incl. PgBouncer/HAProxy read-write split, deadlocks) and ClickHouse (Keeper replication);
the backup and logging drills additionally on MySQL and CockroachDB (`results-k3s/`, see
[kubernetes-backup-logging.md](kubernetes-backup-logging.md)). Backup drills that need a throw-away instance start a scratch
**Pod** on the backup PVC plus a NodePort Service (`harness/rdlab/k8s_scratch.py`) instead of a container on the stack
network. One thing stays Docker-only and is reported as empty, never silently wrong:

- **per-container CPU/memory** in the loadtest is read from host cgroups; under k3d the containers live inside the node
  container, so that accounting is empty (rows/s is still reported).

## Scenarios

`k8s/scenarios.sh` — Kubernetes-native behaviour stories, pure `kubectl`:

```bash
k8s/scenarios.sh deploy postgres clickhouse   # apply + wait for rollout
k8s/scenarios.sh status postgres              # pods, NodePort services, PVCs
k8s/scenarios.sh failover postgres            # delete the primary pod; Deployment recreates it on the same PVC
k8s/scenarios.sh scale-reads clickhouse 3     # scale the reader Deployment, show endpoints
k8s/scenarios.sh rolling postgres             # rolling-restart every Deployment
k8s/scenarios.sh chaos tidb                   # delete a random pod, time recovery to Ready
k8s/scenarios.sh teardown postgres            # delete the namespace (PVCs included)
k8s/scenarios.sh verify postgres              # deploy + wait for rollout, print PASS/FAIL (no teardown)
k8s/scenarios.sh verify-all                   # every stack in turn: deploy -> wait -> teardown (resource-safe)
```

**Measured (PostgreSQL, this host):** deleting the primary pod → recreated and Ready on the reattached
PersistentVolume in **5 s**, all 300k rows intact, and streaming replication re-established itself automatically when
the primary returned. The replica pod stayed up throughout and kept serving reads through the HAProxy read endpoint.

> `failover` here is the Kubernetes self-heal (recreate the pod on its volume), which is the right model for a
> single-writer stack backed by a PVC. Replica **promotion** — the data-plane failover the compose `--failover` path drives —
> is `k8s/scenarios-backup.sh promote <stack>`: the replication phase with `--failover` against the cluster (scale the primary
> Deployment to 0, promote the replica pod, time the first successful write; PostgreSQL 6.4 s vs 0.5 s with `docker kill` on
> compose, because Deployment scale-down and pod deletion dominate).

Backup CronJobs, restore/PITR and logging drills, the log sidecar and `promote` live in `k8s/scenarios-backup.sh` —
see [kubernetes-backup-logging.md](kubernetes-backup-logging.md).

## Two fixes worth knowing (compose worked, k8s did not)

Both are cases where Docker does something implicitly that Kubernetes does not, now handled by `x-k8s:` hints in the
compose file (compose ignores `x-*` keys, so the compose runtime is unaffected):

1. **MaxScale (mariadb) crash-looped.** The image runs `monit -I` in the foreground, and monit adds a `check system
   <hostname>` entry. The generator had defaulted every pod's `hostname` to the compose *service name*, so the hostname
   was `maxscale`, colliding with monit's own `maxscale` service -> monit exits -> container "Completed" -> CrashLoopBackOff.
   Docker never set that hostname (it used a random id). Fix: the generator only sets a pod `hostname` when the compose
   service declares one explicitly (crate, clickhouse, citus need it); otherwise the pod keeps its unique pod name.

2. **Oracle crash-looped with `ORA-01081`.** The `gvenzl` *faststart* image bakes a pre-built database into the image at
   `/opt/oracle/oradata`. A Docker named volume is **seeded from the image** on first use; a Kubernetes PVC mounts **empty**
   and masks the baked DB, so Oracle found no database, and the `/dev/shm` emptyDir (the SGA) persisting across container
   restarts then locked it into "already-running". Fix: two `x-k8s` hints — `seed_from_image` (an initContainer copies the
   image path into the PVC when empty, mimicking Docker) and `shm_reset` (clear `/dev/shm` before the entrypoint each start).
   Oracle then reaches Ready in ~30s and survives a pod delete in ~11s.

### `x-k8s:` hints (per compose service; ignored by compose)

| hint | effect |
|---|---|
| `port: N` | the port other services wait for / that gets exposed, when the service publishes none (ClickHouse Keeper 9181) |
| `startup_seconds: N` | floor for the startupProbe budget — a failing startupProbe kills the pod, so slow first boots need headroom |
| `seed_from_image: {IMG_PATH: VOL}` | initContainer copies the image's `IMG_PATH` into PVC `VOL` when empty (Docker-style volume seeding) |
| `shm_reset: true` | clear `/dev/shm` before the entrypoint each start (stale SGA -> Oracle `ORA-01081` on restart) |

## Files

| path | what |
|---|---|
| `k8s/harbor/up.sh`, `setup.py` | install/start Harbor; create the `rdlab` project + `pusher`/`k3s` robots |
| `k8s/push-images.sh` | mirror every stack image into Harbor (crane fallback for images the daemon won't push), lock digests |
| `k8s/gen.py` | compose → k8s generator: `images`, `ports`, `k3d`, `manifests` subcommands |
| `k8s/up.sh` / `down.sh` | create/delete the k3d cluster (from `k8s/k3d.yaml` + `k8s/registries.yaml`) |
| `k8s/scenarios.sh` | deploy / status / failover / scale-reads / rolling / chaos / teardown |
| `k8s/ports.json` | host port → nodePort map (stable; recreate the cluster after it changes) |
| `k8s/images.lock.json` | source image → Harbor ref + pushed digest |
| `stacks/<engine>/k8s/` | generated Namespace, PVCs, ConfigMaps, Services, Deployments/Jobs, kustomization |
| `harness/rdlab/platform.py` | the `RDLAB_PLATFORM=k3s` shim dockerctl and config delegate to |
| `harness/rdlab/k8s_scratch.py` | scratch restore Pods + NodePort Services (the k8s form of the drills' throw-away containers) |
| `k8s/gen-backup-addons.py`, `k8s/addons/backup/<stack>/` | one backup CronJob per stack into its backup PVC |
| `k8s/scenarios-backup.sh` | schedule / backup-now / artifacts / restore-drill / logging-drill / logs-sidecar / promote / verify-backups |

## Regenerating after a compose change

```bash
make -C k8s gen                     # re-render manifests + ports + k3d config
# if ports.json changed, the cluster's published ports are fixed at creation:
k8s/up.sh --recreate
kubectl apply -k stacks/<engine>/k8s
```

## Tear down

```bash
k8s/scenarios.sh teardown postgres   # one stack: delete its namespace (PVCs included)
k8s/down.sh                          # delete the k3d cluster (Harbor keeps running)
make -C k8s harbor-down              # stop Harbor; the mirrored images and its database stay under k8s/harbor/data
```

## Gaps / not done yet

- cgroup accounting on k8s (above) — `kubectl top` / metrics-server would be the replacement.
- The backup, logging and `promote` drills were verified on postgres, mysql, clickhouse and cockroach (`results-k3s/`); the
  other 13 stacks have their CronJob and sidecar specs generated but their drills have only run on compose.
- StatefulSets: stacks use Deployments + PVCs (fine for the fixed single-instance-per-role topology here); a
  scale-out engine would want a StatefulSet. `ulimits` (CrateDB memlock, others nofile) don't map to k8s — node
  defaults apply, noted as warnings by `gen.py`.
- All 17 containerized stacks are verified to reach Ready; the full harness phase set was exercised end to end on
  PostgreSQL and ClickHouse specifically (the others deploy and pass their own readiness/DB healthchecks).
