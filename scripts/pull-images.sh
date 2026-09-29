#!/usr/bin/env bash
# Pull images while avoiding the Docker Hub anonymous rate limit (100 req/hour):
# Docker Hub images are fetched through Google's pull-through cache (mirror.gcr.io)
# and re-tagged under their canonical name so compose files stay unchanged.
# Usage: scripts/pull-images.sh image[:tag] ...      (or no args = all images referenced by stacks/*/compose.yaml)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ $# -eq 0 ]; then
  mapfile -t IMAGES < <(grep -rhoE '^\s*image:\s*\S+' "$ROOT"/stacks/*/compose.yaml | awk '{print $2}' | tr -d '"' | sort -u)
else
  IMAGES=("$@")
fi
ok=0; fail=0
for img in "${IMAGES[@]}"; do
  if docker image inspect "$img" >/dev/null 2>&1; then echo "have    $img"; ok=$((ok+1)); continue; fi
  case "$img" in
    mcr.microsoft.com/*|container-registry.oracle.com/*|icr.io/*|ghcr.io/*|quay.io/*|gcr.io/*|public.ecr.aws/*|registry.*|*.azurecr.io/*)
      src="$img" ;;
    */*/*) src="$img" ;;                               # already registry-qualified
    */*)   src="mirror.gcr.io/$img" ;;                 # user/repo on Docker Hub
    *)     src="mirror.gcr.io/library/$img" ;;         # official library image
  esac
  echo "pull    $img  (via $src)"
  if docker pull -q "$src" >/dev/null 2>&1; then
    [ "$src" != "$img" ] && docker tag "$src" "$img" && docker rmi "$src" >/dev/null 2>&1 || true
    ok=$((ok+1))
  else
    echo "  mirror failed, trying Docker Hub directly"
    if docker pull -q "$img" >/dev/null 2>&1; then ok=$((ok+1)); else echo "FAILED  $img"; fail=$((fail+1)); fi
  fi
done
echo "done: ok=$ok failed=$fail"
[ $fail -eq 0 ]
