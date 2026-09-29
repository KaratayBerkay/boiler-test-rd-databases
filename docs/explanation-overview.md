# Overview — rd-databases on k3s

**Every engine in the lab, now on one k3s cluster.**

The Docker Compose stacks that benchmark 20 SQL engines also run on k3d, pulling from a private Harbor registry. Manifests are generated from the compose files, so Compose stays the single source of truth — and all 17 containerized engines reach Ready on a single node.

## Tiles

| Tile | Value | Explanation |
|---|---|---|
| Containerized engines deploy & reach Ready | **17/17** | All 17 containerized engines deploy and reach Ready on the cluster. SQLite and DuckDB are embedded (in-process, no containers), so they have no manifests — 17 is the full containerized set. |
| Images mirrored into local Harbor, digest-locked | **25** | 25 images (24 engine images + busybox) are mirrored into the local Harbor registry, digest-locked for reproducibility. |
| Services generated as Deployments, Jobs & PVCs | **~77** | Approximately 77 Kubernetes services are generated from the compose files as Deployments, Jobs, and PersistentVolumeClaims. |
| Primary self-heal on the reattached volume | **5s** | When the PostgreSQL primary pod is deleted, the Deployment recreates it on the same PersistentVolume in 5 seconds, with all data intact. |

## One switch drives the harness

`RDLAB_PLATFORM=k3s` routes container ops (exec, logs, lifecycle) to `kubectl` — resolving each compose container name to its pod via the `rdlab.io/container` label — and remaps every target port to its NodePort. Load, capabilities, bench, connections and replication all run against the cluster.
