#!/usr/bin/env bash
# Run the backup + logging drills (with a fresh load) for a list of stacks, one at a time.
# Usage: scripts/run-drills.sh stack1 stack2 ...        logs -> results/logs/<stack>/<run_id>.log (+ .jsonl)
# Firebird needs FIREBIRD_CLIENT_LIB (and LD_LIBRARY_PATH for libtommath/libtomcrypt next to it).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/harness"
for s in "$@"; do
  echo "=== $s $(date +%H:%M:%S)"
  timeout 5400 uv run rdlab run "$s" --phases load,backup,logging > "$ROOT/results/logs/drill-$s.out" 2>&1
  echo "exit=$? $(grep -cE 'FAILED|ERR ' "$ROOT/results/logs/drill-$s.out") failures/errors"
done
