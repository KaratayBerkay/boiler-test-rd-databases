"""Scratch restore Pods on k3s: the Kubernetes counterpart of `dockerctl.scratch_run` used by the backup drills.

A restore drill starts a throw-away database instance from a backup (pg_combinebackup output, a MySQL clone, a
prepared mariadb-backup/xtrabackup directory, a QuestDB checkpoint copy) and lets the harness connect to it. Under
Docker that is `docker run` on the stack network with the backup volume; here it is a Pod in the stack's namespace
that mounts the same PersistentVolumeClaim, plus a NodePort Service on the nodePort that k8s/ports.json reserves for
the stack's `backup.restore_port` (k3d publishes it on 127.0.0.1, exactly like the compose host port).

Mapping of the docker-run arguments:
  volumes  "<project>_<pvc>:<mountPath>"  -> persistentVolumeClaim <pvc> (RWO: fine, the lab cluster is one node)
  ports    "<hostPort>:<containerPort>"    -> Service type NodePort, nodePort = platform.map_port(hostPort)
  command / entrypoint / user             -> args / command / securityContext.runAsUser
  image                                    -> the Harbor mirror of the image (k8s/images.lock.json, else computed)
The pod carries the label rdlab.io/container=<name>, so platform.exec_in/container_logs/container_running find it
like any generated pod; rm_container deletes the pod and its Service.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import platform

ROOT = Path(__file__).resolve().parents[2]
SCRATCH_LABEL = "rdlab.io/scratch"


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    f = ROOT / "k8s" / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("'\"")
    return out


def harbor_ref(src: str) -> str:
    """Harbor mirror of a source image: the pushed reference from images.lock.json, else the same naming rule as gen.py."""
    lock = ROOT / "k8s" / "images.lock.json"
    if lock.exists():
        try:
            rec = json.loads(lock.read_text()).get("images", {}).get(src)
            if rec and rec.get("harbor"):
                return rec["harbor"]
        except json.JSONDecodeError:
            pass
    env = _env()
    reg = f"{env.get('HARBOR_HOST', '127.0.0.1')}:{env.get('HARBOR_HTTPS_PORT', '8443')}"
    project = env.get("HARBOR_PROJECT", "rdlab")
    name = src
    first = name.split("/")[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        name = name.split("/", 1)[1]
    if name.startswith(f"{project}/"):
        name = name[len(project) + 1:]
    if ":" not in name.rsplit("/", 1)[-1]:
        name += ":latest"
    return f"{reg}/{project}/{name}"


def namespace_of(project: str) -> str:
    """gen.py puts stack rdlab-<key> into namespace rdlab-<key> (the compose project name)."""
    return project


def scratch_run(name: str, image: str, *, project: str, volumes: list[str] | None = None, ports: list[str] | None = None,
                env: dict[str, str] | None = None, command: list[str] | None = None, entrypoint: str | None = None,
                user: str | None = None, extra: list[str] | None = None, timeout: int = 120) -> str:
    ns = namespace_of(project)
    rm_container(name)
    mounts, vols = [], []
    for i, v in enumerate(volumes or []):
        src, _, dest = v.partition(":")
        pvc = src[len(project) + 1:] if src.startswith(project + "_") else src
        mounts.append({"name": f"v{i}", "mountPath": dest})
        vols.append({"name": f"v{i}", "persistentVolumeClaim": {"claimName": pvc}})
    cports, svc_ports = [], []
    for p in ports or []:
        host, _, cont = p.partition(":")
        cports.append({"containerPort": int(cont)})
        svc_ports.append({"name": f"p{cont}", "port": int(cont), "targetPort": int(cont), "nodePort": platform.map_port(int(host))})
    container: dict = {"name": "main", "image": harbor_ref(image), "imagePullPolicy": "IfNotPresent",
                       "env": [{"name": k, "value": str(v)} for k, v in (env or {}).items()],
                       "volumeMounts": mounts, "ports": cports}
    if entrypoint:
        container["command"] = [entrypoint]
    if command:
        container["args"] = list(command)
    if user:
        uid = int(user.split(":")[0]) if user.split(":")[0].isdigit() else None
        if uid is not None:
            container["securityContext"] = {"runAsUser": uid}
    labels = {platform.LABEL: name, SCRATCH_LABEL: "1", "app.kubernetes.io/name": name, "app.kubernetes.io/part-of": "rdlab"}
    docs = [{"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": ns, "labels": labels},
             "spec": {"restartPolicy": "Never", "hostname": name[:63], "containers": [container], "volumes": vols}}]
    if svc_ports:
        docs.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns, "labels": labels},
                     "spec": {"type": "NodePort", "selector": {platform.LABEL: name}, "ports": svc_ports}})
    platform.kubectl("apply", "-f", "-", input=json.dumps({"apiVersion": "v1", "kind": "List", "items": docs}), timeout=120)
    platform.bust_pod_cache()
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = platform.kubectl("get", "pod", "-n", ns, name, "-o", "jsonpath={.status.phase}", check=False)
        if p.stdout.strip() in ("Running", "Succeeded", "Failed"):
            return name
        time.sleep(1.0)
    return name


def rm_container(name: str) -> None:
    platform.kubectl("delete", "pod,svc", "-A", "-l", f"{platform.LABEL}={name},{SCRATCH_LABEL}=1", "--ignore-not-found",
                     "--wait=true", "--grace-period=5", check=False, timeout=180)
    platform.bust_pod_cache()


def container_running(name: str) -> bool:
    return platform.container_running(name)
