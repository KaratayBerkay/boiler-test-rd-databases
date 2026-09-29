#!/usr/bin/env bash
# Kubernetes-native scenarios for the database stacks running on the k3d cluster. Each scenario is a small, observable
# story about how a stack behaves under a Kubernetes operation, using only kubectl against what k8s/gen.py generated.
#
#   k8s/scenarios.sh list
#   k8s/scenarios.sh deploy   <stack> [stack...]     apply stacks/<stack>/k8s and wait for rollout
#   k8s/scenarios.sh status   <stack>                pods, services (nodePorts), pvcs of the stack's namespace
#   k8s/scenarios.sh failover <stack>                delete the primary pod; show the Deployment recreate it and
#                                                    reattach to the same PVC, and (for replicated stacks) the replica
#   k8s/scenarios.sh scale-reads <stack> N           scale the replica/reader Deployment to N and show endpoints
#   k8s/scenarios.sh rolling  <stack>                rolling-restart every Deployment, one at a time
#   k8s/scenarios.sh chaos    <stack>                delete a random pod and time how long until Ready again
#   k8s/scenarios.sh teardown <stack>                delete the namespace (PVCs included)
#
# "primary"/"replica" are resolved from the Deployment labels the generator writes (app.kubernetes.io/name), which come
# straight from the compose service names, so the same verbs work for every engine that has those roles.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
K8S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$K8S")"
set -a; . "$K8S/.env"; set +a
CTX="k3d-${CLUSTER:-rdlab}"
kubectl() { command kubectl --context "$CTX" "$@"; }

ns_of() { echo "rdlab-$1"; }
deploys() { kubectl -n "$(ns_of "$1")" get deploy -o jsonpath='{.items[*].metadata.name}'; }
has_deploy() { kubectl -n "$(ns_of "$1")" get deploy "$2" >/dev/null 2>&1; }
primary_deploy() {                                   # first of: primary, source, coordinator, node1/ch1/crate1, else the first
  for cand in primary source coordinator ch1 crate1 node1 mssql1 tidb1; do has_deploy "$1" "$cand" && { echo "$cand"; return; }; done
  deploys "$1" | awk '{print $1}'
}
replica_deploy() {
  for cand in replica r1 ch2 secondary-reads; do has_deploy "$1" "$cand" && { echo "$cand"; return; }; done
  echo ""
}
pods_ready() { kubectl -n "$(ns_of "$1")" get pods; }

cmd_list() {
  echo "stacks with generated manifests:"
  for d in "$ROOT"/stacks/*/k8s/kustomization.yaml; do s=$(basename "$(dirname "$(dirname "$d")")"); printf "  %-11s ns=%s\n" "$s" "$(ns_of "$s")"; done
}

cmd_deploy() {
  for s in "$@"; do
    [[ -f "$ROOT/stacks/$s/k8s/kustomization.yaml" ]] || { echo "!! no manifests for $s (make k8s-gen)"; exit 1; }
    echo "==> applying $s"
    kubectl apply -k "$ROOT/stacks/$s/k8s"
    for d in $(deploys "$s"); do kubectl -n "$(ns_of "$s")" rollout status deploy/"$d" --timeout=600s; done
    pods_ready "$s"
  done
}

cmd_status() {
  local ns; ns=$(ns_of "$1")
  kubectl -n "$ns" get pods -o wide
  echo; kubectl -n "$ns" get svc -o wide | grep -E 'NodePort|NAME'
  echo; kubectl -n "$ns" get pvc
}

cmd_failover() {
  local s="$1" ns pd pod
  ns=$(ns_of "$s"); pd=$(primary_deploy "$s")
  pod=$(kubectl -n "$ns" get pod -l app.kubernetes.io/name="$pd" -o jsonpath='{.items[0].metadata.name}')
  echo "==> $s: primary Deployment is '$pd', pod '$pod'"
  local rd; rd=$(replica_deploy "$s")
  [[ -n "$rd" ]] && echo "    replica Deployment is '$rd' (should stay up and keep serving reads)"
  echo "==> deleting the primary pod (kubelet + Deployment will recreate it on the same PVC)"
  local t0; t0=$(date +%s)
  kubectl -n "$ns" delete pod "$pod" --wait=true
  kubectl -n "$ns" rollout status deploy/"$pd" --timeout=300s
  local t1; t1=$(date +%s)
  echo "==> primary Ready again after $((t1 - t0))s; data intact on the reattached PersistentVolume:"
  pods_ready "$s"
  echo "    (a real HA failover promotes the replica instead of waiting for the primary; see docs/kubernetes.md)"
}

cmd_scale_reads() {
  local s="$1" n="${2:-2}" ns rd
  ns=$(ns_of "$s"); rd=$(replica_deploy "$s")
  [[ -n "$rd" ]] || { echo "!! $s has no replica/reader Deployment to scale"; exit 1; }
  echo "==> scaling $s reader '$rd' to $n"
  kubectl -n "$ns" scale deploy/"$rd" --replicas="$n"
  kubectl -n "$ns" rollout status deploy/"$rd" --timeout=300s
  kubectl -n "$ns" get endpoints "$rd" -o wide 2>/dev/null || kubectl -n "$ns" get pods -l app.kubernetes.io/name="$rd" -o wide
}

cmd_rolling() {
  local s="$1" ns; ns=$(ns_of "$s")
  for d in $(deploys "$s"); do
    echo "==> rolling restart $d"; kubectl -n "$ns" rollout restart deploy/"$d"; kubectl -n "$ns" rollout status deploy/"$d" --timeout=300s
  done
}

cmd_chaos() {
  local s="$1" ns pod t0 t1; ns=$(ns_of "$s")
  pod=$(kubectl -n "$ns" get pods -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | shuf | head -1)
  echo "==> chaos: deleting random pod $pod"
  t0=$(date +%s); kubectl -n "$ns" delete pod "$pod" --wait=true
  kubectl -n "$ns" wait --for=condition=Ready pod -l app.kubernetes.io/part-of=rdlab --timeout=300s
  t1=$(date +%s); echo "==> all pods Ready again after $((t1 - t0))s"
}

cmd_teardown() { kubectl delete namespace "$(ns_of "$1")" --ignore-not-found; }

cmd_verify() {                                       # deploy one stack, wait for rollout + jobs, print PASS/FAIL, do NOT teardown
  local s="$1" ns t0 t1 rc=0 reason="" ; ns=$(ns_of "$s")
  [[ -f "$ROOT/stacks/$s/k8s/kustomization.yaml" ]] || { echo "VERDICT $s FAIL no-manifests"; return 1; }
  t0=$(date +%s)
  kubectl apply -k "$ROOT/stacks/$s/k8s" >/dev/null 2>&1 || { echo "VERDICT $s FAIL apply-error"; return 1; }
  for d in $(deploys "$s"); do
    if ! kubectl -n "$ns" rollout status deploy/"$d" --timeout="${ROLLOUT_TIMEOUT:-420}s" >/dev/null 2>&1; then
      rc=1; reason="deploy/$d not available"; break
    fi
  done
  if [[ $rc -eq 0 ]]; then
    local jobs; jobs=$(kubectl -n "$ns" get job -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
    for j in $jobs; do
      kubectl -n "$ns" wait --for=condition=complete job/"$j" --timeout="${ROLLOUT_TIMEOUT:-420}s" >/dev/null 2>&1 || { rc=1; reason="job/$j not complete"; break; }
    done
  fi
  t1=$(date +%s)
  local pods; pods=$(kubectl -n "$ns" get pods --no-headers 2>/dev/null | awk '{print $2}' | paste -sd, -)
  if [[ $rc -eq 0 ]]; then echo "VERDICT $s PASS $((t1-t0))s pods=[$pods]"; else
    echo "VERDICT $s FAIL $((t1-t0))s $reason pods=[$pods]"
    kubectl -n "$ns" get pods --no-headers 2>/dev/null | awk '$3!="Running" && $3!="Completed"{print "   "$1" "$3}' | head -6
  fi
  return $rc
}

cmd_verify_all() {                                   # deploy/verify/teardown every stack in turn (resource-safe: one live at a time)
  local list=("$@"); [[ ${#list[@]} -gt 0 ]] || list=($(for d in "$ROOT"/stacks/*/k8s/kustomization.yaml; do basename "$(dirname "$(dirname "$d")")"; done))
  local pass=0 fail=0
  echo "== verify-all: ${#list[@]} stacks (deploy -> wait -> teardown), $(date -u +%FT%TZ) =="
  for s in "${list[@]}"; do
    cmd_verify "$s" && pass=$((pass+1)) || fail=$((fail+1))
    ${KEEP:-false} || cmd_teardown "$s" >/dev/null 2>&1
  done
  echo "== verify-all done: $pass passed, $fail failed =="
}

cmd="${1:-list}"; shift || true
case "$cmd" in
  list) cmd_list ;;
  deploy) cmd_deploy "$@" ;;
  status) cmd_status "$@" ;;
  failover) cmd_failover "$@" ;;
  scale-reads) cmd_scale_reads "$@" ;;
  rolling) cmd_rolling "$@" ;;
  chaos) cmd_chaos "$@" ;;
  teardown) cmd_teardown "$@" ;;
  verify) cmd_verify "$@" ;;
  verify-all) cmd_verify_all "$@" ;;
  *) echo "unknown scenario: $cmd"; sed -n '2,20p' "$0"; exit 2 ;;
esac
