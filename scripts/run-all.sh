#!/usr/bin/env bash
# Run the full harness for a list of stacks sequentially (one engine at a time keeps the numbers honest).
# Usage: scripts/run-all.sh [--failover] stack1 stack2 ...      logs -> results/logs/<stack>.log
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FO=""
if [ "${1:-}" = "--failover" ]; then FO="--failover"; shift; fi
mkdir -p "$ROOT/results/logs"
export FIREBIRD_CLIENT_LIB="${FIREBIRD_CLIENT_LIB:-}"
cd "$ROOT/harness"
for s in "$@"; do
  echo "=== $(date +%H:%M:%S) $s ==="
  timeout 5400 uv run rdlab run "$s" $FO > "$ROOT/results/logs/$s.log" 2>&1
  echo "exit=$? $(grep -cE 'FAILED|ERR' "$ROOT/results/logs/$s.log") failures/errors"
  docker compose -f "$ROOT/stacks/$s/compose.yaml" -p "rdlab-$s" down -v -t 10 >/dev/null 2>&1 || true
done
uv run rdlab report >/dev/null 2>&1 && echo "report rebuilt"
