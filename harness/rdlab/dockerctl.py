"""Thin wrappers around `docker` / `docker compose` for stack lifecycle and chaos."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from . import platform


class DockerError(RuntimeError):
    pass


def _run(cmd: list[str], *, check: bool = True, timeout: int = 600, cwd: Path | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd) if cwd else None)
    if check and p.returncode != 0:
        raise DockerError(f"{' '.join(cmd)}\nrc={p.returncode}\nstdout={p.stdout[-2000:]}\nstderr={p.stderr[-4000:]}")
    return p


def compose(stack_dir: Path, project: str, *args: str, check: bool = True, timeout: int = 900) -> subprocess.CompletedProcess:
    files = [f for f in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml") if (stack_dir / f).exists()]
    if not files:
        raise DockerError(f"no compose file in {stack_dir}")
    cmd = ["docker", "compose", "-f", str(stack_dir / files[0]), "-p", project, *args]
    return _run(cmd, check=check, timeout=timeout, cwd=stack_dir)


def ps(stack_dir: Path, project: str) -> list[dict[str, Any]]:
    p = compose(stack_dir, project, "ps", "-a", "--format", "json", check=False)
    out = p.stdout.strip()
    if not out:
        return []
    # compose v2 prints one JSON object per line (or a JSON array in older versions)
    if out.startswith("["):
        return json.loads(out)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def up(stack_dir: Path, project: str, *, timeout: int = 900, oneshot: set[str] | None = None,
       pull: bool = False) -> dict[str, Any]:
    """`docker compose up -d` and wait until every service is healthy (or running when it
    has no healthcheck; or exited 0 when listed in `oneshot`)."""
    if platform.k8s():
        return platform.up(stack_dir, project, timeout=timeout, oneshot=oneshot, pull=pull)
    oneshot = oneshot or set()
    t0 = time.time()
    if pull:
        compose(stack_dir, project, "pull", "--ignore-buildable", check=False, timeout=3600)
    compose(stack_dir, project, "up", "-d", "--remove-orphans", timeout=timeout)
    last: list[dict[str, Any]] = []
    while time.time() - t0 < timeout:
        last = ps(stack_dir, project)
        pending = []
        failed = []
        for c in last:
            svc = c.get("Service") or c.get("Name")
            state = (c.get("State") or "").lower()
            health = (c.get("Health") or "").lower()
            exit_code = c.get("ExitCode", 0)
            if state == "exited":
                if svc in oneshot and exit_code == 0:
                    continue
                failed.append(f"{svc} exited({exit_code})")
            elif state == "running":
                if svc in oneshot:                      # a one-shot init job still running is not "ready"
                    pending.append(f"{svc}:running")
                    continue
                if health in ("", "healthy"):
                    continue
                pending.append(f"{svc}:{health}")
            else:
                pending.append(f"{svc}:{state}")
        if failed:
            raise DockerError(f"services failed: {failed}\n" + logs_all(stack_dir, project, tail=60))
        if not pending:
            return {"seconds": round(time.time() - t0, 1), "services": [c.get("Service") for c in last]}
        time.sleep(2)
    raise DockerError(f"timeout waiting for stack {project}: {[ (c.get('Service'), c.get('State'), c.get('Health')) for c in last]}\n" + logs_all(stack_dir, project, tail=60))


def down(stack_dir: Path, project: str, *, volumes: bool = True) -> None:
    if platform.k8s():
        return platform.down(stack_dir, project, volumes=volumes)
    args = ["down", "--remove-orphans", "-t", "20"]
    if volumes:
        args.append("-v")
    compose(stack_dir, project, *args, check=False, timeout=600)


def logs_all(stack_dir: Path, project: str, tail: int = 100) -> str:
    p = compose(stack_dir, project, "logs", "--no-color", "--tail", str(tail), check=False)
    return (p.stdout + p.stderr)[-12000:]


def exec_in(container: str, cmd: list[str], *, timeout: int = 300, user: str | None = None,
            check: bool = False) -> tuple[int, str, str]:
    if platform.k8s():
        if user:                                     # kubectl exec has no -u: drop privileges inside the pod
            cmd = _as_user(user, cmd)
        return platform.exec_in(container, cmd, timeout=timeout, user=user, check=check)
    full = ["docker", "exec"]
    if user:
        full += ["-u", user]
    full += [container, *cmd]
    p = _run(full, check=check, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _as_user(user: str, cmd: list[str]) -> list[str]:
    """Wrap a command so it runs as `user` inside a pod: gosu where the image has it (postgres family), else su."""
    import shlex
    quoted = " ".join(shlex.quote(c) for c in cmd)
    return ["sh", "-c", f"if command -v gosu >/dev/null 2>&1; then exec gosu {shlex.quote(user)} {quoted}; "
                        f"else exec su -s /bin/sh {shlex.quote(user)} -c {shlex.quote(quoted)}; fi"]


def container_action(container: str, action: str, *, timeout: int = 120) -> None:
    assert action in {"stop", "start", "kill", "restart", "pause", "unpause"}
    if platform.k8s():
        return platform.container_action(container, action, timeout=timeout)
    args = ["docker", action]
    if action in {"stop", "restart"}:
        args += ["-t", "15"]
    _run([*args, container], timeout=timeout)


def inspect(container: str) -> dict[str, Any]:
    p = _run(["docker", "inspect", container])
    return json.loads(p.stdout)[0]


def stats(containers: list[str]) -> list[dict[str, Any]]:
    if not containers:
        return []
    p = _run(["docker", "stats", "--no-stream", "--format", "json", *containers], check=False, timeout=60)
    out = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append({"name": d.get("Name"), "cpu": d.get("CPUPerc"), "mem": d.get("MemUsage"),
                    "mem_pct": d.get("MemPerc"), "net": d.get("NetIO"), "block": d.get("BlockIO"), "pids": d.get("PIDs")})
    return out


def image_size(image: str) -> str | None:
    p = _run(["docker", "image", "inspect", image, "--format", "{{.Size}}"], check=False)
    if p.returncode != 0:
        return None
    try:
        b = int(p.stdout.strip())
        return f"{b/1e9:.2f} GB" if b > 1e9 else f"{b/1e6:.0f} MB"
    except ValueError:
        return None


def wait_for(predicate, *, timeout: float, interval: float = 0.5, desc: str = "condition") -> float:
    t0 = time.time()
    while True:
        try:
            if predicate():
                return time.time() - t0
        except Exception:  # noqa: BLE001
            pass
        if time.time() - t0 > timeout:
            raise TimeoutError(f"timed out after {timeout}s waiting for {desc}")
        time.sleep(interval)


# ---------------------------------------------------------------------------------------
# cgroup v2 resource accounting (exact CPU-seconds and memory per container, no sampling noise)
_CG_ROOT = Path("/sys/fs/cgroup/system.slice")


def cgroup_paths(containers: list[str]) -> dict[str, Path]:
    out = {}
    for name in containers:
        p = _run(["docker", "inspect", name, "--format", "{{.Id}}"], check=False)
        cid = p.stdout.strip()
        if not cid:
            continue
        d = _CG_ROOT / f"docker-{cid}.scope"
        if d.exists():
            out[name] = d
    return out


def cgroup_snapshot(paths: dict[str, Path]) -> dict[str, dict[str, float]]:
    snap = {}
    for name, d in paths.items():
        try:
            usage = 0.0
            for line in (d / "cpu.stat").read_text().splitlines():
                if line.startswith("usage_usec"):
                    usage = float(line.split()[1])
            quota = (d / "cpu.max").read_text().split()
            cpu_limit = float(quota[0]) / float(quota[1]) if quota[0] != "max" else None
            mem_max = (d / "memory.max").read_text().strip()
            snap[name] = {"usage_usec": usage, "cpu_limit_cores": cpu_limit, "mem_current": float((d / "memory.current").read_text()),
                          "mem_peak": float((d / "memory.peak").read_text()) if (d / "memory.peak").exists() else None,
                          "mem_limit": float(mem_max) if mem_max != "max" else None, "t": time.time()}
        except Exception:  # noqa: BLE001
            pass
    return snap


def cgroup_delta(before: dict[str, dict[str, float]], after: dict[str, dict[str, float]]) -> dict[str, dict[str, float | None]]:
    out = {}
    for name, a in after.items():
        b = before.get(name)
        if not b:
            continue
        el = a["t"] - b["t"]
        cores = (a["usage_usec"] - b["usage_usec"]) / 1e6 / el if el > 0 else 0.0
        lim = a.get("cpu_limit_cores")
        out[name] = {"cpu_cores_avg": round(cores, 3), "cpu_limit_cores": lim, "cpu_pct_of_limit": round(100 * cores / lim, 1) if lim else None,
                     "mem_peak_mb": round(a["mem_peak"] / 1048576) if a.get("mem_peak") else None, "mem_end_mb": round(a["mem_current"] / 1048576),
                     "mem_limit_mb": round(a["mem_limit"] / 1048576) if a.get("mem_limit") else None}
    return out


# ---------------------------------------------------------------------------------------
# helpers for the backup / logging phases: scratch restore containers, sizes, log config
def volume_name(project: str, volume: str) -> str:
    """Compose names a declared volume `<project>_<volume>`."""
    return f"{project}_{volume}"


def network_name(project: str) -> str:
    return f"{project}_default"


def scratch_run(name: str, image: str, *, project: str, volumes: list[str] | None = None, ports: list[str] | None = None,
                env: dict[str, str] | None = None, command: list[str] | None = None, entrypoint: str | None = None,
                user: str | None = None, extra: list[str] | None = None, timeout: int = 120) -> str:
    """`docker run -d` a throw-away container on the stack's network (restore drills). Returns the container id."""
    if platform.k8s():
        from . import k8s_scratch                       # a Pod + NodePort Service in the stack namespace on the same PVC
        return k8s_scratch.scratch_run(name, image, project=project, volumes=volumes, ports=ports, env=env, command=command,
                                       entrypoint=entrypoint, user=user, extra=extra, timeout=timeout)
    rm_container(name)
    cmd = ["docker", "run", "-d", "--name", name, "--network", network_name(project), "--label", "rdlab.scratch=1"]
    for v in volumes or []:
        cmd += ["-v", v]
    for p in ports or []:
        cmd += ["-p", p]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    if user:
        cmd += ["-u", user]
    if entrypoint:
        cmd += ["--entrypoint", entrypoint]
    cmd += extra or []
    cmd.append(image)
    cmd += command or []
    p = _run(cmd, timeout=timeout)
    return p.stdout.strip()


def rm_container(name: str) -> None:
    if platform.k8s():
        from . import k8s_scratch
        return k8s_scratch.rm_container(name)
    _run(["docker", "rm", "-f", "-v", name], check=False, timeout=120)


def container_running(name: str) -> bool:
    if platform.k8s():
        return platform.container_running(name)
    p = _run(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False)
    return p.returncode == 0 and p.stdout.strip() == "true"


def container_logs(name: str, tail: int = 200) -> str:
    if platform.k8s():
        return platform.container_logs(name, tail=tail)
    p = _run(["docker", "logs", "--tail", str(tail), name], check=False, timeout=60)
    return (p.stdout + p.stderr)[-12000:]


def dir_size_bytes(container: str, path: str, *, user: str | None = None) -> int | None:
    """Byte size of a file or directory inside a container (`du -sb`)."""
    if platform.k8s():
        return platform.dir_size_bytes(container, path, user=user)
    rc, out, _ = exec_in(container, ["du", "-sb", path], user=user, timeout=300)
    if rc != 0 or not out.strip():
        rc, out, _ = exec_in(container, ["sh", "-c", f"du -sk '{path}' 2>/dev/null"], user=user, timeout=300)
        if rc != 0 or not out.strip():
            return None
        try:
            return int(out.split()[0]) * 1024
        except ValueError:
            return None
    try:
        return int(out.split()[0])
    except ValueError:
        return None


def read_file(container: str, path: str, *, tail_bytes: int = 2_000_000, user: str | None = None) -> str:
    """Return the last `tail_bytes` of a file inside a container (empty string when missing)."""
    if platform.k8s():
        return platform.read_file(container, path, tail_bytes=tail_bytes, user=user)
    rc, out, _ = exec_in(container, ["sh", "-c", f"tail -c {tail_bytes} '{path}' 2>/dev/null"], user=user, timeout=120)
    return out if rc == 0 else ""


def list_files(container: str, glob_pattern: str, *, user: str | None = None) -> list[str]:
    if platform.k8s():
        return platform.list_files(container, glob_pattern, user=user)
    rc, out, _ = exec_in(container, ["sh", "-c", f"ls -1d {glob_pattern} 2>/dev/null"], user=user, timeout=60)
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def cp_from(container: str, src: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    p = _run(["docker", "cp", f"{container}:{src}", str(dest)], check=False, timeout=600)
    return p.returncode == 0


def log_config(container: str) -> dict[str, Any]:
    """The container's Docker logging driver/options plus the size of its json-file log on the host."""
    if platform.k8s():
        return platform.log_config(container)
    try:
        info = inspect(container)
    except DockerError:
        return {}
    lc = info.get("HostConfig", {}).get("LogConfig", {}) or {}
    out: dict[str, Any] = {"driver": lc.get("Type"), "options": lc.get("Config") or {}}
    lp = info.get("LogPath")
    if lp:
        try:
            out["host_log_bytes"] = Path(lp).stat().st_size
            rotated = sorted(Path(lp).parent.glob(Path(lp).name + ".*"))
            out["rotated_files"] = len(rotated)
            out["host_log_bytes_total"] = out["host_log_bytes"] + sum(f.stat().st_size for f in rotated)
        except OSError:
            out["host_log_bytes"] = None       # docker root not readable by this user
    if out.get("host_log_bytes") is None:
        # fall back to the size of what `docker logs` returns (current file only, uncompressed)
        try:
            p = subprocess.run(["docker", "logs", container], capture_output=True, timeout=120)
            out["docker_logs_bytes"] = len(p.stdout) + len(p.stderr)
            out["docker_logs_lines"] = p.stdout.count(b"\n") + p.stderr.count(b"\n")
        except Exception:  # noqa: BLE001
            pass
    return out


def wait_port(host: str, port: int, timeout: float = 60.0) -> float:
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return time.time() - t0
        except OSError:
            time.sleep(0.3)
    raise TimeoutError(f"port {host}:{port} not open after {timeout}s")
