# Eight failures that only happen on Kubernetes

Each one Docker papers over implicitly — caught and fixed while bringing the stacks up.

## G1 — Pod hostname collision → CrashLoopBackOff

Defaulting a pod's `hostname` to the compose service name broke MaxScale: its `monit` supervisor registers a service named `maxscale` and a `check system maxscale` hostname collides with it → monit exits → container completes → crash loop. Docker had used a random hostname.

**Fix:** Set a pod hostname only when the compose service declares one explicitly. Service DNS is unaffected.

## G2 — Empty PVC masks image-baked data

A Docker named volume is seeded from the image on first use; a k8s PVC mounts **empty** and masks it. Oracle's faststart image bakes its database into `/opt/oracle/oradata` — the PVC hid it, so Oracle found no DB.

**Fix:** `seed_from_image` — an initContainer copies the image directory into the PVC when it is empty.

## G3 — /dev/shm persists across restarts → ORA-01081

An `emptyDir{medium:Memory}` lives for the pod, not the container, so a restarted Oracle finds the old SGA still in shared memory and refuses: "cannot start already-running ORACLE."

**Fix:** `shm_reset` — clear /dev/shm before the entrypoint on every start.

## G4 — A startupProbe kills the pod; a healthcheck doesn't

A failing Docker healthcheck only marks the container unhealthy — the slow first boot finishes. A failing Kubernetes startupProbe restarts the container, cutting Oracle/Db2 off mid-init and corrupting the volume into a loop.

**Fix:** Generous startupProbe budget with a floor, plus a `startup_seconds` override for slow engines.

## G5 — k3d eats the "$" in robot names

k3d expands `$VAR` inside its config file, mangling a Harbor robot login (`robot$rdlab+k3s`) inlined under `registries:`.

**Fix:** Pass registry auth as a separate `--registry-config` file, not inline.

## G6 — Disabled load balancer rejects proxy ports

With the k3d load balancer off, default `--port` mappings are refused.

**Fix:** Publish directly on the server node (`server:0:direct`), on `127.0.0.1:<nodePort>`.

## G7 — k3s pulls its own system images from Docker Hub

coredns, local-path, metrics-server and pause come from Hub on first boot — straight into the anonymous rate limit, and the cluster hangs at "cluster dns configmap."

**Fix:** Add a `docker.io` → `mirror.gcr.io` mirror to `registries.yaml`.

## G8 — kubectl exec has no "-u"

Tooling that runs `docker exec -u postgres` can't pass a user to `kubectl exec`.

**Fix:** `gosu` / `su` inside the pod, or run the container as that user.
