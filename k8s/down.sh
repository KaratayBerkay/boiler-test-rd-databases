#!/usr/bin/env bash
# Delete the k3d cluster (all namespaces, PVCs and pulled images go with it). Harbor keeps running: k8s/harbor/up.sh --down
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
K8S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a; . "$K8S/.env"; set +a
k3d cluster delete "${CLUSTER:-rdlab}"
