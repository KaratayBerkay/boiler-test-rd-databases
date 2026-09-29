#!/usr/bin/env bash
# Mirror every image the stacks use into the Harbor project, and record the pushed digests in k8s/images.lock.json.
#
#   k8s/push-images.sh              push everything that is missing or changed (docker tag + docker push, 3 in parallel)
#   k8s/push-images.sh --dry-run    print the source -> harbor mapping and stop
#   k8s/push-images.sh mysql:9 …    only these source images
#
# The source list and the target names come from `k8s/gen.py images`, the same function the manifest generator
# uses, so what the pods pull is by construction what was pushed. Pushes go through 127.0.0.1:<port>: the Docker
# daemon treats loopback as an insecure registry, so the self-signed certificate needs no root-owned trust store.
# Authentication is the project-scoped `pusher` robot, logged in to an isolated docker config (k8s/.docker-robot)
# so the push can never silently use another identity from ~/.docker/config.json.
set -euo pipefail

K8S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$K8S")"
export FIREBIRD_CLIENT_LIB="${FIREBIRD_CLIENT_LIB:-/dev/null}"

[[ -f "$K8S/.env" ]] || { echo "!! $K8S/.env missing (run k8s/harbor/up.sh)" >&2; exit 1; }
set -a; . "$K8S/.env"; set +a
: "${HARBOR_HOST:?}" "${HARBOR_HTTPS_PORT:=8443}" "${HARBOR_PUSH_USER:?}" "${HARBOR_PUSH_PASSWORD:?}"
PUSH_REG="127.0.0.1:${HARBOR_HTTPS_PORT}"
REG="${HARBOR_HOST}:${HARBOR_HTTPS_PORT}"
export DOCKER_CONFIG="$K8S/.docker-robot"

DRY=false; ONLY=()
for a in "$@"; do case "$a" in --dry-run) DRY=true ;; *) ONLY+=("$a") ;; esac; done

mapfile -t PAIRS < <(cd "$ROOT" && uv run --project harness python k8s/gen.py images 2>/dev/null)
if [[ ${#ONLY[@]} -gt 0 ]]; then
  mapfile -t PAIRS < <(printf '%s\n' "${PAIRS[@]}" | grep -E "^($(IFS='|'; echo "${ONLY[*]}")) ")
fi
echo "==> ${#PAIRS[@]} images -> $REG (pushing via $PUSH_REG as $HARBOR_PUSH_USER)"
if $DRY; then printf '   %s\n' "${PAIRS[@]}"; exit 0; fi

# the two images that are not pulled from a public registry by the compose stacks
docker image inspect rdlab/h2:2.3.232 >/dev/null 2>&1 || { echo "==> building rdlab/h2"; docker compose -f "$ROOT/stacks/h2/compose.yaml" build -q; }
docker image inspect busybox:1.37 >/dev/null 2>&1 || "$ROOT/scripts/pull-images.sh" busybox:1.37

mkdir -p "$DOCKER_CONFIG"; chmod 700 "$DOCKER_CONFIG"
printf '%s' "$HARBOR_PUSH_PASSWORD" | docker login "$PUSH_REG" -u "$HARBOR_PUSH_USER" --password-stdin >/dev/null

# crane (static binary, no root) for the registry-to-registry fallback: Docker's containerd image store
# occasionally refuses to push an image it pulled fine ("does not provide any platform"); copying the same tag
# from its public source straight into Harbor sidesteps the local store entirely.
export PATH="$HOME/.local/bin:$PATH"
command -v crane >/dev/null || {
  echo "==> installing crane into ~/.local/bin"
  tmp=$(mktemp -d); gh release download --repo google/go-containerregistry --pattern 'go-containerregistry_Linux_x86_64.tar.gz' --dir "$tmp" >/dev/null
  tar xzf "$tmp"/go-containerregistry_Linux_x86_64.tar.gz -C "$tmp" crane && install -m 0755 "$tmp/crane" "$HOME/.local/bin/crane"; rm -rf "$tmp"
}

upstream() {                                     # where a compose image name is pulled from (same rules as scripts/pull-images.sh)
  case "$1" in
    */*/*|mcr.microsoft.com/*|icr.io/*|ghcr.io/*|quay.io/*|gcr.io/*|public.ecr.aws/*) echo "$1" ;;
    */*) echo "mirror.gcr.io/$1" ;;
    *)   echo "mirror.gcr.io/library/$1" ;;
  esac
}
push_one() {                                     # "<src> <dst>" -> tag + push (docker), else crane copy from upstream
  local src="$1" dst="$2" pushref
  pushref="${PUSH_REG}/${dst#*/}"                # swap the registry host for the loopback address
  docker image inspect "$src" >/dev/null 2>&1 || { echo "   MISSING  $src (scripts/pull-images.sh $src)"; return 1; }
  docker tag "$src" "$pushref"
  if out=$(docker push -q "$pushref" 2>&1); then
    echo "   pushed   $dst"
  elif [[ "$src" != rdlab/* ]] && out2=$(crane copy --insecure "$(upstream "$src")" "$pushref" 2>&1); then
    echo "   copied   $dst (crane, from $(upstream "$src"); docker push said: ${out: -90})"
  else
    echo "   FAILED   $src: ${out: -200} ${out2: -200}"; return 1
  fi
}
export -f push_one upstream; export PUSH_REG
printf '%s\n' "${PAIRS[@]}" | xargs -P 3 -L 1 bash -c 'push_one "$0" "$1"' | tee "$K8S/.push.log" || true
FAILED=$(grep -c -E '^\s+(FAILED|MISSING)' "$K8S/.push.log" || true)

echo "==> digests -> k8s/images.lock.json"
python3 - "$REG" "$PUSH_REG" "${PAIRS[@]}" <<'PY'
import json, subprocess, sys, datetime
reg, push_reg, pairs = sys.argv[1], sys.argv[2], sys.argv[3:]
lock = {"_comment": "source image -> what the cluster pulls (digest recorded after push by k8s/push-images.sh)",
        "registry": reg, "pushed": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "images": {}}
for line in pairs:
    src, dst = line.split()
    pushref = push_reg + "/" + dst.split("/", 1)[1]
    p = subprocess.run(["docker", "image", "inspect", pushref, "--format", "{{json .RepoDigests}}"], capture_output=True, text=True)
    digests = [d.split("@")[1] for d in (json.loads(p.stdout) if p.returncode == 0 else []) if d.startswith(push_reg + "/")]
    if not digests:                      # pushed by crane: the daemon knows nothing, the registry does
        q = subprocess.run(["crane", "digest", "--insecure", pushref], capture_output=True, text=True)
        digests = [q.stdout.strip()] if q.returncode == 0 and q.stdout.strip() else []
    lock["images"][src] = {"harbor": dst, "digest": digests[0] if digests else None}
open("k8s/images.lock.json", "w").write(json.dumps(lock, indent=1) + "\n")
missing = [s for s, v in lock["images"].items() if not v["digest"]]
print(f"   {len(lock['images']) - len(missing)} digests recorded from the local store" + (f"; resolved from the registry: {missing}" if missing else ""))
PY
[[ "$FAILED" == "0" ]] || { echo "!! $FAILED image(s) failed, see $K8S/.push.log" >&2; exit 1; }
