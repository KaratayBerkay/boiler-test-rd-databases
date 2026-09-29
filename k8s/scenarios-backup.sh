#!/usr/bin/env bash
# Backup, restore and logging scenarios for the stacks running on the k3d cluster (add-on to k8s/scenarios.sh).
# Everything here layers on top of the generated stack manifests without editing them: CronJobs from
# k8s/addons/backup/<stack>/ (k8s/gen-backup-addons.py), `kubectl exec` for the engines whose backup must run inside
# the server, a log sidecar patch, and the harness phases driven against the cluster (RDLAB_PLATFORM=k3s).
#
#   k8s/scenarios-backup.sh list                         stacks with a backup add-on / exec-based backup
#   k8s/scenarios-backup.sh schedule   <stack> [...]      apply the CronJob (every 6 h, keeps 5) into the stack namespace
#   k8s/scenarios-backup.sh unschedule <stack>            remove it
#   k8s/scenarios-backup.sh backup-now <stack>            run one backup now (Job from the CronJob, or kubectl exec for db2/questdb) and wait
#   k8s/scenarios-backup.sh artifacts  <stack>            what is in the backup volume (or the engine's backup catalog)
#   k8s/scenarios-backup.sh restore-drill <stack>         the harness backup phase against the cluster: backup -> scratch Pod
#                                                         restore -> fingerprint -> PITR probe; results in results-k3s/<stack>/
#   k8s/scenarios-backup.sh logging-drill <stack>         the harness logging phase against the cluster (slow-query capture,
#                                                         audit trail, cost of logging everything)
#   k8s/scenarios-backup.sh logs-sidecar <stack>          add a `logs` sidecar that tails the engine's JSON/slow/audit log
#                                                         files to stdout, then show `kubectl logs -c logs`
#   k8s/scenarios-backup.sh promote    <stack>            data-plane failover: kill the primary, promote the replica, time
#                                                         the first successful write (harness replication --failover on k3s)
#   k8s/scenarios-backup.sh verify-backups [stack...]     backup-now + artifacts for every scheduled stack, PASS/FAIL per stack
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
K8S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$K8S")"
set -a; . "$K8S/.env"; set +a
CTX="k3d-${CLUSTER:-rdlab}"
export FIREBIRD_CLIENT_LIB="${FIREBIRD_CLIENT_LIB:-/dev/null}"
kubectl() { command kubectl --context "$CTX" "$@"; }
ns_of() { echo "rdlab-$1"; }
harbor_ref() { (cd "$ROOT" && uv run --project harness python -c "import sys; sys.path.insert(0,'k8s'); import gen; print(gen.harbor_ref('$1'))"); }

ADDON_STACKS="postgres citus mysql mariadb pxc tidb cockroach yugabyte mssql oracle clickhouse crate monetdb firebird h2"
EXEC_STACKS="db2 questdb"

# stack -> "deployment pvc mountPath glob..." for the log sidecar (only stacks whose engine logs live on a PVC)
sidecar_spec() {
  case "$1" in
    postgres) echo "primary pg-primary /var/lib/postgresql /var/lib/postgresql/log/*.json" ;;
    citus)    echo "coordinator citus-coordinator /var/lib/postgresql /var/lib/postgresql/log/*.json" ;;
    mysql)    echo "source my-logs /var/log/mysql /var/log/mysql/error.log.00.json /var/log/mysql/slow.log" ;;
    mariadb)  echo "primary mdb-logs /var/log/mysql /var/log/mysql/slow.log /var/log/mysql/audit.log" ;;
    pxc)      echo "pxc1 pxc1-logs /var/log/mysql /var/log/mysql/audit.json /var/log/mysql/slow.log" ;;
    tidb)     echo "tidb1 tidb-logs /var/log/tidb /var/log/tidb/tidb-slow.log" ;;
    cockroach) echo "crdb1 crdb1 /cockroach/cockroach-data /cockroach/cockroach-data/logs/cockroach-sql-exec.log /cockroach/cockroach-data/logs/cockroach-sql-audit.log" ;;
    yugabyte) echo "yb1 yb1 /home/yugabyte/yb_data /home/yugabyte/yb_data/data/yb-data/tserver/logs/postgresql-*.log" ;;
    mssql)    echo "mssql1 mssql1 /var/opt/mssql /var/opt/mssql/log/errorlog" ;;
    db2)      echo "db2 db2 /database /database/config/db2inst1/sqllib/db2dump/DIAG0000/db2diag.log" ;;
    monetdb)  echo "monetdb monetdb /var/monetdb5/dbfarm /var/monetdb5/dbfarm/merovingian.log" ;;
    firebird) echo "firebird firebird-logs /var/log/firebird /var/log/firebird/audit.log" ;;
    h2)       echo "h2 h2 /data /data/lab.trace.db" ;;
    *) echo "" ;;
  esac
}

cmd_list() {
  echo "CronJob add-ons (k8s/addons/backup/<stack>):"; for s in $ADDON_STACKS; do printf "  %-10s %s\n" "$s" "$( [ -f "$K8S/addons/backup/$s/cronjob.yaml" ] && echo generated || echo 'run k8s/gen-backup-addons.py')"; done
  echo "exec-based (backup runs inside the server pod): $EXEC_STACKS"
  echo "embedded, no cluster backup: sqlite duckdb"
}

cmd_schedule() {
  for s in "$@"; do
    kubectl apply -k "$K8S/addons/backup/$s"
    kubectl -n "$(ns_of "$s")" get cronjob "backup-$s"
  done
}

cmd_unschedule() { kubectl delete -k "$K8S/addons/backup/$1" --ignore-not-found; }

wait_job() {   # ns job timeout
  local ns=$1 job=$2 t=${3:-900}
  if ! kubectl -n "$ns" wait --for=condition=complete "job/$job" --timeout="${t}s" 2>/dev/null; then
    echo "!! job $job did not complete"; kubectl -n "$ns" logs "job/$job" --tail=40 || true; return 1
  fi
  kubectl -n "$ns" logs "job/$job" --tail=30
}

cmd_backup_now() {
  local s=$1 ns; ns=$(ns_of "$s")
  case "$s" in
    db2)
      local pod; pod=$(kubectl -n "$ns" get pod -l app.kubernetes.io/name=db2 -o jsonpath='{.items[0].metadata.name}')
      kubectl -n "$ns" exec "$pod" -- su - db2inst1 -c "mkdir -p /backups/cron && db2 -v 'backup db lab online to /backups/cron compress include logs without prompting' || db2 -v 'backup db lab to /backups/cron compress without prompting'; ls -la /backups/cron" ;;
    questdb)
      local pod; pod=$(kubectl -n "$ns" get pod -l app.kubernetes.io/name=questdb -o jsonpath='{.items[0].metadata.name}')
      kubectl -n "$ns" exec "$pod" -- sh -c 'TS=$(date +%Y%m%d-%H%M%S); q(){ wget -qO- "http://127.0.0.1:9000/exec?query=$1"; echo; }; q "CHECKPOINT%20CREATE"; mkdir -p /backups/cron/$TS; cd /var/lib/questdb && for d in .checkpoint conf db snapshot; do [ -e $d ] && cp -a $d /backups/cron/$TS/; done; q "CHECKPOINT%20RELEASE"; du -sh /backups/cron/*' ;;
    *)
      kubectl -n "$ns" get cronjob "backup-$s" >/dev/null 2>&1 || cmd_schedule "$s"
      local job="backup-$s-manual-$(date +%H%M%S)"
      kubectl -n "$ns" create job "$job" --from="cronjob/backup-$s"
      wait_job "$ns" "$job" 900 ;;
  esac
}

cmd_artifacts() {
  local s=$1 ns; ns=$(ns_of "$s")
  case "$s" in
    cockroach) kubectl -n "$ns" exec deploy/crdb1 -- /cockroach/cockroach sql --insecure -e "SHOW BACKUPS IN 'nodelocal://1/cron'" ;;
    crate)     kubectl -n "$ns" exec deploy/crate1 -- crash --hosts localhost:4200 -c "SELECT name, state, finished FROM sys.snapshots ORDER BY finished DESC LIMIT 10" ;;
    *)
      local deploy; deploy=$(sidecar_spec "$s" | awk '{print $1}'); [ -n "$deploy" ] || deploy=$(kubectl -n "$ns" get deploy -o jsonpath='{.items[0].metadata.name}')
      kubectl -n "$ns" exec "deploy/$deploy" -- sh -c 'ls -la /backups/cron 2>/dev/null; du -sh /backups/cron 2>/dev/null || ls -la /backups' ;;
  esac
}

cmd_restore_drill() { (cd "$ROOT" && RDLAB_PLATFORM=k3s RDLAB_RESULTS_DIR="$ROOT/results-k3s" uv run --project harness rdlab phase "$1" backup "${@:2}"); }
cmd_logging_drill() { (cd "$ROOT" && RDLAB_PLATFORM=k3s RDLAB_RESULTS_DIR="$ROOT/results-k3s" uv run --project harness rdlab phase "$1" logging "${@:2}"); }
cmd_promote()       { (cd "$ROOT" && RDLAB_PLATFORM=k3s RDLAB_RESULTS_DIR="$ROOT/results-k3s" uv run --project harness rdlab phase "$1" replication --failover); }

cmd_logs_sidecar() {
  local s=$1 ns spec; ns=$(ns_of "$s"); spec=$(sidecar_spec "$s")
  [ -n "$spec" ] || { echo "no sidecar spec for $s (its logs are already on stdout, or not on a PVC: oracle diag dir, clickhouse console)"; return 1; }
  set -- $spec; local deploy=$1 pvc=$2 mount=$3; shift 3; local globs="$*"
  local img; img=$(harbor_ref busybox:1.37)
  local patch; patch=$(cat <<EOF
{"spec":{"template":{"spec":{"containers":[{"name":"logs","image":"$img","imagePullPolicy":"IfNotPresent",
 "command":["sh","-c","while :; do set -- $globs; if [ -f \"\$1\" ]; then echo \"== tailing \$*\"; tail -n +1 -F \"\$@\"; fi; sleep 5; done"],
 "volumeMounts":[{"name":"logs-sidecar","mountPath":"$mount"}]}],
 "volumes":[{"name":"logs-sidecar","persistentVolumeClaim":{"claimName":"$pvc"}}]}}}}
EOF
)
  kubectl -n "$ns" patch deployment "$deploy" --type strategic -p "$patch"
  kubectl -n "$ns" rollout status "deployment/$deploy" --timeout=300s
  echo "== last lines from the sidecar (kubectl -n $ns logs deploy/$deploy -c logs -f to follow):"
  sleep 8; kubectl -n "$ns" logs "deploy/$deploy" -c logs --tail=15
}

cmd_verify_backups() {
  local list="$*"; [ -n "$list" ] || list="$ADDON_STACKS $EXEC_STACKS"
  for s in $list; do
    if ! kubectl get ns "$(ns_of "$s")" >/dev/null 2>&1; then echo "VERDICT $s SKIP (not deployed)"; continue; fi
    local t0=$SECONDS
    if cmd_backup_now "$s" >"/tmp/rdlab-backup-$s.log" 2>&1; then echo "VERDICT $s PASS $((SECONDS-t0))s $(grep -cE 'done|Backup successful|CHECKPOINT' /tmp/rdlab-backup-$s.log) markers"
    else echo "VERDICT $s FAIL $((SECONDS-t0))s (see /tmp/rdlab-backup-$s.log)"; fi
  done
}

cmd=${1:-help}; shift || true
case "$cmd" in
  list) cmd_list ;;
  schedule) cmd_schedule "$@" ;;
  unschedule) cmd_unschedule "$@" ;;
  backup-now) cmd_backup_now "$@" ;;
  artifacts) cmd_artifacts "$@" ;;
  restore-drill) cmd_restore_drill "$@" ;;
  logging-drill) cmd_logging_drill "$@" ;;
  logs-sidecar) cmd_logs_sidecar "$@" ;;
  promote) cmd_promote "$@" ;;
  verify-backups) cmd_verify_backups "$@" ;;
  *) sed -n 2,22p "$0" ;;
esac
