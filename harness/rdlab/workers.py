"""Multi-process query workers (Python threads cannot drive a database past a few thousand QPS
because of the GIL; each worker here is a separate process with its own connection)."""
from __future__ import annotations

import multiprocessing as mp
import random
import time
from typing import Any

from .util import summarize


def _worker(stack_key: str, target_name: str, sql: str, upper: int, seconds: float, seed: int, start_at: float, q: mp.Queue,
            session_sql: str | None = None) -> None:
    from .config import load_stack
    from .engines import make_engine
    cfg = load_stack(stack_key)
    eng = make_engine(cfg)
    t = cfg.by_name(target_name) or cfg.primary
    lat: list[float] = []
    errors = 0
    first_err = None
    try:
        c = eng.connect(t, timeout=20)
    except Exception as e:  # noqa: BLE001
        q.put({"seed": seed, "lat": [], "errors": 1, "first_error": f"connect: {e}"[:200]})
        return
    for ss in (t.extra.get("session_sql"), session_sql):
        if not ss:
            continue
        try:
            c.execute(ss, rendered=True, fetch=False)
        except Exception as e:  # noqa: BLE001
            q.put({"seed": seed, "lat": [], "errors": 1, "first_error": f"session_sql: {e}"[:200]})
            return
    r = random.Random(seed)
    while time.time() < start_at:
        time.sleep(0.001)
    stop = time.perf_counter() + seconds
    while time.perf_counter() < stop:
        t0 = time.perf_counter_ns()
        try:
            c.execute(sql, (r.randint(1, upper),))
            lat.append((time.perf_counter_ns() - t0) / 1e6)
        except Exception as e:  # noqa: BLE001
            errors += 1
            if first_err is None:
                first_err = str(e)[:200]
            if errors > 50:
                break
    try:
        c.close()
    except Exception:  # noqa: BLE001
        pass
    q.put({"seed": seed, "lat": lat, "errors": errors, "first_error": first_err})


def _thread_workers(stack_key: str, target_names: list[str], sql: str, upper: int, workers: int, seconds: float,
                    session_sql: str | None = None) -> dict[str, Any]:
    """Embedded engines (one process owns the database file): threads with one cursor/connection each."""
    import threading
    from .config import load_stack
    from .engines import make_engine
    cfg = load_stack(stack_key)
    eng = make_engine(cfg)
    lat: list[list[float]] = [[] for _ in range(workers)]
    errors = [0] * workers
    first_err: list[str | None] = [None]
    conns = [eng.connect(cfg.by_name(target_names[i % len(target_names)]) or cfg.primary) for i in range(workers)]
    if session_sql:
        for c in conns:
            c.execute(session_sql, rendered=True, fetch=False)
    barrier = threading.Barrier(workers)
    stop_at = [0.0]

    def run(i: int):
        c = conns[i]
        r = random.Random(i)
        barrier.wait()
        if i == 0:
            stop_at[0] = time.perf_counter() + seconds
        while stop_at[0] == 0.0:
            time.sleep(0.0005)
        while time.perf_counter() < stop_at[0]:
            t0 = time.perf_counter_ns()
            try:
                c.execute(sql, (r.randint(1, upper),))
                lat[i].append((time.perf_counter_ns() - t0) / 1e6)
            except Exception as e:  # noqa: BLE001
                errors[i] += 1
                first_err[0] = first_err[0] or str(e)[:200]
                if errors[i] > 50:
                    break

    ths = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(workers)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=seconds + 60)
    for c in conns:
        c.close()
    flat = [x for l in lat for x in l]
    return {"workers": workers, "targets": target_names, "queries": len(flat), "errors": sum(errors), "seconds": seconds,
            "qps": round(len(flat) / seconds) if seconds else 0, "latency_ms": summarize(flat), "first_error": first_err[0],
            "workers_reported": workers, "mode": "threads (embedded engine)"}


def run_workers(stack_key: str, target_names: list[str], sql: str, upper: int, workers: int, seconds: float,
                session_sql: str | None = None) -> dict[str, Any]:
    """Spawn `workers` processes spread round-robin over `target_names`; return aggregate QPS/latency.
    `session_sql` (optional) runs once per connection before the loop (e.g. per-session logging settings)."""
    from .config import load_stack
    if load_stack(stack_key).driver in ("duckdb",):          # single-process engine; sqlite3 supports multi-process (WAL)
        return _thread_workers(stack_key, target_names, sql, upper, workers, seconds, session_sql)
    ctx = mp.get_context("fork")
    q: mp.Queue = ctx.Queue()
    start_at = time.time() + min(3.0, 0.05 * workers + 1.0)
    procs = [ctx.Process(target=_worker, args=(stack_key, target_names[i % len(target_names)], sql, upper, seconds, i, start_at, q, session_sql), daemon=True)
             for i in range(workers)]
    for p in procs:
        p.start()
    results = []
    deadline = time.time() + (start_at - time.time()) + seconds + 60
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
    errors = sum(r["errors"] for r in results)
    first_err = next((r["first_error"] for r in results if r.get("first_error")), None)
    return {"workers": workers, "targets": target_names, "queries": len(lat), "errors": errors, "seconds": seconds,
            "qps": round(len(lat) / seconds) if seconds else 0, "latency_ms": summarize(lat), "first_error": first_err,
            "workers_reported": len(results)}
