"""Shared pieces of the logging phase.

A `LogProbe` describes how one engine logs: where its slow-query / statement log lives and how to read it,
how to switch to "log every statement" (for the overhead measurement), how auditing is switched on and
verified, and which files to collect. The phase runner (phases/dblogging.py) drives the same script for every
engine:

  1. report the effective logging settings
  2. run a fast and a slow query, each tagged with a unique marker alias, and prove that only the slow one
     shows up in the slow-query sink (and how long it takes to appear)
  3. switch on auditing, run an audited action, find it in the audit trail
  4. measure the throughput cost of logging every statement vs. the configured threshold
  5. record the Docker log driver/rotation of every container and copy the DB log files into results/
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from .. import dockerctl
from ..config import StackConfig, Target
from ..engines import Engine
from ..util import short_err


def marker() -> str:
    return "rdlab_mk_" + uuid.uuid4().hex[:10]


class LogProbe:
    slow_threshold_ms = 100
    slow_sleep_s = 0.3            # the slow query sleeps this long (3x threshold)
    settle_s = 1.0                # wait after switching "log everything" on (cluster settings propagate asynchronously)
    sink = ""                     # human description of where slow queries go
    audit_mechanism = ""          # human description of the audit facility

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        self.engine = engine
        self.cfg = cfg
        self.log = log
        self.primary: Target = cfg.primary
        self.container: str | None = cfg.primary.container
        self.lconf: dict[str, Any] = cfg.raw.get("logging", {}) or {}
        self.slow_threshold_ms = int(self.lconf.get("slow_ms", self.slow_threshold_ms))

    # --- helpers -------------------------------------------------------------------------------
    def q(self, sql: str, *, target: Target | None = None, fetch: bool = True, autocommit: bool = True):
        c = self.engine.connect(target or self.primary, autocommit=autocommit)
        try:
            return c.execute(sql, rendered=True, fetch=fetch)
        finally:
            c.close()

    def q_many(self, statements: list[str], *, target: Target | None = None):
        c = self.engine.connect(target or self.primary)
        out = None
        try:
            for s in statements:
                out = c.execute(s, rendered=True, fetch=True)
        finally:
            c.close()
        return out

    def read(self, path: str, *, tail_bytes: int = 4_000_000, container: str | None = None, user: str | None = None) -> str:
        return dockerctl.read_file(container or self.container, path, tail_bytes=tail_bytes, user=user)

    def grep_files(self, pattern: str, paths: list[str], *, container: str | None = None, user: str | None = None) -> str | None:
        """First line matching `pattern` across files (glob patterns allowed) inside the container."""
        c = container or self.container
        rc, out, _ = dockerctl.exec_in(c, ["sh", "-c", f"grep -h -m1 -e '{pattern}' {' '.join(paths)} 2>/dev/null | head -c 1500"], user=user, timeout=120)
        return out.strip() or None

    def file_bytes(self, paths: list[str], *, container: str | None = None, user: str | None = None) -> int:
        c = container or self.container
        rc, out, _ = dockerctl.exec_in(c, ["sh", "-c", f"cat {' '.join(paths)} 2>/dev/null | wc -c"], user=user, timeout=120)
        try:
            return int(out.strip() or 0)
        except ValueError:
            return 0

    # --- what each engine overrides ----------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return {}

    def slow_query(self, mk: str) -> str:
        """A statement that takes >= slow_sleep_s and carries `mk` as a column alias."""
        raise NotImplementedError

    def fast_query(self, mk: str) -> str:
        return f"SELECT 1 AS {mk}"

    def run_query(self, sql: str) -> None:
        self.q(sql)

    def find_slow(self, mk: str) -> dict[str, Any]:
        """Look the marker up in the slow-query sink. Returns {"found": bool, "sample": str, ...}."""
        raise NotImplementedError

    def full_logging(self, on: bool) -> str | None:
        """Switch "log every statement" on/off server-wide. Returns a description of what was changed,
        or None when the engine cannot do it (then the overhead test is skipped)."""
        return None

    def workload_session_sql(self, on: bool) -> str | None:
        """Per-connection statement for engines whose 'log everything' switch is session-scoped."""
        return None

    def audit_setup(self) -> str | None:
        return None

    def audit_actions(self, mk: str) -> list[str]:
        return [f"CREATE TABLE audit_{mk} (id INTEGER)", f"DROP TABLE audit_{mk}"]

    def audit_find(self, mk: str) -> dict[str, Any]:
        return {"found": False}

    def audit_teardown(self) -> None:
        pass

    def log_files(self) -> list[tuple[str, str]]:
        """(container, path-or-glob) pairs to copy into results/<engine>/logs/."""
        return []

    def log_dirs(self) -> list[tuple[str, str]]:
        """(container, directory) pairs whose total size is the engine's log footprint (rotation-proof measurement)."""
        d = self.lconf.get("dir")
        return [(self.container, d)] if d and self.container else []

    def log_bytes(self) -> int:
        """Total bytes of the engine's own logs right now (for the growth measurement). Directory totals when the
        engine logs into a directory (rotated files included), otherwise the sum of the listed files."""
        dirs = self.log_dirs()
        if dirs:
            return sum(dockerctl.dir_size_bytes(c, d) or 0 for c, d in dirs)
        tot = 0
        for c, p in self.log_files():
            tot += self.file_bytes([p], container=c)
        return tot

    def extra_report(self) -> dict[str, Any]:
        """Anything else worth recording (e.g. pg_stat_statements top entries, query_log counts)."""
        return {}


def wait_found(fn, *, timeout: float = 25.0, interval: float = 0.5) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    last: dict[str, Any] = {"found": False}
    while time.perf_counter() - t0 < timeout:
        try:
            last = fn()
        except Exception as e:  # noqa: BLE001
            last = {"found": False, "error": short_err(e)}
        if last.get("found"):
            return last, time.perf_counter() - t0
        time.sleep(interval)
    return last, time.perf_counter() - t0


def collect_logs(probe: LogProbe, dest: Path, *, max_bytes: int = 5_000_000) -> list[str]:
    """Copy (the tail of) every log file into results/<engine>/logs/. Returns relative paths."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for container, pattern in probe.log_files():
        files = dockerctl.list_files(container, pattern)
        if not files and "*" not in pattern and "?" not in pattern:
            files = [pattern]
        for f in files[:12]:
            data = dockerctl.read_file(container, f, tail_bytes=max_bytes)
            if not data:
                continue
            name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f.strip("/"))
            p = dest / f"{container.replace('rdlab-', '')}__{name}"
            p.write_text(data, errors="replace")
            out.append(str(p))
    return out
