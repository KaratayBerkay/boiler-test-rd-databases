#!/usr/bin/env bash
# Register 5 worker primaries and their streaming replicas (as Citus secondary nodes) with the coordinator.
set -euo pipefail
Q() { psql -v ON_ERROR_STOP=1 -h coordinator -U lab -d lab -tAc "$1"; }
for i in $(seq 1 60); do Q "SELECT 1" >/dev/null 2>&1 && break; sleep 2; done
Q "SELECT citus_set_coordinator_host('coordinator', 5432);"
for i in 1 2 3 4 5; do
  Q "SELECT citus_add_node('w$i', 5432);" || Q "SELECT nodeid FROM pg_dist_node WHERE nodename = 'w$i'"
done
for i in 1 2 3 4 5; do
  Q "SELECT citus_add_secondary_node('r$i', 5432, 'w$i', 5432);" || true
done
Q "SELECT citus_version();"
psql -h coordinator -U lab -d lab -c "SELECT nodeid, nodename, nodeport, noderole, isactive, shouldhaveshards FROM pg_dist_node ORDER BY nodeid;"
