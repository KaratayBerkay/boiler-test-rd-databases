"""rdlab command line."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import typer
from rich.console import Console

from . import dockerctl
from .config import list_stacks, load_stack
from .datagen import ensure_csvs, counts_for
from .engines import make_engine
from .logsetup import log, log_exception, reset_phase, set_phase, setup_logging
from .report import build_summary, save_result
from .util import RESULTS, now_iso, run_id as new_run_id, short_err

app = typer.Typer(help="Relational database lab: capability, optimisation, connection, backup, logging and replication tests across SQL engines.", no_args_is_help=True)
con = Console()


@app.command("list")
def cmd_list():
    """List configured stacks."""
    for k in list_stacks():
        c = load_stack(k)
        log(f"{k:18s} {c.display:32s} dialect={c.dialect:10s} driver={c.driver:18s} targets={[t.name for t in c.targets]} compose={'yes' if c.has_compose else 'no'}")


@app.command()
def gen(scale: float = typer.Option(1.0, help="data scale factor"), force: bool = False):
    """Generate the deterministic CSV dataset."""
    t0 = time.time()
    paths = ensure_csvs(scale, force=force)
    log(f"generated in {time.time()-t0:.1f}s: {counts_for(scale)} -> {paths['events'].parent}")


@app.command()
def up(stack: str, pull: bool = False):
    """Start a stack and wait until healthy."""
    c = load_stack(stack)
    if not c.has_compose:
        log("embedded engine: nothing to start")
        return
    r = dockerctl.up(c.stack_dir, c.project, oneshot=c.oneshot, pull=pull)
    log(f"up: {r}")


@app.command()
def down(stack: str, volumes: bool = True):
    """Stop a stack (and remove its volumes)."""
    c = load_stack(stack)
    if c.has_compose:
        dockerctl.down(c.stack_dir, c.project, volumes=volumes)
    log("down")


def _phase(engine, cfg, name: str, fn, result: dict, **kw):
    log(f"[{cfg.key}] phase: {name}")
    tok = set_phase(name)
    t0 = time.time()
    try:
        result["phases"][name] = fn(engine, cfg, **kw)
        result["phases"][name]["_seconds"] = round(time.time() - t0, 1) if isinstance(result["phases"][name], dict) else None
        log(f"[{cfg.key}] phase {name} done in {round(time.time() - t0, 1)}s")
    except Exception as e:  # noqa: BLE001
        result["phases"][name] = {"_error": short_err(e), "_seconds": round(time.time() - t0, 1)}
        log_exception(f"  phase {name} FAILED: {short_err(e)}")
    finally:
        reset_phase(tok)
    save_result(cfg.key, result["run_id"], result)


def _run_phases(stack: str, phases: list[str], scale: float | None, failover: bool, storms: list[int] | None = None) -> dict:
    cfg = load_stack(stack)
    scale = scale if scale is not None else cfg.scale
    engine = make_engine(cfg)
    result = {"engine": cfg.key, "display": cfg.display, "category": cfg.category, "dialect": cfg.dialect, "driver": cfg.driver,
              "image": cfg.image, "run_id": new_run_id(), "started": now_iso(), "scale": scale, "notes": cfg.notes, "phases": {}}
    logfile = setup_logging(cfg.key, result["run_id"])
    result["harness_log"] = str(logfile.relative_to(RESULTS.parent))
    log(f"[{cfg.key}] run {result['run_id']} phases={phases} scale={scale} (log: {result['harness_log']})")
    from .phases.load import run_load
    from .phases.capabilities import run_capabilities
    from .phases.bench import run_bench
    from .phases.optimize import run_optimize
    from .phases.connections import run_connections
    from .phases.replication import run_replication
    from .phases.loadtest import run_loadtest
    from .phases.backup import run_backup
    from .phases.dblogging import run_dblogging
    ensure_csvs(scale)
    if "load" in phases:
        _phase(engine, cfg, "load", lambda e, c: run_load(e, c, scale, log=log), result)
    if "capabilities" in phases:
        _phase(engine, cfg, "capabilities", lambda e, c: run_capabilities(e, c, log=log), result)
    if "bench" in phases:
        _phase(engine, cfg, "bench", lambda e, c: run_bench(e, c, scale, plans_dir=RESULTS / cfg.key / "plans", log=log), result)
    if "optimize" in phases:
        _phase(engine, cfg, "optimize", lambda e, c: run_optimize(e, c, scale, log=log), result)
    if "connections" in phases:
        _phase(engine, cfg, "connections", lambda e, c: run_connections(e, c, scale, log=log, storms=storms), result)
    if "loadtest" in phases:
        _phase(engine, cfg, "loadtest", lambda e, c: run_loadtest(e, c, scale, log=log), result)
    if "backup" in phases:
        _phase(engine, cfg, "backup", lambda e, c: run_backup(e, c, scale, log=log), result)
    if "logging" in phases:
        _phase(engine, cfg, "logging", lambda e, c: run_dblogging(e, c, scale, log=log), result)
    if "replication" in phases:
        _phase(engine, cfg, "replication", lambda e, c: run_replication(e, c, scale, do_failover=failover, log=log), result)
    try:
        c = engine.connect_primary()
        result["server_version"] = c.server_version()
        c.close()
    except Exception:  # noqa: BLE001
        pass
    result["finished"] = now_iso()
    p = save_result(cfg.key, result["run_id"], result)
    log(f"saved {p}")
    return result


ALL_PHASES = ["load", "capabilities", "bench", "optimize", "connections", "loadtest", "backup", "logging", "replication"]


@app.command()
def run(stack: str, scale: float = typer.Option(None), phases: str = typer.Option(",".join(ALL_PHASES), help="comma-separated phases"),
        failover: bool = typer.Option(False, help="run the destructive failover test at the end of the replication phase"),
        no_up: bool = typer.Option(False, "--no-up", help="assume the stack is already running"),
        keep: bool = typer.Option(False, help="leave the stack running afterwards"), pull: bool = False,
        storms: str = typer.Option(None, help="comma-separated connection storm sizes")):
    """Bring a stack up, run the selected phases, tear it down."""
    cfg = load_stack(stack)
    ph = [p.strip() for p in phases.split(",") if p.strip()]
    st = [int(x) for x in storms.split(",")] if storms else None
    if cfg.has_compose and not no_up:
        log(f"[{stack}] starting stack ...")
        t0 = time.time()
        r = dockerctl.up(cfg.stack_dir, cfg.project, oneshot=cfg.oneshot, pull=pull)
        log(f"[{stack}] up in {r['seconds']}s: {r['services']}")
        time.sleep(float(cfg.features.get("settle_seconds", 3)))
    try:
        _run_phases(stack, ph, scale, failover, st)
    finally:
        if cfg.has_compose and not keep and not no_up:
            log(f"[{stack}] tearing down ...")
            dockerctl.down(cfg.stack_dir, cfg.project)
    build_summary()


@app.command()
def phase(stack: str, name: str, scale: float = typer.Option(None), failover: bool = False, storms: str = typer.Option(None)):
    """Run a single phase against an already-running stack."""
    st = [int(x) for x in storms.split(",")] if storms else None
    _run_phases(stack, [name], scale, failover, st)
    build_summary()


@app.command()
def report():
    """Rebuild results/SUMMARY.md from all latest.json files."""
    md, js = build_summary()
    log(f"wrote {md} and {js}")


@app.command()
def sql(stack: str, query: str, target: str = "primary"):
    """Run an ad-hoc SQL statement against a running stack."""
    cfg = load_stack(stack)
    eng = make_engine(cfg)
    t = cfg.by_name(target) or cfg.primary
    c = eng.connect(t)
    try:
        rows = c.execute(query, rendered=True)
        for r in (rows or [])[:50]:
            log(str(r))
        log(f"({len(rows) if rows else 0} rows)")
    finally:
        c.close()


if __name__ == "__main__":
    app()
