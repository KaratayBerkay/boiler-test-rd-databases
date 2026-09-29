# How a stack gets to the cluster

Two pipelines, one source of truth — the compose file.

## Manifests pipeline

```
stacks/<engine>/compose.yaml          the tested topology — source of truth
        ↓
k8s/gen.py manifests                  reads `docker compose config`; maps services→Deployments, volumes→PVCs, healthchecks→probes
        ↓
kubectl apply -k                      Namespace · PVCs · ConfigMaps · Services · workloads
        ↓
k3d — 1 server, 0 agents              NodePorts published on 127.0.0.1 so lab configs connect unchanged
```

## Images pipeline

```
24 engine images + busybox            ≈35 GB across Postgres…Db2
        ↓
k8s/push-images.sh                    docker push via 127.0.0.1 (rootless); crane fallback for images containerd won't push
        ↓
Harbor · project rdlab               private; push + pull-only robot accounts; digests in images.lock.json
        ↓
k3s nodes pull                       pull-only robot + mounted CA (registries.yaml)
```

## One switch drives the harness

`RDLAB_PLATFORM=k3s` routes container ops (exec, logs, lifecycle) to `kubectl` — resolving each compose container name to its pod via the `rdlab.io/container` label — and remaps every target port to its NodePort. Load, capabilities, bench, connections and replication all run against the cluster.
