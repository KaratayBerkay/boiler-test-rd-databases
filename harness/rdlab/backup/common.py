"""Shared pieces of the backup phase: strategy objects, the restore drill context, data fingerprints.

A *strategy* is one backup method of an engine (e.g. "pg_dump -Fd -j4", "pg_basebackup + WAL archive PITR").
Every strategy runs the same drill:  backup -> measure size -> restore somewhere else -> fingerprint the
restored copy against the source -> (optionally) a point-in-time test on a probe table -> clean up.
The result dict of every strategy has the same shape so the report can tabulate engines side by side.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import dockerctl
from ..config import StackConfig, Target
from ..dialects import render
from ..engines import Engine
from ..schema import LOAD_ORDER, TABLE_BY_NAME, Col, Table
from ..util import checksum_rows, short_err

PROBE_TABLE = Table("backup_probe", (Col("id", "int"), Col("ts_ms", "bigint")), pk=("id",), order_hint=("id",))
PROBE_ROWS = 1000          # rows present at the recovery point
SAMPLE_ROWS = 20000        # rows per table hashed for the fingerprint (ordered by primary key)


@dataclass
class Step:
    name: str
    cmd: str
    rc: int | None
    seconds: float
    out: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass
class StrategyResult:
    status: str = "ok"                      # ok | error | n/a | partial
    kind: str = ""                          # logical | physical | incremental | snapshot | pitr | flashback
    tool: str = ""
    backup_seconds: float | None = None
    backup_bytes: int | None = None
    restore_seconds: float | None = None
    verify: dict[str, Any] = field(default_factory=dict)      # {"match": bool, "tables": n, "mismatch": [...]}
    pitr: dict[str, Any] = field(default_factory=dict)        # {"match": bool, ...}
    notes: str = ""
    error: str | None = None
    steps: list[Step] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def backup_mb(self) -> float | None:
        return round(self.backup_bytes / 1048576, 1) if self.backup_bytes is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "kind": self.kind, "tool": self.tool, "backup_seconds": self.backup_seconds,
                "backup_bytes": self.backup_bytes, "backup_mb": self.backup_mb, "restore_seconds": self.restore_seconds,
                "verify": self.verify, "pitr": self.pitr, "notes": self.notes, "error": self.error,
                "steps": [{"name": s.name, "cmd": s.cmd[:400], "rc": s.rc, "seconds": round(s.seconds, 2), "out": s.out[-600:]} for s in self.steps],
                **self.extra}


@dataclass
class Strategy:
    name: str                    # short key used in results/SUMMARY.md, e.g. "pg_dump"
    kind: str
    tool: str
    description: str
    run: Callable[["Ctx"], StrategyResult]
    requires_container: bool = True


class Ctx:
    """What a strategy gets: engine/config, docker helpers with timing, fingerprints, the probe table."""

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        self.engine = engine
        self.cfg = cfg
        self.log = log
        self.primary: Target = cfg.primary
        self.container: str | None = cfg.primary.container
        self.bconf: dict[str, Any] = cfg.raw.get("backup", {}) or {}
        self.backup_dir: str = self.bconf.get("dir", "/backups")
        self.res = StrategyResult()
        self.source_fp: dict[str, dict[str, Any]] = {}

    # --- shell steps (docker exec) ------------------------------------------------------
    def sh(self, name: str, script: str, *, container: str | None = None, user: str | None = None, timeout: int = 1800,
           check: bool = True, shell: str = "sh", tail: int | None = None) -> Step:
        """Run a shell snippet inside a container, record it as a step, raise on failure when check=True.
        `tail=n` keeps only the last n output lines while preserving the command's exit code (no pipe masking)."""
        c = container or self.container
        if not c:
            raise RuntimeError("no container for docker exec")
        if tail:
            script = f"__o=$(mktemp); ( {script} ) > $__o 2>&1; __rc=$?; tail -n {tail} $__o; rm -f $__o; exit $__rc"
        t0 = time.perf_counter()
        rc, out, err = dockerctl.exec_in(c, [shell, "-c", script], user=user, timeout=timeout)
        st = Step(name, script.strip(), rc, time.perf_counter() - t0, (out + ("\n" + err if err else ""))[-4000:])
        self.res.steps.append(st)
        self.log(f"    {name:34s} rc={rc} {st.seconds:7.2f}s")
        if check and rc != 0:
            raise RuntimeError(f"{name} failed rc={rc}: {(err or out)[-800:]}")
        return st

    def sql_step(self, name: str, statements: list[str], *, target: Target | None = None, fetch_last: bool = False,
                 autocommit: bool = True) -> Any:
        """Run SQL statements through the driver as a recorded step."""
        t0 = time.perf_counter()
        conn = self.engine.connect(target or self.primary, autocommit=autocommit)
        rows = None
        try:
            for i, s in enumerate(statements):
                rows = conn.execute(s, rendered=True, fetch=(fetch_last and i == len(statements) - 1))
            if not autocommit:
                conn.commit()
            self.res.steps.append(Step(name, "; ".join(statements), 0, time.perf_counter() - t0, str(rows)[:400] if rows else ""))
            self.log(f"    {name:34s} ok {time.perf_counter() - t0:7.2f}s")
            return rows
        except Exception as e:  # noqa: BLE001
            self.res.steps.append(Step(name, "; ".join(statements), 1, time.perf_counter() - t0, short_err(e)))
            self.log(f"    {name:34s} FAILED {short_err(e)[:120]}")
            raise
        finally:
            conn.close()

    def size(self, path: str, *, container: str | None = None, user: str | None = None) -> int | None:
        return dockerctl.dir_size_bytes(container or self.container, path, user=user)

    # --- targets -----------------------------------------------------------------------------
    def alt_target(self, *, database: str | None = None, port: int | None = None, host: str | None = None,
                   user: str | None = None, password: str | None = None, name: str = "restored", **extra) -> Target:
        p = self.primary
        return Target(name=name, role="restored", host=host or p.host, port=port or p.port, user=user if user is not None else p.user,
                      password=password if password is not None else p.password, database=database if database is not None else p.database,
                      container=p.container, extra={**p.extra, **extra})

    # --- fingerprints -------------------------------------------------------------------------
    def fingerprint(self, target: Target | None = None, *, qualify: Callable[[str], str] | None = None,
                    tables: list[str] | None = None, conn=None) -> dict[str, dict[str, Any]]:
        """count + checksum of the first SAMPLE_ROWS rows (by primary key) of every benchmark table."""
        own = conn is None
        c = conn or self.engine.connect(target or self.primary)
        out: dict[str, dict[str, Any]] = {}
        try:
            for name in tables or LOAD_ORDER:
                t = TABLE_BY_NAME[name]
                q = qualify(name) if qualify else name
                try:
                    n = self.engine.count(c, q)
                    cols = ", ".join(t.colnames)
                    rows = c.execute(render(f"SELECT {cols} FROM {q} ORDER BY {', '.join(t.pk)} {{limit({SAMPLE_ROWS})}}", self.engine.dialect), rendered=True)
                    cs, k = checksum_rows(rows or [])
                    out[name] = {"count": n, "sample": k, "checksum": cs}
                except Exception as e:  # noqa: BLE001
                    out[name] = {"error": short_err(e)}
        finally:
            if own:
                c.close()
        return out

    def verify(self, target: Target | None = None, *, qualify: Callable[[str], str] | None = None, tables: list[str] | None = None,
               conn=None, label: str = "restored copy") -> dict[str, Any]:
        """Compare a restored copy with the source fingerprint taken at the start of the phase."""
        t0 = time.perf_counter()
        fp = self.fingerprint(target, qualify=qualify, tables=tables, conn=conn)
        mism = []
        for name, rec in fp.items():
            src = self.source_fp.get(name, {})
            if "error" in rec or "error" in src:
                mism.append({"table": name, "error": rec.get("error") or src.get("error")})
            elif rec["count"] != src.get("count") or rec["checksum"] != src.get("checksum"):
                mism.append({"table": name, "source_count": src.get("count"), "restored_count": rec["count"],
                             "checksum_match": rec["checksum"] == src.get("checksum")})
        v = {"match": not mism, "tables": len(fp), "rows": sum(r.get("count", 0) for r in fp.values() if "count" in r),
             "mismatch": mism, "seconds": round(time.perf_counter() - t0, 2)}
        self.log(f"    verify {label:27s} {'MATCH' if v['match'] else 'MISMATCH ' + str(mism)[:160]} ({v['tables']} tables, {v['rows']:,} rows)")
        return v

    # --- probe table for point-in-time tests --------------------------------------------------
    def probe_create(self, conn=None, *, rows: int = PROBE_ROWS, name: str = PROBE_TABLE.name) -> None:
        own = conn is None
        c = conn or self.engine.connect(self.primary)
        try:
            for stmt in (self.engine.dialect.drop_table_if_exists(name), self.engine.dialect.drop_table(name)):
                try:
                    c.execute(stmt, rendered=True, fetch=False)
                    break
                except Exception:  # noqa: BLE001
                    pass
            ddl = self.engine.dialect.create_table(PROBE_TABLE)
            if name != PROBE_TABLE.name:
                ddl = ddl.replace(PROBE_TABLE.name, name)
            c.execute(ddl, rendered=True, fetch=False)
            self.probe_insert(c, 1, rows, name=name)
        finally:
            if own:
                c.close()

    def probe_insert(self, conn, start: int, n: int, *, name: str = PROBE_TABLE.name) -> None:
        now = time.time_ns() // 1_000_000
        sql = f"INSERT INTO {name} (id, ts_ms) VALUES (?, ?)"
        conn.set_autocommit(False)
        try:
            for i in range(start, start + n, 500):
                conn.executemany(sql, [(j, now) for j in range(i, min(start + n, i + 500))])
            conn.commit()
        finally:
            conn.set_autocommit(True)

    def probe_count(self, target: Target | None = None, *, name: str = PROBE_TABLE.name, conn=None) -> int:
        own = conn is None
        c = conn or self.engine.connect(target or self.primary)
        try:
            return self.engine.count(c, name)
        finally:
            if own:
                c.close()

    def probe_drop(self, target: Target | None = None, *, name: str = PROBE_TABLE.name) -> None:
        try:
            c = self.engine.connect(target or self.primary)
            try:
                c.execute(self.engine.dialect.drop_table_if_exists(name), rendered=True, fetch=False)
            finally:
                c.close()
        except Exception:  # noqa: BLE001
            pass


def run_strategy(ctx: Ctx, s: Strategy) -> dict[str, Any]:
    ctx.res = StrategyResult(kind=s.kind, tool=s.tool)
    ctx.log(f"  strategy {s.name}: {s.description}")
    if s.requires_container and not ctx.container:
        ctx.res.status = "n/a"
        ctx.res.notes = "embedded engine / no container"
        return ctx.res.to_dict()
    t0 = time.perf_counter()
    try:
        r = s.run(ctx)
        if r is not ctx.res:
            ctx.res = r
    except Exception as e:  # noqa: BLE001
        ctx.res.status = "error"
        ctx.res.error = short_err(e, 600)
        ctx.log(f"  strategy {s.name} FAILED: {short_err(e)}")
    d = ctx.res.to_dict()
    d["seconds"] = round(time.perf_counter() - t0, 1)
    d["description"] = s.description
    if ctx.res.status == "ok":
        ctx.log(f"  {s.name}: backup {ctx.res.backup_seconds}s / {ctx.res.backup_mb} MB, restore {ctx.res.restore_seconds}s, "
                f"verified={ctx.res.verify.get('match')}" + (f", pitr={ctx.res.pitr.get('match')}" if ctx.res.pitr else ""))
    return d
