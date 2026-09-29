"""Run the harness against either Docker Compose (default) or a k3s/k3d cluster.

Set RDLAB_PLATFORM=k3s to make the container-level primitives in dockerctl talk to kubectl instead of docker. The
translation is keyed on the compose container name, which the manifest generator (k8s/gen.py) stamps on every pod as
the label `rdlab.io/container`; so `dockerctl.exec_in("rdlab-pg-primary", ...)` finds that pod in any namespace and
`kubectl exec`s into it. Everything the phases read through exec_in (read_file, list_files, dir_size_bytes, grep,
counts) then works unchanged, and so do container_logs, container_running, lifecycle stop/start (for failover) and
up/down (kubectl apply/delete of stacks/<key>/k8s).

Two things are deliberately not emulated and stay Docker-only (the phase degrades or is skipped, never silently wrong):
  - scratch_run: the backup restore drills start throw-away containers on the stack network with the backup volume.
    On k8s the equivalent is a Pod mounting the same PVC; that is engine-specific and not generated here yet, so the
    backup phase raises PlatformUnsupported under k3s and the runner records it as skipped.
  - cgroup_snapshot/stats: cgroup files are read from the host under compose; under k3d the containers live inside the
    node container, so per-container CPU/mem accounting returns empty and the loadtest reports rows/s without it.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = os.environ.get("RDLAB_PLATFORM", "compose").lower()
CONTEXT = os.environ.get("RDLAB_K8S_CONTEXT", f"k3d-{os.environ.get('CLUSTER', 'rdlab')}")
LABEL = "rdlab.io/container"


class PlatformUnsupported(RuntimeError):
    """Raised when a phase needs a primitive that this platform does not provide (e.g. scratch restore on k8s)."""


def k8s() -> bool:
    return PLATFORM == "k3s"


def kubectl(*args: str, check: bool = True, timeout: int = 120, input: str | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(["kubectl", "--context", CONTEXT, *args], capture_output=True, text=True, timeout=timeout, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}\nrc={p.returncode}\n{p.stdout[-1500:]}\n{p.stderr[-2500:]}")
    return p


@lru_cache(maxsize=256)
def _resolve(container: str, _generation: int = 0) -> tuple[str, str]:
    """(namespace, pod) for a compose container name, via the rdlab.io/container label. Cached; _generation busts it."""
    p = kubectl("get", "pod", "-A", "-l", f"{LABEL}={container}",
                "-o", "jsonpath={range .items[*]}{.metadata.namespace} {.metadata.name} {.status.phase}\n{end}", check=False)
    running = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            running.append(parts)
    if not running:
        raise RuntimeError(f"no pod with {LABEL}={container} (is the stack deployed? kubectl get pod -A -l {LABEL}={container})")
    for ns, pod, phase in running:
        if phase == "Running":
            return ns, pod
    return running[0][0], running[0][1]


_GEN = 0


def _pod(container: str) -> tuple[str, str]:
    return _resolve(container, _GEN)


def bust_pod_cache() -> None:
    """After a pod is deleted/recreated (failover, rollout) the cached name is stale; call this to force re-resolution."""
    global _GEN
    _GEN += 1


# --- primitives dockerctl delegates to when k8s() -----------------------------------------------------------
def exec_in(container: str, cmd: list[str], *, timeout: int = 300, user: str | None = None, check: bool = False):
    ns, pod = _pod(container)
    # `user` (docker exec -u) has no kubectl equivalent; the pod runs as its manifest's securityContext/user.
    p = kubectl("exec", "-n", ns, pod, "--", *cmd, check=False, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"kubectl exec {pod} {' '.join(cmd)} rc={p.returncode}: {p.stderr[-800:]}")
    return p.returncode, p.stdout, p.stderr


def container_logs(name: str, tail: int = 200) -> str:
    try:
        ns, pod = _pod(name)
    except RuntimeError as e:
        return str(e)
    p = kubectl("logs", "-n", ns, pod, f"--tail={tail}", check=False)
    return (p.stdout + p.stderr)[-12000:]


def container_running(name: str) -> bool:
    p = kubectl("get", "pod", "-A", "-l", f"{LABEL}={name}", "-o", "jsonpath={.items[*].status.phase}", check=False)
    return "Running" in p.stdout.split()


def container_action(container: str, action: str, *, timeout: int = 120) -> None:
    """Failover/chaos primitive. stop/kill: scale the owning Deployment to 0 (the pod is deleted, the endpoint drops
    out of every Service). start: scale back to 1. restart: rollout restart. Pod recreation invalidates the cache."""
    ns, pod = _pod(container)
    owner = _deploy_of(ns, pod)
    if action in ("stop", "kill"):
        kubectl("scale", "-n", ns, f"deployment/{owner}", "--replicas=0", timeout=timeout)
        kubectl("wait", "-n", ns, "--for=delete", f"pod/{pod}", f"--timeout={timeout}s", check=False)
    elif action == "start":
        kubectl("scale", "-n", ns, f"deployment/{owner}", "--replicas=1", timeout=timeout)
        kubectl("rollout", "status", "-n", ns, f"deployment/{owner}", f"--timeout={timeout}s", check=False)
    elif action == "restart":
        kubectl("rollout", "restart", "-n", ns, f"deployment/{owner}", timeout=timeout)
        kubectl("rollout", "status", "-n", ns, f"deployment/{owner}", f"--timeout={timeout}s", check=False)
    elif action in ("pause", "unpause"):
        raise PlatformUnsupported(f"{action} not supported on k8s")
    bust_pod_cache()


def _deploy_of(ns: str, pod: str) -> str:
    p = kubectl("get", "pod", "-n", ns, pod, "-o", "jsonpath={.metadata.labels.app\\.kubernetes\\.io/name}", check=False)
    return p.stdout.strip() or pod.rsplit("-", 2)[0]


def read_file(container: str, path: str, *, tail_bytes: int = 2_000_000, user: str | None = None) -> str:
    rc, out, _ = exec_in(container, ["sh", "-c", f"tail -c {tail_bytes} '{path}' 2>/dev/null"], timeout=120)
    return out if rc == 0 else ""


def list_files(container: str, glob_pattern: str, *, user: str | None = None) -> list[str]:
    rc, out, _ = exec_in(container, ["sh", "-c", f"ls -1d {glob_pattern} 2>/dev/null"], timeout=60)
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def dir_size_bytes(container: str, path: str, *, user: str | None = None) -> int | None:
    rc, out, _ = exec_in(container, ["sh", "-c", f"du -sb '{path}' 2>/dev/null || du -sk '{path}' 2>/dev/null"], timeout=300)
    if rc != 0 or not out.strip():
        return None
    try:
        return int(out.split()[0])
    except ValueError:
        return None


def log_config(container: str) -> dict:
    """No Docker log driver on k8s; report the kubelet's container-log rotation and the current log size instead."""
    try:
        ns, pod = _pod(container)
    except RuntimeError:
        return {}
    return {"driver": "kubelet (container-log-max-size/-files via k3d.yaml)", "options": {}, "pod": f"{ns}/{pod}"}


# --- lifecycle: up/down a whole stack -----------------------------------------------------------------------
def _kustomize_dir(stack_dir: Path) -> Path:
    return stack_dir / "k8s"


def up(stack_dir: Path, project: str, *, timeout: int = 900, oneshot=None, pull: bool = False) -> dict:
    kdir = _kustomize_dir(stack_dir)
    if not (kdir / "kustomization.yaml").exists():
        raise PlatformUnsupported(f"no k8s manifests for {stack_dir.name}: run `make k8s-gen` (k8s/gen.py manifests {stack_dir.name})")
    t0 = time.time()
    kubectl("apply", "-k", str(kdir), timeout=300)
    ns = _namespace(kdir)
    # wait for every Deployment to become available (Jobs run to completion on their own; init waits handle ordering)
    deploys = kubectl("get", "deploy", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}", check=False).stdout.split()
    for d in deploys:
        kubectl("rollout", "status", "-n", ns, f"deployment/{d}", f"--timeout={timeout}s", check=False)
    bust_pod_cache()
    return {"seconds": round(time.time() - t0, 1), "namespace": ns, "deployments": deploys}


def down(stack_dir: Path, project: str, *, volumes: bool = True) -> None:
    kdir = _kustomize_dir(stack_dir)
    ns = _namespace(kdir)
    if volumes:
        kubectl("delete", "namespace", ns, "--ignore-not-found", "--wait=true", timeout=300, check=False)
    else:
        kubectl("delete", "-k", str(kdir), "--ignore-not-found", check=False, timeout=300)
    bust_pod_cache()


def _namespace(kdir: Path) -> str:
    import yaml
    k = yaml.safe_load((kdir / "kustomization.yaml").read_text())
    return k.get("namespace") or f"rdlab-{kdir.parent.name}"


# --- port translation: lab.yaml host port -> published nodePort ---------------------------------------------
@lru_cache(maxsize=1)
def _port_table() -> dict[str, int]:
    f = ROOT / "k8s" / "ports.json"
    if not f.exists():
        return {}
    return {hp: v["nodePort"] for hp, v in json.loads(f.read_text())["ports"].items()}


def map_port(host_port: int) -> int:
    """Translate a compose host port (as written in lab.yaml targets) to the nodePort k3d publishes on 127.0.0.1."""
    return _port_table().get(str(host_port), host_port)
