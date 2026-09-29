#!/usr/bin/env python3
"""compose -> Kubernetes generator for the rd-databases lab (k3s/k3d + Harbor).

The compose files under stacks/<engine>/ stay the single source of truth. This script reads them through
`docker compose config` (anchors, env and units resolved) and writes, for every stack, a kustomize directory
stacks/<engine>/k8s/ that recreates the same topology in one namespace:

  compose service (long-running)      -> Deployment (1 replica, Recreate) + headless Service (DNS name = service
                                         name, pod IP, published even when not ready: cluster bootstrap traffic
                                         must flow like on a compose network) + NodePort Service for every published
                                         port (host port -> fixed nodePort, see ports.json; k3d maps host:nodePort)
  compose one-shot (restart: "no")    -> initContainer of every service that depends on it when it only prepares a
                                         shared volume (backup-init/volumes-init), otherwise a Job with wait
                                         initContainers (setup scripts that need the databases up)
  named volume                        -> PersistentVolumeClaim of the same name (RWO; the lab cluster is one node)
  bind-mounted file / directory       -> ConfigMap (subPath mount for files, whole dir otherwise)
  healthcheck                         -> readinessProbe + startupProbe (no livenessProbe: the failover scenario
                                         stops a primary on purpose and must not be "healed" by the kubelet)
  depends_on service_healthy          -> initContainer `wait-for` (busybox nc on the dependency's first port)
  deploy.resources.limits / shm_size  -> resources.limits / emptyDir(Memory) on /dev/shm
  privileged / cap_add / user: root   -> securityContext
  container_name                      -> label rdlab.io/container (what the harness uses to find the pod), plus a
                                         second headless Service so the compose container name also resolves
  image                               -> <HARBOR_HOST:PORT>/<project>/<path>:<tag>  (push-images.sh mirrors them)
  x-k8s: {port: N}                    -> compose extension (ignored by compose) naming the port other services wait
                                         for when a service publishes none (e.g. ClickHouse Keeper 9181)
  x-k8s: {seed_from_image: {IMG_PATH: VOL}}  -> initContainer copies the image's IMG_PATH into the PVC when empty. A
                                         Docker named volume is seeded from the image on first use; a k8s PVC starts empty
                                         and masks image content (Oracle faststart bakes its DB into /opt/oracle/oradata)
  x-k8s: {shm_reset: true}            -> clear /dev/shm before the entrypoint each start, so a pod restart does not find a
                                         stale SGA and fail (Oracle ORA-01081). Reads the image ENTRYPOINT/CMD at gen time
  x-k8s: {startup_seconds: N}         -> floor for the startupProbe budget (slow first-boot DB creation)

Commands:
  gen.py images               print "<source image> <harbor image>" for every image of every stack
  gen.py ports                (re)write k8s/ports.json: host port -> nodePort for every published/restore port
  gen.py k3d                  write k8s/k3d.yaml (cluster config: port mappings, CA, kubelet args) + k8s/registries.yaml (mirror + robot auth)
  gen.py manifests [KEY...]   write stacks/<key>/k8s/ for the given stacks (default: all)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml


class Dumper(yaml.SafeDumper):
    """No anchors/aliases: shared dicts (labels, probes) are written out in full so the manifests read plainly."""

    def ignore_aliases(self, data):
        return True


ROOT = Path(__file__).resolve().parents[1]
STACKS = ROOT / "stacks"
K8S = ROOT / "k8s"
PORTS_JSON = K8S / "ports.json"
NODEPORT_BASE = 30000
WAIT_IMAGE = "busybox:1.37"           # pushed to Harbor by push-images.sh as well
PVC_SIZE = "20Gi"                     # local-path provisioner does not enforce it; documents the expectation


# ----------------------------------------------------------------------------------------------- helpers
def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    f = K8S / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip("'\"")
    return env


ENV = load_env()


def registry() -> str:
    host, port = ENV.get("HARBOR_HOST"), ENV.get("HARBOR_HTTPS_PORT", "8443")
    if not host:
        sys.exit("!! k8s/.env has no HARBOR_HOST (run k8s/harbor/up.sh first)")
    return f"{host}:{port}"


def project() -> str:
    return ENV.get("HARBOR_PROJECT", "rdlab")


def image_path(src: str) -> str:
    """Path of a source image inside the Harbor project: registry host dropped, `rdlab/` namespace folded."""
    name = src
    first = name.split("/")[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        name = name.split("/", 1)[1]
    if name.startswith(f"{project()}/"):
        name = name[len(project()) + 1:]
    if ":" not in name.rsplit("/", 1)[-1]:
        name += ":latest"
    return name


def harbor_ref(src: str) -> str:
    return f"{registry()}/{project()}/{image_path(src)}"


_ENTRYPOINT_CACHE: dict[str, list[str]] = {}


def image_entrypoint(image: str) -> list[str]:
    """The image's ENTRYPOINT + CMD (for wrapping the container command, e.g. shm_reset). Cached."""
    if image not in _ENTRYPOINT_CACHE:
        parts: list[str] = []
        for field in ("Entrypoint", "Cmd"):
            r = subprocess.run(["docker", "image", "inspect", image, "--format", f"{{{{json .Config.{field}}}}}"], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip() and r.stdout.strip() != "null":
                parts += json.loads(r.stdout)
        _ENTRYPOINT_CACHE[image] = parts
    return _ENTRYPOINT_CACHE[image]


def list_stacks() -> list[str]:
    return sorted(p.name for p in STACKS.iterdir() if (p / "lab.yaml").exists() and (p / "compose.yaml").exists())


def compose_config(key: str) -> dict:
    p = subprocess.run(["docker", "compose", "-f", str(STACKS / key / "compose.yaml"), "config", "--format", "json"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"!! docker compose config failed for {key}: {p.stderr[-500:]}")
    return json.loads(p.stdout)


def lab_config(key: str) -> dict:
    return yaml.safe_load((STACKS / key / "lab.yaml").read_text()) or {}


def seconds(d) -> int:
    """compose duration ('3s', '1m30s', '500ms', '2m0s') -> whole seconds (>= 1)."""
    if d is None:
        return 0
    if isinstance(d, (int, float)):
        return max(1, int(d))
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|s|m|h)", str(d)):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return max(1, int(round(total)))


def cpu_quantity(cpus) -> str:
    return f"{int(round(float(cpus) * 1000))}m"


def mem_quantity(b) -> str:
    b = int(b)
    return f"{b // (1024 * 1024)}Mi" if b % (1024 * 1024) == 0 else f"{b}"


def unescape(s: str) -> str:
    """compose keeps `$$` (literal $) in `config` output; the runtime would have unescaped it."""
    return s.replace("$$", "$")


def dns_label(s: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return s[:63]


def cm_key(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path)


# ----------------------------------------------------------------------------------------------- images / ports
def all_images() -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for key in list_stacks():
        for svc in compose_config(key)["services"].values():
            img = svc.get("image")
            if img and img not in seen:
                seen[img] = harbor_ref(img)
    seen.setdefault(WAIT_IMAGE, harbor_ref(WAIT_IMAGE))
    return sorted(seen.items())


def collect_host_ports() -> dict[str, list[str]]:
    """host port -> ["stack/service", ...] for every published port and every backup restore_port."""
    out: dict[str, set[str]] = {}
    for key in list_stacks():
        cfg = compose_config(key)
        for name, svc in cfg["services"].items():
            for p in svc.get("ports") or []:
                if p.get("published"):
                    out.setdefault(str(p["published"]), set()).add(f"{key}/{name}")
        rp = (lab_config(key).get("backup") or {}).get("restore_port")
        if rp:
            out.setdefault(str(rp), set()).add(f"{key}/restore")
    return {k: sorted(v) for k, v in out.items()}


def port_table() -> dict[str, int]:
    """host port -> nodePort. Stable: existing assignments in ports.json are kept, new ports appended."""
    existing: dict[str, int] = {}
    if PORTS_JSON.exists():
        existing = {k: v["nodePort"] for k, v in json.loads(PORTS_JSON.read_text())["ports"].items()}
    used = set(existing.values())
    nxt = NODEPORT_BASE
    table = dict(existing)
    for hp in sorted(collect_host_ports(), key=int):
        if hp in table:
            continue
        while nxt in used:
            nxt += 1
        table[hp] = nxt
        used.add(nxt)
        nxt += 1
    return table


def write_ports() -> dict[str, int]:
    table = port_table()
    users = collect_host_ports()
    PORTS_JSON.write_text(json.dumps({
        "_comment": "host port (as in stacks/*/compose.yaml and lab.yaml targets) -> nodePort. Generated by k8s/gen.py ports; "
                    "k3d.yaml publishes every nodePort on 127.0.0.1:<nodePort>; the harness translates lab.yaml ports through this table when RDLAB_PLATFORM=k3s. Recreate the cluster when it changes.",
        "ports": {hp: {"nodePort": np, "used_by": users.get(hp, [])} for hp, np in sorted(table.items(), key=lambda kv: int(kv[0]))},
    }, indent=1) + "\n")
    return table


# ----------------------------------------------------------------------------------------------- k3d config
def write_k3d() -> Path:
    table = write_ports()
    cluster = ENV.get("CLUSTER", "rdlab")
    reg = registry()
    ca = K8S / "harbor" / "certs" / "ca.crt"
    registries = {
        # docker.io through Google's pull-through cache: k3s fetches its own system images (coredns, local-path,
        # metrics-server, pause) from Docker Hub at first start, and anonymous Hub pulls are rate-limited
        "mirrors": {reg: {"endpoint": [f"https://{reg}"]}, "docker.io": {"endpoint": ["https://mirror.gcr.io"]}},
        "configs": {reg: {"auth": {"username": ENV.get("HARBOR_PULL_USER", ""), "password": ENV.get("HARBOR_PULL_PASSWORD", "")},
                          "tls": {"ca_file": "/etc/ssl/certs/harbor-ca.crt"}}},
    }
    (K8S / "registries.yaml").write_text(yaml.safe_dump(registries, sort_keys=False))
    doc = {
        "apiVersion": "k3d.io/v1alpha5",
        "kind": "Simple",
        "metadata": {"name": cluster},
        "servers": 1,
        "agents": 0,
        "image": ENV.get("K3S_IMAGE", "rancher/k3s:v1.35.5-k3s1"),
        # published as 127.0.0.1:<nodePort> (not as the compose host port: a running compose stack would clash); the
        # harness translates lab.yaml ports through ports.json when RDLAB_PLATFORM=k3s
        "ports": [{"port": f"127.0.0.1:{np}:{np}", "nodeFilters": ["server:0:direct"]} for np in sorted(set(table.values()))],
        "volumes": [{"volume": f"{ca}:/etc/ssl/certs/harbor-ca.crt", "nodeFilters": ["all"]}],
        # registries.yaml is passed separately (`--registry-config`): k3d expands $VAR in its config file and would
        # eat the `$` in the robot account name (robot$rdlab+k3s)
        "options": {
            "k3d": {"wait": True, "timeout": "300s", "disableLoadbalancer": True},
            "k3s": {"extraArgs": [
                {"arg": "--disable=traefik", "nodeFilters": ["server:*"]},
                {"arg": "--kubelet-arg=container-log-max-size=50Mi", "nodeFilters": ["all"]},
                {"arg": "--kubelet-arg=container-log-max-files=3", "nodeFilters": ["all"]},
                {"arg": f"--kube-apiserver-arg=service-node-port-range={NODEPORT_BASE}-32767", "nodeFilters": ["server:*"]},
            ]},
            "kubeconfig": {"updateDefaultKubeconfig": True, "switchCurrentContext": False},
        },
    }
    out = K8S / "k3d.yaml"
    out.write_text("# generated by k8s/gen.py k3d from k8s/.env + k8s/ports.json -- do not edit, contains the pull secret\n"
                   + yaml.safe_dump(doc, sort_keys=False, width=200))
    return out


# ----------------------------------------------------------------------------------------------- manifests
class Stack:
    def __init__(self, key: str, table: dict[str, int]):
        self.key = key
        self.cfg = compose_config(key)
        self.lab = lab_config(key)
        self.ns = self.cfg.get("name") or self.lab.get("project") or f"rdlab-{key}"
        self.services: dict[str, dict] = self.cfg["services"]
        # `docker compose config` drops x-* extension fields: read them from the raw file (anchors resolved by PyYAML)
        raw = yaml.safe_load((STACKS / key / "compose.yaml").read_text()) or {}
        self.hints: dict[str, dict] = {n: (v or {}).get("x-k8s") or {} for n, v in (raw.get("services") or {}).items()}
        self.table = table
        self.oneshot = set(self.lab.get("oneshot") or []) | {n for n, s in self.services.items() if s.get("restart") == "no"}
        self.configmaps: dict[str, dict] = {}      # name -> manifest
        self.warnings: list[str] = []

    # --- classification -------------------------------------------------------------------
    def injectable(self, name: str) -> bool:
        """A one-shot with no dependencies of its own only prepares volumes: run it as an initContainer of its dependents."""
        return name in self.oneshot and not (self.services[name].get("depends_on") or {})

    def dependents_of(self, name: str) -> list[str]:
        return [n for n, s in self.services.items() if name in (s.get("depends_on") or {})]

    def first_port(self, name: str) -> int | None:
        svc = self.services[name]
        if self.hints.get(name, {}).get("port"):           # x-k8s extension field: the port to wait for / expose
            return int(self.hints[name]["port"])
        for p in svc.get("ports") or []:
            return int(p["target"])
        m = re.search(r"-p\s*(\d+)|:(\d{4,5})\b|port\s+(\d+)", " ".join((svc.get("healthcheck") or {}).get("test") or []))
        if m:
            return int(next(g for g in m.groups() if g))
        return None

    def wait_deps(self, name: str) -> list[tuple[str, int]]:
        """(service, port) pairs a pod must wait for, resolving Jobs to their own dependencies."""
        out: list[tuple[str, int]] = []
        for dep, cond in (self.services[name].get("depends_on") or {}).items():
            c = cond.get("condition")
            if self.injectable(dep) or c == "service_started":
                continue
            if dep in self.oneshot:                       # a Job: wait for what it waited for
                out += self.wait_deps(dep)
                continue
            port = self.first_port(dep)
            if port:
                out.append((dep, port))
            else:
                self.warnings.append(f"{name}: cannot derive a port to wait for {dep}")
        seen: set = set()
        return [x for x in out if not (x in seen or seen.add(x))]

    # --- pieces ---------------------------------------------------------------------------------
    def container(self, name: str, svc: dict) -> tuple[dict, list[dict]]:
        c: dict = {"name": dns_label(name), "image": harbor_ref(svc["image"]), "imagePullPolicy": "IfNotPresent"}
        if svc.get("entrypoint"):
            ep = svc["entrypoint"] if isinstance(svc["entrypoint"], list) else shlex.split(svc["entrypoint"])
            c["command"] = [unescape(x) for x in ep]
        if svc.get("command"):
            cmd = svc["command"] if isinstance(svc["command"], list) else shlex.split(svc["command"])
            c["args"] = [unescape(x) for x in cmd]
        hints = self.hints.get(name, {})
        if hints.get("shm_reset"):
            base = (c.get("command") or []) + (c.get("args") or [])
            if not base:
                base = [unescape(x) for x in image_entrypoint(svc["image"])]
            joined = " ".join(shlex.quote(x) for x in base)
            c["command"] = ["sh", "-c", f"rm -rf /dev/shm/* 2>/dev/null || true; exec {joined}"]
            c.pop("args", None)
        env = svc.get("environment") or {}
        if env:
            c["env"] = [{"name": k, "value": unescape(str(v))} for k, v in sorted(env.items()) if v is not None]
        ports = sorted({int(p["target"]) for p in (svc.get("ports") or [])})
        if ports:
            c["ports"] = [{"containerPort": p} for p in ports]
        mounts, volumes = self.volumes(name, svc)
        if svc.get("shm_size"):
            mounts.append({"name": "dshm", "mountPath": "/dev/shm"})
            volumes.append({"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": mem_quantity(svc["shm_size"])}})
        if mounts:
            c["volumeMounts"] = mounts
        hc = svc.get("healthcheck") or {}
        if hc.get("test") and hc["test"][0] != "NONE":
            probe = self.probe(hc)
            interval, retries = seconds(hc.get("interval", "30s")), int(hc.get("retries", 3))
            c["readinessProbe"] = {**probe, "periodSeconds": interval, "timeoutSeconds": seconds(hc.get("timeout", "30s")), "failureThreshold": 3}
            # startupProbe failure kills the pod; be generous so a slow first boot is never interrupted mid-init
            startup_s = int(self.hints.get(name, {}).get("startup_seconds", 0))
            budget = max(retries + (seconds(hc.get("start_period", 0)) // interval), 120, startup_s // interval)
            c["startupProbe"] = {**probe, "periodSeconds": interval, "timeoutSeconds": seconds(hc.get("timeout", "30s")),
                                 "failureThreshold": budget}
        lim = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        if lim:
            r: dict = {}
            if lim.get("cpus") is not None:
                r["cpu"] = cpu_quantity(lim["cpus"])
            if lim.get("memory") is not None:
                r["memory"] = mem_quantity(lim["memory"])
            c["resources"] = {"limits": r}
        sc: dict = {}
        if svc.get("privileged"):
            sc["privileged"] = True
        if svc.get("cap_add"):
            sc["capabilities"] = {"add": list(svc["cap_add"])}
        if svc.get("user") in ("root", "0"):
            sc["runAsUser"] = 0
        elif svc.get("user") and str(svc["user"]).isdigit():
            sc["runAsUser"] = int(svc["user"])
        elif svc.get("user"):
            self.warnings.append(f"{name}: user '{svc['user']}' is not numeric; not mapped")
        if sc:
            c["securityContext"] = sc
        if svc.get("ulimits"):
            self.warnings.append(f"{name}: ulimits {list(svc['ulimits'])} not representable in Kubernetes (node defaults apply)")
        return c, volumes

    @staticmethod
    def probe(hc: dict) -> dict:
        t = hc["test"]
        if t[0] == "CMD-SHELL":
            return {"exec": {"command": ["sh", "-c", unescape(t[1])]}}
        if t[0] == "CMD":
            return {"exec": {"command": [unescape(x) for x in t[1:]]}}
        return {"exec": {"command": ["sh", "-c", unescape(" ".join(t))]}}

    def volumes(self, name: str, svc: dict) -> tuple[list[dict], list[dict]]:
        mounts: list[dict] = []
        vols: list[dict] = []
        seen: set[str] = set()
        for v in svc.get("volumes") or []:
            if v["type"] == "volume":
                vn = dns_label(v["source"])
                mounts.append({"name": vn, "mountPath": v["target"]})
                if vn not in seen:
                    vols.append({"name": vn, "persistentVolumeClaim": {"claimName": vn}})
                    seen.add(vn)
            elif v["type"] == "bind":
                src = Path(v["source"])
                cm = self.configmap_for(src)
                if cm is None:
                    continue
                vn = cm["metadata"]["name"]
                if src.is_file():
                    mounts.append({"name": vn, "mountPath": v["target"], "subPath": cm_key(src.name), "readOnly": bool(v.get("read_only"))})
                else:
                    mounts.append({"name": vn, "mountPath": v["target"], "readOnly": bool(v.get("read_only"))})
                if vn not in seen:
                    vols.append({"name": vn, "configMap": {"name": vn, "defaultMode": 0o755}})
                    seen.add(vn)
            elif v["type"] == "tmpfs":
                vn = dns_label("tmpfs-" + v["target"])
                mounts.append({"name": vn, "mountPath": v["target"]})
                vols.append({"name": vn, "emptyDir": {"medium": "Memory"}})
            else:
                self.warnings.append(f"{name}: volume type {v['type']} not mapped")
        return mounts, vols

    def configmap_for(self, src: Path) -> dict | None:
        try:
            rel = src.relative_to(STACKS / self.key)
        except ValueError:
            self.warnings.append(f"bind mount outside the stack directory not mapped: {src}")
            return None
        name = dns_label("cm-" + str(rel).replace("/", "-").replace(".", "-"))
        if name in self.configmaps:
            return self.configmaps[name]
        data: dict[str, str] = {}
        files = [src] if src.is_file() else sorted(p for p in src.iterdir() if p.is_file())
        for f in files:
            try:
                data[cm_key(f.name)] = f.read_text()
            except UnicodeDecodeError:
                self.warnings.append(f"binary file skipped in ConfigMap {name}: {f}")
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": self.ns,
                                                                   "annotations": {"rdlab.io/source": str(rel)}}, "data": data}
        self.configmaps[name] = cm
        return cm

    def wait_init_containers(self, name: str) -> list[dict]:
        out = []
        for dep, port in self.wait_deps(name):
            out.append({"name": dns_label(f"wait-{dep}"), "image": harbor_ref(WAIT_IMAGE),
                        "command": ["sh", "-c", f"until nc -z -w 2 {dep} {port}; do echo waiting for {dep}:{port}; sleep 2; done"]})
        return out

    def seed_init_containers(self, name: str) -> tuple[list[dict], list[dict]]:
        """Copy image content into an empty PVC (Docker seeds named volumes from the image; a PVC does not)."""
        seeds = (self.hints.get(name, {}) or {}).get("seed_from_image") or {}
        out, vols = [], []
        for img_path, vol in seeds.items():
            vn = dns_label(vol)
            scratch = f"/rdlab-seed/{vn}"
            out.append({"name": dns_label(f"seed-{vn}"), "image": harbor_ref(self.services[name]["image"]),
                        "command": ["sh", "-c", f"if [ -z \"$(ls -A {scratch} 2>/dev/null)\" ]; then echo seeding {img_path} -> {scratch}; cp -a {img_path}/. {scratch}/; else echo {scratch} already populated; fi"],
                        "volumeMounts": [{"name": vn, "mountPath": scratch}], "securityContext": {"runAsUser": 0}})
            vols.append({"name": vn, "persistentVolumeClaim": {"claimName": vn}})
        return out, vols

    def injected_init_containers(self, name: str) -> tuple[list[dict], list[dict]]:
        out, vols = [], []
        for dep, cond in (self.services[name].get("depends_on") or {}).items():
            if cond.get("condition") == "service_completed_successfully" and self.injectable(dep):
                c, v = self.container(dep, self.services[dep])
                c["name"] = dns_label("init-" + dep)
                for k in ("readinessProbe", "startupProbe", "ports", "resources"):
                    c.pop(k, None)
                c.setdefault("securityContext", {})["runAsUser"] = 0
                out.append(c)
                vols += [x for x in v if x["name"] not in {y["name"] for y in vols}]
        return out, vols

    # --- workloads ------------------------------------------------------------------------------
    def labels(self, name: str, svc: dict) -> dict:
        return {"app.kubernetes.io/name": dns_label(name), "app.kubernetes.io/part-of": "rdlab", "rdlab.io/stack": self.key,
                "rdlab.io/container": dns_label(svc.get("container_name") or name)}

    def pod_spec(self, name: str, svc: dict, *, job: bool) -> dict:
        c, vols = self.container(name, svc)
        seed_inits, seed_vols = self.seed_init_containers(name)
        inits = seed_inits + self.wait_init_containers(name)
        inj, inj_vols = self.injected_init_containers(name)
        inits += inj
        inj_vols += [v for v in seed_vols if v["name"] not in {x["name"] for x in inj_vols}]
        allv = vols + [v for v in inj_vols if v["name"] not in {x["name"] for x in vols}]
        spec: dict = {"containers": [c], "terminationGracePeriodSeconds": 30}
        if svc.get("hostname"):                       # only when compose set it (crate node.name, clickhouse macros, citus);
            spec["hostname"] = dns_label(svc["hostname"])   # defaulting it to the service name collides with e.g. MaxScale's monit

        if inits:
            spec["initContainers"] = inits
        if allv:
            spec["volumes"] = allv
        if job:
            spec["restartPolicy"] = "OnFailure"
        return spec

    def deployment(self, name: str, svc: dict) -> dict:
        lab = self.labels(name, svc)
        return {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": dns_label(name), "namespace": self.ns, "labels": lab},
                "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app.kubernetes.io/name": dns_label(name)}},
                         "template": {"metadata": {"labels": lab}, "spec": self.pod_spec(name, svc, job=False)}}}

    def job(self, name: str, svc: dict) -> dict:
        lab = self.labels(name, svc)
        return {"apiVersion": "batch/v1", "kind": "Job",
                "metadata": {"name": dns_label(name), "namespace": self.ns, "labels": lab},
                "spec": {"backoffLimit": 10, "template": {"metadata": {"labels": lab}, "spec": self.pod_spec(name, svc, job=True)}}}

    def services_for(self, name: str, svc: dict) -> list[dict]:
        out = []
        sel = {"app.kubernetes.io/name": dns_label(name)}
        ports = sorted({int(p["target"]) for p in (svc.get("ports") or [])})
        cports = ports or ([self.first_port(name)] if self.first_port(name) else [])
        names = [dns_label(name)]
        cn = svc.get("container_name")
        if cn and dns_label(cn) != dns_label(name):
            names.append(dns_label(cn))
        for n in names:                                   # headless: DNS name -> pod IP, like the compose network
            out.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": n, "namespace": self.ns, "labels": self.labels(name, svc)},
                        "spec": {"clusterIP": "None", "publishNotReadyAddresses": True, "selector": sel,
                                 "ports": [{"name": f"p{p}", "port": p, "targetPort": p} for p in cports] or [{"name": "placeholder", "port": 1}]}})
        published = [(int(p["target"]), str(p["published"])) for p in (svc.get("ports") or []) if p.get("published")]
        if published:
            out.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": dns_label(name) + "-np", "namespace": self.ns, "labels": self.labels(name, svc),
                                                                            "annotations": {"rdlab.io/host-ports": ",".join(f"{hp}->{t}" for t, hp in published)}},
                        "spec": {"type": "NodePort", "selector": sel,
                                 "ports": [{"name": f"p{t}", "port": t, "targetPort": t, "nodePort": self.table[hp]} for t, hp in published]}})
        return out

    def pvcs(self) -> list[dict]:
        out = []
        for vn in sorted(self.cfg.get("volumes") or {}):
            out.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": dns_label(vn), "namespace": self.ns,
                                                                                         "labels": {"app.kubernetes.io/part-of": "rdlab", "rdlab.io/stack": self.key}},
                        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": PVC_SIZE}}}})
        return out

    # --- output ---------------------------------------------------------------------------------
    def render(self) -> Path:
        out_dir = STACKS / self.key / "k8s"
        out_dir.mkdir(exist_ok=True)
        for old in out_dir.glob("*.yaml"):
            old.unlink()
        workloads, services = [], []
        for name, svc in self.services.items():
            if self.injectable(name) and self.dependents_of(name):
                continue                                  # became initContainers
            if name in self.oneshot:
                workloads.append(self.job(name, svc))
            else:
                workloads.append(self.deployment(name, svc))
                services += self.services_for(name, svc)
        ns = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": self.ns, "labels": {"app.kubernetes.io/part-of": "rdlab", "rdlab.io/stack": self.key}}}
        files = {"namespace.yaml": [ns], "pvcs.yaml": self.pvcs(), "configmaps.yaml": list(self.configmaps.values()),
                 "services.yaml": services, "workloads.yaml": workloads}
        header = (f"# generated by k8s/gen.py from stacks/{self.key}/compose.yaml -- edit the compose file, then `make k8s-gen`\n")
        resources = []
        for fname, docs in files.items():
            if not docs:
                continue
            (out_dir / fname).write_text(header + yaml.dump_all(docs, Dumper=Dumper, sort_keys=False, width=200))
            resources.append(fname)
        kust = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "namespace": self.ns, "resources": resources}
        (out_dir / "kustomization.yaml").write_text(header + yaml.dump(kust, Dumper=Dumper, sort_keys=False))
        return out_dir


def cmd_manifests(keys: list[str]) -> None:
    table = write_ports()
    for key in keys or list_stacks():
        st = Stack(key, table)
        d = st.render()
        n = sum(1 for _ in d.glob("*.yaml"))
        print(f"  {key:11s} -> {d.relative_to(ROOT)} ({n} files, namespace {st.ns}, {len(st.services)} services, {len(st.configmaps)} configmaps)")
        for w in st.warnings:
            print(f"      ! {w}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("images")
    sub.add_parser("ports")
    sub.add_parser("k3d")
    m = sub.add_parser("manifests")
    m.add_argument("keys", nargs="*")
    a = ap.parse_args()
    if a.cmd == "images":
        for src, dst in all_images():
            print(src, dst)
    elif a.cmd == "ports":
        t = write_ports()
        print(f"{len(t)} host ports -> nodePorts written to {PORTS_JSON.relative_to(ROOT)}")
    elif a.cmd == "k3d":
        p = write_k3d()
        print(f"wrote {p.relative_to(ROOT)} and k8s/registries.yaml")
    elif a.cmd == "manifests":
        cmd_manifests(a.keys)


if __name__ == "__main__":
    main()
