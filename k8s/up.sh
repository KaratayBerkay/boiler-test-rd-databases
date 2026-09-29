#!/usr/bin/env bash
# Create (or verify) the k3d cluster the lab's Kubernetes stacks run on, wired to the local Harbor registry.
#
#   k8s/up.sh              render k8s/k3d.yaml from .env + ports.json, create the cluster if missing, smoke-test a pull
#   k8s/up.sh --recreate   delete and create again (needed after ports.json changed: port mappings are fixed at creation)
#   k8s/down.sh            delete the cluster (Harbor keeps running)
#
# Why k3d (k3s inside Docker) and not the host k3s service: no root, disposable, reproducible from one config file,
# and the same host already runs its other labs that way. One server node, no agents: every compose stack was
# measured on one host, and shared RWO volumes (backup drills, replica bootstraps) need co-located pods anyway.
# Host ports: every nodePort in k8s/ports.json is published on 127.0.0.1:<nodePort> of the server node (not on the
# compose host port, which a running compose stack may hold); the harness maps lab.yaml ports through ports.json.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
K8S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$K8S")"
export FIREBIRD_CLIENT_LIB="${FIREBIRD_CLIENT_LIB:-/dev/null}"
[[ -f "$K8S/.env" ]] || { echo "!! $K8S/.env missing (run k8s/harbor/up.sh)" >&2; exit 1; }
set -a; . "$K8S/.env"; set +a
CLUSTER="${CLUSTER:-rdlab}"
CONTEXT="k3d-${CLUSTER}"
kubectl() { command kubectl --context "$CONTEXT" "$@"; }

command -v k3d >/dev/null || {
  echo "==> installing k3d into ~/.local/bin (no root needed)"
  curl -sfL https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | USE_SUDO=false K3D_INSTALL_DIR="$HOME/.local/bin" bash
}
curl -fsk "https://127.0.0.1:${HARBOR_HTTPS_PORT:-8443}/api/v2.0/health" >/dev/null 2>&1 || { echo "!! harbor is not up: k8s/harbor/up.sh" >&2; exit 1; }

echo "==> rendering k8s/k3d.yaml (ports.json + registry auth + CA)"
( cd "$ROOT" && uv run --project harness python k8s/gen.py k3d 2>/dev/null )

if [[ "${1:-}" == "--recreate" ]] && k3d cluster list 2>/dev/null | grep -q "^$CLUSTER "; then
  echo "==> deleting cluster '$CLUSTER'"
  k3d cluster delete "$CLUSTER"
fi
if k3d cluster list 2>/dev/null | grep -q "^$CLUSTER "; then
  echo "==> cluster '$CLUSTER' exists"
  k3d cluster start "$CLUSTER" >/dev/null 2>&1 || true
else
  # the node image comes from Docker Hub: fetch it through the mirror first (anonymous Hub pulls are rate-limited)
  IMG="$(grep '^image:' "$K8S/k3d.yaml" | awk '{print $2}')"
  docker image inspect "$IMG" >/dev/null 2>&1 || "$ROOT/scripts/pull-images.sh" "$IMG"
  echo "==> creating cluster '$CLUSTER' ($(grep -c 'nodeFilters' "$K8S/k3d.yaml") host port mappings)"
  k3d cluster create --config "$K8S/k3d.yaml" --registry-config "$K8S/registries.yaml"
fi
kubectl wait --for=condition=Ready node --all --timeout=180s >/dev/null
for i in $(seq 1 60); do kubectl get sa default >/dev/null 2>&1 && break; sleep 1; done     # default SA appears a few seconds after the node
kubectl wait --for=condition=Available deploy/coredns deploy/local-path-provisioner -n kube-system --timeout=300s >/dev/null
kubectl get nodes -o wide

# smoke test: a pod that pulls the wait image from Harbor with the pull-only robot
echo "==> smoke test: pulling from ${HARBOR_HOST}:${HARBOR_HTTPS_PORT:-8443} inside the cluster"
kubectl delete pod harbor-smoke --ignore-not-found >/dev/null
kubectl run harbor-smoke --restart=Never --image="${HARBOR_HOST}:${HARBOR_HTTPS_PORT:-8443}/${HARBOR_PROJECT:-rdlab}/busybox:1.37" -- sh -c 'echo pulled-from-harbor' >/dev/null
if kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/harbor-smoke --timeout=120s >/dev/null 2>&1; then
  echo "    ok: $(kubectl logs harbor-smoke)"
else
  echo "!! smoke test failed:"; kubectl describe pod harbor-smoke | tail -12; exit 1
fi
kubectl delete pod harbor-smoke >/dev/null
echo
echo "  context   $CONTEXT        (kubectl --context $CONTEXT get ns)"
echo "  stacks    RDLAB_PLATFORM=k3s uv run --project harness rdlab run <stack> ...   or   kubectl apply -k stacks/<stack>/k8s"
