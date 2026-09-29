#!/usr/bin/env bash
# Install and start a local Harbor registry (official online installer, docker compose) for the lab.
#
#   k8s/harbor/up.sh            download the installer once, generate a CA + server cert, render harbor.yml,
#                               `prepare`, `docker compose up -d`, wait for /api/v2.0/health, create the
#                               project + robot accounts (harbor/setup.py) and record their secrets in k8s/.env
#   k8s/harbor/up.sh --down     stop Harbor (keeps data/ and certs/); `--purge` also deletes data/
#
# Design notes (why it looks like this):
#   - Harbor's hostname is the host's LAN IP (HARBOR_HOST): the docker client on the host and the k3s nodes
#     inside k3d must both reach the *same* address, because Harbor puts it into the token realm of every
#     401 challenge. 127.0.0.1 would make every k3s node ask itself for the token.
#   - No root anywhere: pushes from the host use 127.0.0.1:<port>, which Docker treats as an insecure registry
#     by default (the realm at HARBOR_HOST is fetched through the same skip-verify transport); k3s nodes get
#     the CA file mounted by k3d (k8s/k3d.yaml) and pull with a read-only robot account (registries.yaml).
#   - Everything lives under k8s/harbor/{certs,data,logs}, all gitignored; the installer tarball under dist/.
#   - Harbor's generic container names (nginx, redis, registry, registryctl) are prefixed with harbor- so they
#     cannot collide with other labs on the same Docker host.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S="$(dirname "$HERE")"
VERSION="${HARBOR_VERSION:-v2.15.2}"
DIST="$HERE/dist/harbor"
PROJECT_NAME="harbor"                       # docker compose project name

[[ -f "$K8S/.env" ]] || { echo "!! $K8S/.env missing: cp k8s/.env.example k8s/.env && chmod 600 k8s/.env" >&2; exit 1; }
set -a; . "$K8S/.env"; set +a
: "${HARBOR_HOST:?}" "${HARBOR_HTTP_PORT:=8880}" "${HARBOR_HTTPS_PORT:=8443}" "${HARBOR_ADMIN_PASSWORD:?}" "${HARBOR_DB_PASSWORD:?}"

compose() { docker compose -p "$PROJECT_NAME" -f "$DIST/docker-compose.yml" "$@"; }

case "${1:-}" in
  --down)  compose down; exit 0 ;;
  --purge) compose down -v; rm -rf "$HERE/data" "$HERE/logs"; echo "harbor stopped, data removed"; exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--down|--purge]" >&2; exit 2 ;;
esac

# 1. installer ----------------------------------------------------------------------------------------------
if [[ ! -x "$DIST/prepare" ]]; then
  echo "==> downloading harbor online installer $VERSION"
  mkdir -p "$HERE/dist"
  gh release download "$VERSION" --repo goharbor/harbor --pattern "harbor-online-installer-${VERSION}.tgz" --dir "$HERE/dist" --clobber
  tar xzf "$HERE/dist/harbor-online-installer-${VERSION}.tgz" -C "$HERE/dist"
fi

# 2. certificates (CA + server cert with every name/IP the registry is reached by) --------------------------
CERTS="$HERE/certs"
if [[ ! -f "$CERTS/harbor.crt" ]]; then
  echo "==> generating CA and server certificate in $CERTS"
  mkdir -p "$CERTS"; chmod 700 "$CERTS"
  openssl req -x509 -new -nodes -sha256 -days 3650 -newkey rsa:4096 -subj "/CN=rdlab harbor CA" \
    -keyout "$CERTS/ca.key" -out "$CERTS/ca.crt" >/dev/null 2>&1
  cat > "$CERTS/san.cnf" <<EOF
[req]
distinguished_name = dn
req_extensions = ext
prompt = no
[dn]
CN = $HARBOR_HOST
[ext]
subjectAltName = @alt
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
[alt]
IP.1 = $HARBOR_HOST
IP.2 = 127.0.0.1
DNS.1 = harbor.rdlab.local
DNS.2 = localhost
DNS.3 = host.k3d.internal
EOF
  openssl req -new -nodes -newkey rsa:4096 -keyout "$CERTS/harbor.key" -out "$CERTS/harbor.csr" -config "$CERTS/san.cnf" >/dev/null 2>&1
  openssl x509 -req -sha256 -days 3650 -in "$CERTS/harbor.csr" -CA "$CERTS/ca.crt" -CAkey "$CERTS/ca.key" -CAcreateserial \
    -out "$CERTS/harbor.crt" -extensions ext -extfile "$CERTS/san.cnf" >/dev/null 2>&1
  chmod 600 "$CERTS"/*.key
fi

# 3. harbor.yml from the shipped template (only the keys we care about are changed) --------------------------
mkdir -p "$HERE/data" "$HERE/logs"
HARBOR_DIR="$HERE" python3 - "$DIST/harbor.yml.tmpl" "$DIST/harbor.yml" <<'PY'
import os, sys, re
src, dst = sys.argv[1], sys.argv[2]
t = open(src).read()
here = os.environ["HARBOR_DIR"]
certs = os.path.join(here, "certs")
def sub(pattern, repl):
    global t
    t, n = re.subn(pattern, repl, t, count=1, flags=re.M)
    assert n == 1, pattern
sub(r"^hostname: .*$", f"hostname: {os.environ['HARBOR_HOST']}")
sub(r"^http:\n((?:\s*#.*\n)*)  port: \d+", lambda m: f"http:\n{m.group(1)}  port: {os.environ['HARBOR_HTTP_PORT']}")
sub(r"^https:\n((?:\s*#.*\n)*)  port: \d+", lambda m: f"https:\n{m.group(1)}  port: {os.environ['HARBOR_HTTPS_PORT']}")
sub(r"^  certificate: .*$", f"  certificate: {certs}/harbor.crt")
sub(r"^  private_key: .*$", f"  private_key: {certs}/harbor.key")
sub(r"^harbor_admin_password: .*$", f"harbor_admin_password: {os.environ['HARBOR_ADMIN_PASSWORD']}")
sub(r"^database:\n((?:\s*#.*\n)*)  password: .*$", lambda m: f"database:\n{m.group(1)}  password: {os.environ['HARBOR_DB_PASSWORD']}")
sub(r"^data_volume: .*$", f"data_volume: {os.path.join(here, 'data')}")
sub(r"^    location: /var/log/harbor$", f"    location: {os.path.join(here, 'logs')}")
open(dst, "w").write(t)
print(f"==> rendered {dst} (hostname {os.environ['HARBOR_HOST']}, https {os.environ['HARBOR_HTTPS_PORT']}, data {os.path.join(here, 'data')})")
PY

# 4. prepare + up ------------------------------------------------------------------------------------------
echo "==> prepare (generates $DIST/common/config and docker-compose.yml)"
( cd "$DIST" && HARBOR_BUNDLE_DIR="$DIST" ./prepare >/dev/null )
# prepare runs as root inside its container and leaves the */env files root-owned 0640: they are read by
# `docker compose` on the host, so hand exactly those (and the compose file) to this user through a container
# (no sudo). Everything else stays uid 10000, the user the Harbor processes run as.
docker run --rm --entrypoint sh -v "$DIST:/c" goharbor/prepare:${VERSION} -c \
  "chown $(id -u):$(id -g) /c/docker-compose.yml /c/common/config/*/env" >/dev/null
# prefix the generic container names so they cannot clash with other stacks on this host
sed -i -E 's/^(\s*container_name:\s*)(nginx|redis|registry|registryctl)\s*$/\1harbor-\2/' "$DIST/docker-compose.yml"
echo "==> docker compose up"
compose up -d >/dev/null

# 5. wait for health ----------------------------------------------------------------------------------------
echo -n "==> waiting for harbor"
for i in $(seq 1 90); do
  if curl -fsk "https://127.0.0.1:${HARBOR_HTTPS_PORT}/api/v2.0/health" 2>/dev/null | grep -q '"status":"healthy"'; then echo " healthy"; break; fi
  echo -n "."; sleep 2
  [[ $i -eq 90 ]] && { echo; echo "!! harbor not healthy; docker compose -p harbor -f $DIST/docker-compose.yml logs" >&2; exit 1; }
done

# 6. project + robot accounts ---------------------------------------------------------------------------------
python3 "$HERE/setup.py"

echo
echo "  UI        https://${HARBOR_HOST}:${HARBOR_HTTPS_PORT}   (admin / see k8s/.env; self-signed: trust k8s/harbor/certs/ca.crt)"
echo "  push      k8s/push-images.sh          (docker login 127.0.0.1:${HARBOR_HTTPS_PORT} as the pusher robot, isolated docker config)"
echo "  cluster   k8s/up.sh                   (k3d cluster that pulls from ${HARBOR_HOST}:${HARBOR_HTTPS_PORT} with the k3s robot)"
