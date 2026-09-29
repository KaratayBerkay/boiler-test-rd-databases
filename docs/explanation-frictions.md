# How the frictions are absorbed

k8s-only knobs live in the compose file as `x-k8s:` hints — ignored by Compose, read by the generator.

## Registry, rootless

- **Push over loopback** — Docker trusts `127.0.0.1` as insecure, so a self-signed cert needs no daemon change or root.
- **LAN-IP hostname** — host and nodes must reach the same address; Harbor puts it in every token realm.
- **Robot accounts** — project-scoped push for CI, pull-only for nodes; a leaked secret costs one project.
- **crane fallback** — copies registry-to-registry when the containerd store refuses a multi-arch push.

## x-k8s: hints

| Hint | Purpose |
|---|---|
| `port: N` | The port peers wait on when a service publishes none (Keeper 9181) |
| `startup_seconds` | Floor for the startupProbe budget — slow first-boot creation |
| `seed_from_image` | initContainer copies image-baked data into an empty PVC |
| `shm_reset` | Clear `/dev/shm` before the entrypoint each start |

## Failover, as Kubernetes self-heal

Delete the PostgreSQL primary pod → the Deployment recreates it on the same PersistentVolume in **5 s**, all 300,000 rows intact, and streaming replication re-establishes itself automatically. The replica pod stays up and keeps serving reads through HAProxy throughout.
