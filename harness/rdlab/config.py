"""Load stack metadata (stacks/<key>/lab.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import STACKS


@dataclass
class Target:
    name: str
    role: str                      # primary | replica | proxy | node | embedded
    host: str = "127.0.0.1"
    port: int = 0
    user: str = ""
    password: str = ""
    database: str = ""
    container: str | None = None   # docker container name (for exec/stop/start)
    extra: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.name}({self.role}) {self.host}:{self.port}"


@dataclass
class StackConfig:
    key: str
    display: str
    dialect: str
    driver: str
    category: str
    stack_dir: Path
    project: str
    targets: list[Target]
    image: str = ""
    replication: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)
    oneshot: set[str] = field(default_factory=set)
    scale: float = 1.0
    notes: str = ""
    docs: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def primary(self) -> Target:
        for t in self.targets:
            if t.role == "primary":
                return t
        return self.targets[0]

    def by_role(self, role: str) -> list[Target]:
        return [t for t in self.targets if t.role == role]

    def by_name(self, name: str) -> Target | None:
        for t in self.targets:
            if t.name == name:
                return t
        return None

    @property
    def replicas(self) -> list[Target]:
        return self.by_role("replica")

    @property
    def proxies(self) -> list[Target]:
        return self.by_role("proxy")

    @property
    def has_compose(self) -> bool:
        return any((self.stack_dir / f).exists() for f in ("compose.yaml", "compose.yml"))


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return os.path.expandvars(v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


def load_stack(key: str) -> StackConfig:
    d = STACKS / key
    f = d / "lab.yaml"
    if not f.exists():
        raise FileNotFoundError(f"no lab.yaml for stack '{key}' ({f})")
    raw = _expand(yaml.safe_load(f.read_text()) or {})
    targets = [Target(**{k: v for k, v in t.items() if k in Target.__dataclass_fields__} | {"extra": {k: v for k, v in t.items() if k not in Target.__dataclass_fields__}}) for t in raw.get("targets", [])]
    # On k3s the stack's ports are published on 127.0.0.1:<nodePort> (k8s/ports.json), not the compose host port.
    from . import platform
    if platform.k8s():
        for t in targets:
            if t.host in ("127.0.0.1", "localhost") and t.port:
                t.port = platform.map_port(t.port)
        rb = raw.get("backup") or {}
        if rb.get("restore_port"):
            rb["restore_port"] = platform.map_port(rb["restore_port"])
    return StackConfig(
        key=key,
        display=raw.get("display", key),
        dialect=raw.get("dialect", "ansi"),
        driver=raw.get("driver", ""),
        category=raw.get("category", ""),
        stack_dir=d,
        project=raw.get("project", f"rdlab-{key}"),
        targets=targets,
        image=raw.get("image", ""),
        replication=raw.get("replication", {}) or {},
        features=raw.get("features", {}) or {},
        oneshot=set(raw.get("oneshot", []) or []),
        scale=float(raw.get("scale", 1.0)),
        notes=raw.get("notes", "") or "",
        docs=raw.get("docs", []) or [],
        raw=raw,
    )


def list_stacks() -> list[str]:
    if not STACKS.exists():
        return []
    return sorted(p.name for p in STACKS.iterdir() if (p / "lab.yaml").exists())
