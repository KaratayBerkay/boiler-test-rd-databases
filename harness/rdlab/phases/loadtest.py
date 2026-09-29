"""Phase: multi-connection bulk-insert load test with per-container resource sampling.

For each target (e.g. the coordinator directly vs the HAProxy->PgBouncer entry point) and each worker count,
W processes insert `batch`-row multi-row INSERTs into `events` for `seconds`. Reports rows/s, batch latency
percentiles, errors, and `docker stats` CPU/memory of every container in the stack during the run.
"""
from __future__ import annotations

import multiprocessing as mp
import random
import re
import threading
import time
from typing import Any

from .. import dockerctl
from ..config import StackConfig
from ..datagen import Sizes
from ..engines import Engine
from ..util import short_err, summarize

INSERT_COLS = "id, customer_id, event_type, occurred_at, payload, value_num"


def _insert_worker(stack_key: str, target_name: str, upper: int, seconds: float, batch: int, seed: int, start_at: float, q: mp.Queue, run_index: int = 0, mode: str = "values") -> None:
    from ..config import load_stack
    from ..engines import make_engine
    cfg = load_stack(stack_key)
    eng = make_engine(cfg)
    t = cfg.by_name(target_name) or cfg.primary
    d = eng.dialect
    lat: list[float] = []
    rows = 0
    errors = 0
    first_err = None
    try:
        c = eng.connect(t, timeout=30)
    except Exception as e:  # noqa: BLE001
        q.put({"seed": seed, "lat": [], "rows": 0, "errors": 1, "first_error": f"connect: {e}"[:200]})
        return
    r = random.Random(seed)
    base = 40_000_000_000 + (run_index * 100 + seed) * 10_000_000     # unique id range per run and worker
    multirow = getattr(d, "multirow_insert", True)
    row_tpl = "(?, ?, 'loadtest', {now}, NULL, ?)"
    sql_multi = f"INSERT INTO events ({INSERT_COLS}) VALUES " + ", ".join(row_tpl for _ in range(batch))
    sql_one = f"INSERT INTO events ({INSERT_COLS}) VALUES {row_tpl}"
    n = 0
    while time.time() < start_at:
        time.sleep(0.001)
    stop = time.perf_counter() + seconds
    while time.perf_counter() < stop:
        vals = [(base + n + i, r.randint(1, upper), r.randint(0, 100)) for i in range(batch)]
        n += batch
        t0 = time.perf_counter_ns()
        try:
            if mode == "copy":
                # PostgreSQL-family COPY of a CSV chunk (the bulk path Citus/PostgreSQL recommend)
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                data = "".join(f"{i},{cid},loadtest,{now},,{v}\n" for i, cid, v in vals)
                with c.raw.cursor() as cur:
                    with cur.copy(f"COPY events ({INSERT_COLS}) FROM STDIN (FORMAT csv, NULL '')") as cp:
                        cp.write(data)
            elif multirow:
                c.execute(sql_multi, [x for v in vals for x in v], fetch=False)
            else:
                c.set_autocommit(False)
                c.executemany(sql_one, vals)
                c.commit()
                c.set_autocommit(True)
            lat.append((time.perf_counter_ns() - t0) / 1e6)
            rows += batch
        except Exception as e:  # noqa: BLE001
            errors += 1
            first_err = first_err or str(e)[:200]
            try:
                c.rollback()
            except Exception:  # noqa: BLE001
                pass
            if errors > 20:
                break
    try:
        c.close()
    except Exception:  # noqa: BLE001
        pass
    q.put({"seed": seed, "lat": lat, "rows": rows, "errors": errors, "first_error": first_err})


def _stats_sampler(containers: list[str], stop: threading.Event, samples: list[dict[str, Any]]) -> None:
    while not stop.is_set():
        try:
            snap = dockerctl.stats(containers)
            samples.append({"t": time.time(), "stats": snap})
        except Exception:  # noqa: BLE001
            pass
        stop.wait(2.0)


def _pct(s: str | None) -> float:
    if not s:
        return 0.0
    m = re.match(r"([\d.]+)%", s)
    return float(m.group(1)) if m else 0.0


def _mem_mb(s: str | None) -> float:
    if not s:
        return 0.0
    m = re.match(r"([\d.]+)\s*([KMG]i?B)", s)
    if not m:
        return 0.0
    v, u = float(m.group(1)), m.group(2)
    return v * {"KiB": 1 / 1024, "KB": 1 / 1000, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1000}[u]


def summarize_stats(samples: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    per: dict[str, dict[str, list[float]]] = {}
    for s in samples:
        for c in s["stats"]:
            d = per.setdefault(c["name"], {"cpu": [], "mem": []})
            d["cpu"].append(_pct(c.get("cpu")))
            d["mem"].append(_mem_mb(c.get("mem")))
    out = {}
    for name, d in sorted(per.items()):
        if d["cpu"]:
            out[name] = {"cpu_avg_pct": round(sum(d["cpu"]) / len(d["cpu"]), 1), "cpu_max_pct": round(max(d["cpu"]), 1),
                         "mem_max_mb": round(max(d["mem"]), 0), "samples": len(d["cpu"])}
    return out


def run_insert_workers(stack_key: str, target_name: str, upper: int, workers: int, seconds: float, batch: int, run_index: int = 0, mode: str = "values") -> dict[str, Any]:
    ctx = mp.get_context("fork")
    q: mp.Queue = ctx.Queue()
    start_at = time.time() + min(5.0, 0.05 * workers + 1.5)
    procs = [ctx.Process(target=_insert_worker, args=(stack_key, target_name, upper, seconds, batch, i, start_at, q, run_index, mode), daemon=True) for i in range(workers)]
    for p in procs:
        p.start()
    results = []
    deadline = start_at + seconds + 120
    while len(results) < workers and time.time() < deadline:
        try:
            results.append(q.get(timeout=1.0))
        except Exception:  # noqa: BLE001
            pass
    for p in procs:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()
    lat = [x for r in results for x in r["lat"]]
    rows = sum(r["rows"] for r in results)
    return {"workers": workers, "target": target_name, "mode": mode, "rows": rows, "batches": len(lat), "errors": sum(r["errors"] for r in results),
            "seconds": seconds, "rows_per_s": round(rows / seconds), "batch_ms": summarize(lat),
            "first_error": next((r["first_error"] for r in results if r.get("first_error")), None), "workers_reported": len(results)}


def run_loadtest(engine: Engine, cfg: StackConfig, scale: float, *, log=print, targets: list[str] | None = None,
                 workers_list: list[int] | None = None, seconds: float | None = None, batch: int | None = None) -> dict[str, Any]:
    sz = Sizes.for_scale(scale)
    f = cfg.features
    targets = targets or f.get("loadtest_targets") or [cfg.primary.name] + [p.name for p in cfg.proxies]
    workers_list = workers_list or f.get("loadtest_workers") or [4, 16, 32]
    seconds = seconds or float(f.get("loadtest_seconds", 15))
    batch = batch or int(f.get("loadtest_batch", 1000))
    if engine.dialect.name == "tsql":
        batch = min(batch, 300)          # 2100-parameter limit
    containers = sorted({t.container for t in cfg.targets if t.container} | set(cfg.raw.get("stats_containers", [])))
    out: dict[str, Any] = {"batch_rows": batch, "seconds_per_run": seconds, "containers": containers, "runs": []}
    run_index = int(time.time()) % 1000          # keeps id ranges unique across repeated phase runs
    modes = f.get("loadtest_modes") or ["values"]
    if engine.driver not in ("psycopg",):
        modes = [m for m in modes if m != "copy"]
    for mode in modes:
      for tn in targets:
        t = cfg.by_name(tn)
        if t is None:
            log(f"  unknown target {tn}")
            continue
        for w in workers_list:
            paths = dockerctl.cgroup_paths(containers)
            # reset memory.peak is not possible without root; report end-of-run current + peak since start
            before = dockerctl.cgroup_snapshot(paths)
            run_index += 1
            t_run0 = time.time()
            r = run_insert_workers(cfg.key, tn, sz.customers, w, seconds, batch, run_index, mode)
            after = dockerctl.cgroup_snapshot(paths)
            # the workers only insert during `seconds` inside a longer window; scale CPU to the insert window
            window = after[next(iter(after))]["t"] - before[next(iter(before))]["t"] if before and after else seconds
            r["container_usage"] = dockerctl.cgroup_delta(before, after)
            for v in r["container_usage"].values():
                v["cpu_cores_avg_during_inserts"] = round(v["cpu_cores_avg"] * window / seconds, 3)
                if v.get("cpu_limit_cores"):
                    v["cpu_pct_of_limit_during_inserts"] = round(100 * v["cpu_cores_avg_during_inserts"] / v["cpu_limit_cores"], 1)
            busiest = sorted(r["container_usage"].items(), key=lambda kv: -kv[1]["cpu_cores_avg_during_inserts"])[:4]
            out["runs"].append(r)
            log(f"  {mode:6s} {tn:12s} workers={w:3d} rows/s={r['rows_per_s']:>9,} batch p50={r['batch_ms'].get('p50', 0):7.1f} ms p95={r['batch_ms'].get('p95', 0):7.1f} ms "
                f"errors={r['errors']} | busiest: " + ", ".join(f"{n.replace('rdlab-', '')} {s['cpu_cores_avg_during_inserts']:.2f} cores" + (f" ({s['cpu_pct_of_limit_during_inserts']}% of limit)" if s.get('cpu_pct_of_limit_during_inserts') is not None else "") for n, s in busiest))
            if r["first_error"]:
                log(f"    first error: {r['first_error'][:120]}")
    # cleanup
    c = engine.connect_primary()
    try:
        try:
            c.execute("DELETE FROM events WHERE event_type = 'loadtest'", rendered=True, fetch=False)
        except Exception:  # noqa: BLE001
            try:
                c.execute("ALTER TABLE events DELETE WHERE event_type = 'loadtest'", rendered=True, fetch=False)
            except Exception:  # noqa: BLE001
                pass
    finally:
        c.close()
    best = max(out["runs"], key=lambda r: r["rows_per_s"], default=None)
    if best:
        out["best"] = {"target": best["target"], "mode": best.get("mode"), "workers": best["workers"], "rows_per_s": best["rows_per_s"]}
    return out
