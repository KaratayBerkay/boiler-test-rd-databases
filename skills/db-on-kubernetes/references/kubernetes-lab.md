# Kubernetes lab reference — toolchain, k3d config, per-engine notes, runbook

Concrete detail behind `SKILL.md`. Paths are from the rd-databases lab (`k8s/` + generated `stacks/<engine>/k8s/`).

## Toolchain (one-time, then per-change)

```
cp k8s/.env.example k8s/.env   # HARBOR_HOST = a LAN IP both host and k3d nodes reach; admin/db passwords
k8s/harbor/up.sh               # official installer via docker compose; creates project + push/pull robots -> .env
k8s/push-images.sh             # docker tag+push via 127.0.0.1 (crane fallback); writes images.lock.json (digests)
make -C k8s gen                # docker compose config -> stacks/*/k8s/ + ports.json + k3d.yaml + registries.yaml
k8s/up.sh                      # k3d cluster create --config k3d.yaml --registry-config registries.yaml; smoke pull
kubectl apply -k stacks/<e>/k8s
```

Generator (`k8s/gen.py`, run under the harness venv) subcommands:
- `images` — print `<source image> <harbor ref>` for every stack image (single source of the push list and the manifest image refs).
- `ports` — (re)write `ports.json`: a **stable** host-port→nodePort map for every published port and every `backup.restore_port`. Existing assignments are kept; new ports appended. Cluster nodePort range set to 30000-32767.
- `k3d` — write `k3d.yaml` (port publishes, CA mount, kubelet log-rotation args, `service-node-port-range`) + `registries.yaml` (mirror + robot auth). Ports use nodeFilter `server:0:direct` (loadbalancer disabled).
- `manifests [keys...]` — write `stacks/<key>/k8s/{namespace,pvcs,configmaps,services,workloads}.yaml + kustomization.yaml`.

## k3d.yaml essentials

```yaml
apiVersion: k3d.io/v1alpha5
kind: Simple
servers: 1
agents: 0
image: rancher/k3s:v1.35.5-k3s1          # pull via mirror.gcr.io first (Hub rate limit)
ports:                                    # one per distinct nodePort; direct, not via serverlb
  - { port: "127.0.0.1:30025:30025", nodeFilters: ["server:0:direct"] }
volumes:
  - { volume: "<abs>/k8s/harbor/certs/ca.crt:/etc/ssl/certs/harbor-ca.crt", nodeFilters: ["all"] }
options:
  k3d: { disableLoadbalancer: true }
  k3s:
    extraArgs:
      - { arg: "--disable=traefik", nodeFilters: ["server:*"] }
      - { arg: "--kubelet-arg=container-log-max-size=50Mi", nodeFilters: ["all"] }   # NOT containerLogMaxSize
      - { arg: "--kubelet-arg=container-log-max-files=3", nodeFilters: ["all"] }
      - { arg: "--kube-apiserver-arg=service-node-port-range=30000-32767", nodeFilters: ["server:*"] }
```
Create with `k3d cluster create --config k3d.yaml --registry-config registries.yaml` (the separate file avoids k3d's
`$VAR` expansion eating the `$` in `robot$project+user`).

`registries.yaml`:
```yaml
mirrors:
  <harbor-host>:<port>: { endpoint: ["https://<harbor-host>:<port>"] }
  docker.io:            { endpoint: ["https://mirror.gcr.io"] }        # k3s system images
configs:
  <harbor-host>:<port>:
    auth: { username: "robot$rdlab+k3s", password: "<pull-secret>" }
    tls:  { ca_file: /etc/ssl/certs/harbor-ca.crt }
```

## Probe mapping (from a compose healthcheck)

```
readinessProbe: exec(healthcheck), periodSeconds=interval, timeoutSeconds=timeout, failureThreshold=3
startupProbe:   exec(healthcheck), periodSeconds=interval, timeoutSeconds=timeout,
                failureThreshold = max(retries + start_period/interval, 120, x-k8s.startup_seconds/interval)
# no livenessProbe (a failover scenario stops a primary on purpose)
```
`CMD-SHELL` → `exec sh -c "<test>"`; `CMD` → `exec [<argv>]`.

## Per-engine k8s notes (beyond the compose bootstrap)

| engine | k8s-specific |
|---|---|
| PostgreSQL / Citus | works as-is; replica bootstraps via Service DNS (`-h primary`), needs `publishNotReadyAddresses`. Citus init is a Job with wait-inits for coordinator+workers+replicas |
| MariaDB (MaxScale) | **do not set pod hostname** (monit `check system` collides with the `maxscale` service) — the #1 gotcha |
| Oracle (gvenzl faststart) | `x-k8s: {seed_from_image: {/opt/oracle/oradata: oracle}, shm_reset: true, startup_seconds: 900}` — empty PVC masks the baked DB; shm persists across restarts |
| ClickHouse | Keeper publishes no client port → `x-k8s: {port: 9181}` so the servers' wait-init has a port to poll |
| Db2 | `securityContext.privileged: true`; slow first start (~2-3 min) — keep the startup budget high |
| SQL Server AG | one-shot AG-setup Job with wait-inits; each replica needs its explicit `hostname` (AG endpoints) → keep it in compose so the generator preserves it |
| TiDB | PD/TiKV/TiDB are separate Deployments + Services; TiKV waits on PD's client port |
| Crate / ClickHouse / Citus / Cockroach / Yugabyte | declare `hostname:` in compose (node identity) — the generator preserves explicit hostnames, only the default is dropped |
| embedded (SQLite, DuckDB) | no compose, no manifests — in-process only |

## Runbook — CrashLoopBackOff / not-Ready

```
kubectl -n <ns> get pod <p> -o jsonpath='{.status.containerStatuses[0].lastState}'   # exitCode/reason
kubectl -n <ns> logs <p> --previous                                                  # why it died last time
kubectl -n <ns> describe pod <p> | tail -20                                          # events (probe, pull, OOM)
```
- **exitCode 0, "Completed", instantly** → the process daemonized/forked and the foreground returned (run it in the
  foreground; for MaxScale the real cause was the hostname/monit collision).
- **`ORA-01081` / "already-running"** → stale `/dev/shm` across restart → `shm_reset` (gotcha #3).
- **"database already initialized" on a fresh PVC, or DB missing** → PVC masked image-baked data → `seed_from_image` (#2).
- **killed ~every N×period seconds, mid-init** → startupProbe budget too small (#4).
- **ImagePullBackOff `401 unauthorized`** → node registry auth: pass `registries.yaml` as a file; check the robot
  secret and that the CA is mounted.
- **cluster create hangs at "cluster dns configmap"** then rolls back → k3s couldn't pull system images → add the
  `docker.io` mirror.

## Scenarios (`k8s/scenarios.sh`)

`deploy | status | failover | scale-reads | rolling | chaos | teardown | verify | verify-all`. `verify-all` deploys →
waits for every Deployment Available + every Job Complete → tears down, one stack at a time (resource-safe on a busy
host). `failover` deletes the primary pod and times its self-heal on the reattached PVC.

`k8s/scenarios-backup.sh` (`docs/kubernetes-backup-logging.md`): `schedule | unschedule | backup-now | artifacts | restore-drill |
logging-drill | logs-sidecar | promote | verify-backups`. `schedule` applies the generated CronJob (`k8s/gen-backup-addons.py` ->
`k8s/addons/backup/<stack>/`, the engine's client image writing into the backup PVC, keeps 5); `restore-drill`/`logging-drill` run
the harness backup/logging phases with `RDLAB_PLATFORM=k3s RDLAB_RESULTS_DIR=results-k3s` (scratch restore Pods + NodePort on the
reserved restore port); `logs-sidecar` patches the primary Deployment with a `logs` container tailing the engine's log files on the
PVC; `promote` is the replication phase with `--failover` (scale the primary to 0, promote the replica pod).
