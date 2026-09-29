---
name: db-on-kubernetes
description: Run stateful SQL database stacks on k3s/k3d with a local Harbor registry - generating Kubernetes manifests from Docker Compose files, mirroring images into Harbor (rootless, robot accounts, crane fallback), and the non-obvious failures that only appear on Kubernetes and not under Docker (pod hostname collisions, empty PVCs masking image-baked data, /dev/shm persisting across restarts, startup probes killing slow-booting databases, k3d registry auth with '$' in robot names). Verified by bringing up 17 engines (PostgreSQL, Citus, MySQL, MariaDB/MaxScale, Percona XtraDB, TiDB, CockroachDB, YugabyteDB, SQL Server, Oracle, Db2, ClickHouse, CrateDB, QuestDB, MonetDB, Firebird, H2) on one k3d cluster. Use when moving database Compose stacks to Kubernetes, setting up a private registry for a cluster, writing Deployments/StatefulSets/PVCs/NodePorts for databases, or debugging CrashLoopBackOff / ImagePullBackOff / stuck-init database pods.
---

# Databases on k3s (k3d) with a local Harbor registry

Built as a second runtime for the rd-databases lab: the Docker Compose stacks (one per engine) also run on a k3d
cluster, pulling every image from a local Harbor. **Compose stays the source of truth**; the Kubernetes manifests are
generated from `docker compose config` so a compose change flows into both. Verified: all 17 containerized engines
reach Ready on one k3d node.

## Shape and why

| choice | why |
|---|---|
| **k3d** (k3s in Docker), not host k3s | no root, disposable, one config file; joins the Docker host it already runs on |
| **1 server node, 0 agents** | benchmarks were measured on one host; shared RWO PVCs (restore drills, replica bootstrap) need co-located pods |
| **local Harbor**, not Docker Hub | one private mirror of ~35 GB of images: no Hub rate limits, no per-node re-pull of a 7 GB Db2 image |
| **generate from compose** | 17 stacks, ~77 services - hand-maintaining manifests drifts. Read `docker compose config` (anchors/env resolved) |
| **Deployment + headless Service**, `publishNotReadyAddresses: true` | cluster bootstrap traffic (a replica running `pg_basebackup` against a not-yet-ready primary, `citus_add_node`) must flow before readiness, like a compose network |
| **NodePort per published port** on `127.0.0.1:<nodePort>` | the client connects to `127.0.0.1`; a fixed host-port→nodePort table keeps connection configs working, and loopback keeps a self-signed registry cert trusted with no daemon change |
| **readiness + startup probes, NO liveness** | a failover test stops a primary on purpose; a liveness probe fights it |

## Compose → Kubernetes mapping

| compose | Kubernetes |
|---|---|
| long-running service | Deployment (1 replica, `Recreate`) + headless Service (DNS = service name) + NodePort Service per published port |
| one-shot that only prepares a shared volume (`restart: "no"`, no deps) | initContainer of each dependent |
| one-shot setup job that needs the DBs up | Job with `wait-for` initContainers |
| named volume | PersistentVolumeClaim, same name (RWO; one node) |
| bind-mounted file / dir | ConfigMap (subPath mount for a file) |
| `healthcheck` | readinessProbe + startupProbe (never a livenessProbe) |
| `depends_on: condition: service_healthy` | `wait-<dep>` initContainer (`nc -z` the dep's first port) |
| `deploy.resources.limits`, `shm_size` | `resources.limits`, `emptyDir{medium: Memory}` on `/dev/shm` |
| `privileged`, `cap_add`, `user: root` | `securityContext` |
| `container_name` | label `rdlab.io/container` (how tooling finds the pod) + a second headless Service on that name |
| `image` | `<harbor-host:port>/<project>/<path>:<tag>` (mirrored, pinned by digest) |

## The failures that only happen on Kubernetes (each verified, with the fix)

1. **Pod hostname collision → CrashLoopBackOff.** Do **not** default a pod's `hostname` to the compose service name.
   Docker gives a container a random hostname unless told otherwise; a generator that sets `hostname: <service>` can
   collide with software that registers a service by hostname. MaxScale runs `monit -I` in the foreground, monit adds a
   `check system <hostname>` entry, and hostname `maxscale` collided with monit's own `maxscale` service → monit exits →
   container "Completed" → crash loop. **Fix:** set a pod hostname only when the compose service declares one explicitly
   (nodes that need stable identity - Crate `node.name`, ClickHouse macros, Citus - do declare it); otherwise leave the
   pod's own name. Service DNS (for `-h primary`) comes from the Service, not the pod hostname, so nothing breaks.

2. **Empty PVC masks image-baked data → the DB "disappears".** A Docker **named volume is seeded from the image**'s
   contents at that path on first use; a Kubernetes **PVC mounts empty** and masks whatever the image baked there. The
   Oracle `gvenzl` *faststart* image ships a pre-built database at `/opt/oracle/oradata`; a PVC there hid it, Oracle
   found no DB, and crash-looped. **Fix:** an initContainer that copies the image's directory into the PVC when the PVC
   is empty (mount the PVC at a scratch path, `cp -a /opt/oracle/oradata/. /scratch/`), then the main container mounts
   the now-populated PVC. This is the general fix for any faststart / seed-in-image database.

3. **`/dev/shm` emptyDir persists across container restarts → Oracle `ORA-01081`.** `emptyDir{medium: Memory}` lives for
   the pod's lifetime, not the container's, so a restarted DB container finds the previous instance's shared-memory
   segments (SGA) still present and refuses to start ("cannot start already-running ORACLE"). **Fix:** clear `/dev/shm`
   before the entrypoint each start (`command: ["sh","-c","rm -rf /dev/shm/* 2>/dev/null||true; exec <entrypoint>"]`).

4. **A startupProbe KILLS the pod; a compose healthcheck does not.** A failing Docker healthcheck only flips the
   container to "unhealthy" - the process keeps running and finishes its slow first boot. A failing Kubernetes
   startupProbe restarts the container. Map a healthcheck to a startupProbe with a **generous** budget
   (`failureThreshold * periodSeconds` ≥ the real worst-case first boot; floor it, and allow an override) or a
   slow-initialising engine (Oracle creating a DB, Db2) gets killed mid-init and corrupts its volume into a crash loop.

5. **k3d registry auth: pass `--registry-config <file>`, not the inline `registries:` block.** k3d expands `$VAR` in
   its config file, which eats the `$` in a Harbor robot name (`robot$project+user`). Give the mirror+auth as a separate
   `registries.yaml` file instead. Mount the registry CA into every node (`volumes: [<ca>:/etc/ssl/certs/harbor-ca.crt@all]`).

6. **Disabled load balancer needs `server:0:direct` port mappings.** With `disableLoadbalancer: true`, `--port` mappings
   of the default proxy type are rejected; publish directly on the server node. Publish nodePorts on `127.0.0.1:<nodePort>`
   (not the compose host port - a running compose stack may hold it).

7. **k3s pulls its own system images from Docker Hub on first start** (coredns, local-path, metrics-server, pause):
   add a `docker.io` mirror (`mirror.gcr.io`) to `registries.yaml` or first boot hits the anonymous Hub rate limit.

8. **`kubectl exec` has no `-u`.** Tools that `docker exec -u postgres` must, on k8s, `gosu`/`su` inside the pod (or the
   container must already run as that user).

## Harbor, rootless

- Push via `127.0.0.1:<port>` - Docker treats loopback as an insecure registry, so a self-signed cert needs no
  daemon config or root. The registry hostname is a **LAN IP** both the host and the k3d nodes can reach (not
  `127.0.0.1`, which every node would resolve to itself), because Harbor puts it in the token realm of every 401.
- **Robot accounts, not the admin user**: one scoped to the project, push+pull for CI, pull-only for the nodes;
  a leaked robot secret costs one project. Harbor shows a robot secret once - capture it immediately.
- **crane fallback**: Docker's containerd image store sometimes refuses to push an image it pulled fine
  ("image ... does not provide any platform", seen on multi-arch indexes). `crane copy --insecure <upstream> <harbor>`
  copies registry-to-registry, bypassing the local store.
- Lock what the cluster pulls to a **digest** after pushing (an `images.lock.json`), so the tag can move without the
  cluster silently changing.

## `x-k8s:` hints (compose extension; compose ignores `x-*`)

Keep the k8s-only knobs in the compose file so it stays the single source of truth:

| hint | effect |
|---|---|
| `port: N` | the port other services wait for / that is exposed, when the service publishes none (ClickHouse Keeper 9181) |
| `startup_seconds: N` | floor for the startupProbe budget (slow first-boot DB creation) |
| `seed_from_image: {IMG_PATH: VOL}` | initContainer copies the image's `IMG_PATH` into PVC `VOL` when empty (gotcha #2) |
| `shm_reset: true` | clear `/dev/shm` before the entrypoint each start (gotcha #3) |

## Deployment vs StatefulSet

These stacks use **Deployment + PVC**: the topology is fixed (one instance per role - primary, replica, w1..w5), each
role is its own Service and PVC, and `Recreate` + a per-role PVC gives stable storage without ordinals. Reach for a
**StatefulSet** when you need N interchangeable ordered replicas with per-replica PVCs from one spec (a scale-out
cluster you grow with `scale`), stable ordinal DNS, or ordered rolling updates.

## Restore drills and resource accounting on k8s

- Backup **restore drills** (start a throw-away DB from a backup and verify it) run as **scratch Pods** that mount the
  same backup PVC (RWO is fine: one node) plus a NodePort Service on the restore port - the k8s counterpart of
  `docker run` on the stack network with the backup volume.
- **Per-container CPU/memory** measured from host cgroups does not work under k3d (containers live inside the node
  container); use `kubectl top` / metrics-server instead, or run the load test on compose for exact cgroup numbers.

## Verified (one k3d node, Sept 2026)

17/17 containerized engines reach Ready (deploy → wait → teardown, one at a time). Fast single nodes 15-30 s
(h2, questdb, monetdb, firebird, oracle-with-seed); primary+replica+proxy stacks 28-70 s (postgres, mysql, mariadb,
clickhouse); multi-node clusters 65-160 s (cockroach, yugabyte, tidb 7 pods, pxc, citus 16 pods, db2). Failover as
Kubernetes self-heal: delete the primary pod → recreated on the same PVC in ~5 s, data intact, streaming replication
re-established automatically.

## Files
- `references/kubernetes-lab.md` - the concrete toolchain (generator subcommands, k3d config, per-engine k8s notes), the
  backup/restore/log-sidecar/promotion scenarios (`k8s/scenarios-backup.sh`) and a runbook for the failures above.
- Day-2 on the cluster (CronJob backups into the backup PVC, restore/PITR drills as scratch Pods, log sidecars, replica
  promotion) is covered by `skills/db-backup-recovery` and `skills/db-logging-observability` ("On Kubernetes" sections).
