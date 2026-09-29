"""Phase: connection behaviour — connect latency, connection storms, concurrency scaling,
pool saturation, transaction conflicts/deadlocks, proxy read/write split."""
from __future__ import annotations

import queue
import random
import threading
import time
from typing import Any

from ..config import StackConfig, Target
from ..datagen import Sizes
from ..engines import Engine
from ..util import short_err, summarize

PING = {"oracle": "SELECT 1 FROM DUAL", "db2": "SELECT 1 FROM SYSIBM.SYSDUMMY1", "firebird": "SELECT 1 FROM RDB$DATABASE", "mysql": "SELECT 1"}


def _ping_sql(engine: Engine) -> str:
    return PING.get(engine.dialect.name, "SELECT 1")


def connect_latency(engine: Engine, target: Target, n: int = 30) -> dict[str, Any]:
    samples, errors = [], []
    ping = _ping_sql(engine)
    for _ in range(n):
        t0 = time.perf_counter_ns()
        try:
            c = engine.connect(target, timeout=10)
            c.execute(ping, rendered=True)
            c.close()
            samples.append((time.perf_counter_ns() - t0) / 1e6)
        except Exception as e:  # noqa: BLE001
            errors.append(short_err(e))
    return {"target": target.name, "n": n, "ms": summarize(samples), "errors": errors[:3], "error_count": len(errors)}


def connection_storm(engine: Engine, target: Target, n: int, hold_seconds: float = 1.0) -> dict[str, Any]:
    """Open n connections concurrently, hold them all open, then close. Records connect-time
    percentiles, failures and their error codes."""
    ping = _ping_sql(engine)
    results: list[tuple[float | None, str | None, str | None]] = [(None, None, None)] * n
    conns: list[Any] = [None] * n
    start = threading.Barrier(n + 1)
    done = threading.Event()

    def worker(i: int):
        try:
            start.wait(timeout=30)
        except threading.BrokenBarrierError:
            return
        t0 = time.perf_counter_ns()
        try:
            c = engine.connect(target, timeout=30)
            c.execute(ping, rendered=True)
            conns[i] = c
            results[i] = ((time.perf_counter_ns() - t0) / 1e6, None, None)
        except Exception as e:  # noqa: BLE001
            results[i] = (None, short_err(e), engine.error_code(e))
        done.wait(timeout=hold_seconds + 60)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    t_all = time.perf_counter()
    start.wait(timeout=30)
    # wait until every worker has produced a result
    deadline = time.time() + 90
    while time.time() < deadline and any(r == (None, None, None) for r in results):
        time.sleep(0.05)
    total_ms = (time.perf_counter() - t_all) * 1000
    time.sleep(hold_seconds)
    done.set()
    for c in conns:
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    for t in threads:
        t.join(timeout=5)
    ok = [r[0] for r in results if r[0] is not None]
    errs = [r for r in results if r[1] is not None]
    codes: dict[str, int] = {}
    for _, msg, code in errs:
        k = code or (msg or "?")[:60]
        codes[k] = codes.get(k, 0) + 1
    return {"target": target.name, "requested": n, "opened": len(ok), "failed": len(errs), "wall_ms_to_open_all": round(total_ms, 1),
            "connect_ms": summarize(ok), "error_codes": codes, "sample_error": errs[0][1] if errs else None}


def concurrency_scaling(engine: Engine, target: Target, sz: Sizes, workers_list: list[int], seconds: float = 4.0) -> list[dict[str, Any]]:
    """W worker processes, each with its own connection, run PK lookups for `seconds`. Reports QPS and latency."""
    from ..workers import run_workers
    sql = "SELECT id, name, email FROM customers WHERE id = ?"
    out = []
    for w in workers_list:
        try:
            out.append(run_workers(engine.cfg.key, [target.name], sql, sz.customers, w, seconds))
        except Exception as e:  # noqa: BLE001
            out.append({"workers": w, "error": short_err(e)})
    return out


def pool_saturation(engine: Engine, target: Target, sz: Sizes, pool_size: int = 8, workers: int = 32, seconds: float = 4.0) -> dict[str, Any]:
    """A fixed pool of `pool_size` connections shared by `workers` threads: measures queue wait time."""
    sql = "SELECT id, name FROM customers WHERE id = ?"
    q: queue.Queue = queue.Queue()
    for _ in range(pool_size):
        q.put(engine.connect(target, timeout=15))
    waits: list[list[float]] = [[] for _ in range(workers)]
    execs: list[list[float]] = [[] for _ in range(workers)]
    stop = time.perf_counter() + seconds

    def run(i: int):
        r = random.Random(100 + i)
        while time.perf_counter() < stop:
            t0 = time.perf_counter_ns()
            c = q.get()
            t1 = time.perf_counter_ns()
            try:
                c.execute(sql, (r.randint(1, sz.customers),))
            except Exception:  # noqa: BLE001
                pass
            finally:
                q.put(c)
            t2 = time.perf_counter_ns()
            waits[i].append((t1 - t0) / 1e6)
            execs[i].append((t2 - t1) / 1e6)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=seconds + 30)
    elapsed = time.perf_counter() - t0
    while not q.empty():
        try:
            q.get_nowait().close()
        except Exception:  # noqa: BLE001
            pass
    all_w = [x for l in waits for x in l]
    all_e = [x for l in execs for x in l]
    return {"pool_size": pool_size, "workers": workers, "queries": len(all_e), "qps": round(len(all_e) / elapsed) if elapsed else 0,
            "checkout_wait_ms": summarize(all_w), "exec_ms": summarize(all_e)}


class _SkipDeadlock(Exception):
    pass


def transaction_conflicts(engine: Engine, target: Target) -> dict[str, Any]:
    """(a) write-write conflict under REPEATABLE READ / SERIALIZABLE; (b) deadlock detection time."""
    d = engine.dialect
    out: dict[str, Any] = {}
    if not d.supports_transactions:
        return {"status": "n/a", "note": "engine has no transactional isolation"}
    setup = ["CREATE TABLE conflict_lab (id INT NOT NULL, balance INT NOT NULL, PRIMARY KEY (id))",
             "INSERT INTO conflict_lab (id, balance) VALUES (1, 100)", "INSERT INTO conflict_lab (id, balance) VALUES (2, 100)"]
    iso_sql = {"postgres": "SET TRANSACTION ISOLATION LEVEL {lvl}", "mysql": "SET TRANSACTION ISOLATION LEVEL {lvl}",
               "tsql": "SET TRANSACTION ISOLATION LEVEL {lvl}", "oracle": "SET TRANSACTION ISOLATION LEVEL {lvl}",
               "db2": None, "sqlite": None, "duckdb": None, "firebird": None, "h2": "SET TRANSACTION ISOLATION LEVEL {lvl}",
               "monetdb": "SET TRANSACTION ISOLATION LEVEL {lvl}"}
    fam = d.family
    admin = engine.connect(target)
    try:
        for s in ("DROP TABLE conflict_lab",):
            try:
                admin.execute(s, rendered=True, fetch=False)
            except Exception:  # noqa: BLE001
                pass
        for s in setup:
            admin.execute(s, rendered=True, fetch=False)
        for lvl in ("REPEATABLE READ", "SERIALIZABLE"):
            rec: dict[str, Any] = {}
            c1 = c2 = None
            try:
                c1 = engine.connect(target, autocommit=False)
                c2 = engine.connect(target, autocommit=False)
                tpl = iso_sql.get(fam)
                if tpl:
                    for c in (c1, c2):
                        try:
                            c.execute(tpl.format(lvl=lvl), rendered=True, fetch=False)
                        except Exception as e:  # noqa: BLE001
                            rec["set_isolation_error"] = short_err(e)
                c1.execute("SELECT balance FROM conflict_lab WHERE id = 1", rendered=True)      # T1 reads
                t2_res: dict[str, Any] = {}

                def t2():
                    t0 = time.perf_counter()
                    try:
                        c2.execute("UPDATE conflict_lab SET balance = balance - 10 WHERE id = 1", rendered=True, fetch=False)
                        c2.commit()
                        t2_res["outcome"] = "committed"
                    except Exception as e:  # noqa: BLE001
                        t2_res["outcome"] = "error"
                        t2_res["error_code"] = engine.error_code(e)
                        t2_res["error"] = short_err(e)
                    t2_res["ms"] = round((time.perf_counter() - t0) * 1000, 1)

                th = threading.Thread(target=t2, daemon=True)
                th.start()
                th.join(timeout=3.0)
                if th.is_alive():
                    rec["t2_update_blocked_by_t1_read"] = True      # locking reads (e.g. MySQL SERIALIZABLE)
                t0 = time.perf_counter()
                try:
                    c1.execute("UPDATE conflict_lab SET balance = balance - 10 WHERE id = 1", rendered=True, fetch=False)
                    c1.commit()
                    rec["t1_update_after_t2_commit"] = "succeeded"
                except Exception as e:  # noqa: BLE001
                    rec["t1_update_after_t2_commit"] = "rejected"
                    rec["error_code"] = engine.error_code(e)
                    rec["error"] = short_err(e)
                    c1.rollback()
                rec["ms"] = round((time.perf_counter() - t0) * 1000, 1)
                th.join(timeout=60)
                rec["t2"] = t2_res
                bal = admin.execute("SELECT balance FROM conflict_lab WHERE id = 1", rendered=True)[0][0]
                rec["final_balance"] = bal
                admin.execute("UPDATE conflict_lab SET balance = 100 WHERE id = 1", rendered=True, fetch=False)
            except Exception as e:  # noqa: BLE001
                rec["error"] = short_err(e)
            finally:
                for c in (c1, c2):
                    if c:
                        try:
                            c.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                        c.close()
            out[f"write_conflict_{lvl.lower().replace(' ', '_')}"] = rec
        # deadlock
        rec = {}
        c1 = c2 = None
        try:
            c1 = engine.connect(target, autocommit=False)
            c2 = engine.connect(target, autocommit=False)
            c1.execute("UPDATE conflict_lab SET balance = balance - 1 WHERE id = 1", rendered=True, fetch=False)
            try:
                c2.execute("UPDATE conflict_lab SET balance = balance - 1 WHERE id = 2", rendered=True, fetch=False)
            except Exception as e:  # noqa: BLE001
                rec["detected"] = False
                rec["note"] = "second writer could not even start (single-writer engine: table/database-level write lock)"
                rec["error"] = short_err(e)
                rec["error_code"] = engine.error_code(e)
                raise _SkipDeadlock()
            res: dict[str, Any] = {}

            def t1():
                t0 = time.perf_counter()
                try:
                    c1.execute("UPDATE conflict_lab SET balance = balance - 1 WHERE id = 2", rendered=True, fetch=False)
                    res["t1"] = ("ok", None, time.perf_counter() - t0)
                except Exception as e:  # noqa: BLE001
                    res["t1"] = ("error", engine.error_code(e) or short_err(e), time.perf_counter() - t0)

            def t2():
                t0 = time.perf_counter()
                try:
                    c2.execute("UPDATE conflict_lab SET balance = balance - 1 WHERE id = 1", rendered=True, fetch=False)
                    res["t2"] = ("ok", None, time.perf_counter() - t0)
                except Exception as e:  # noqa: BLE001
                    res["t2"] = ("error", engine.error_code(e) or short_err(e), time.perf_counter() - t0)

            th = threading.Thread(target=t1, daemon=True)
            th.start()
            time.sleep(0.3)
            th2 = threading.Thread(target=t2, daemon=True)
            th2.start()
            th.join(timeout=30)
            th2.join(timeout=30)
            if th.is_alive() or th2.is_alive():
                # no deadlock detection / no lock timeout within 30 s: break the cycle by rolling back T1
                rec["timed_out_after_s"] = 30
                try:
                    c1.rollback()
                except Exception:  # noqa: BLE001
                    pass
                th.join(timeout=30)
                th2.join(timeout=30)
            victim = [k for k, v in res.items() if v[0] == "error"]
            rec["detected"] = bool(victim)
            rec["victim"] = victim
            rec["error_codes"] = {k: v[1] for k, v in res.items() if v[0] == "error"}
            rec["detection_ms"] = round(min(v[2] for v in res.values() if v[0] == "error") * 1000, 1) if victim else None
            rec["timeline_s"] = {k: round(v[2], 3) for k, v in res.items()}
        except _SkipDeadlock:
            pass
        except Exception as e:  # noqa: BLE001
            rec["error"] = short_err(e)
        finally:
            for c in (c1, c2):
                if c:
                    try:
                        c.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    c.close()
        out["deadlock"] = rec
        try:
            admin.execute("DROP TABLE conflict_lab", rendered=True, fetch=False)
        except Exception:  # noqa: BLE001
            pass
    finally:
        admin.close()
    return out


IDENTITY_SQL = {"postgres": "SELECT inet_server_addr()::text, pg_is_in_recovery()::text", "mysql": "SELECT @@server_id, @@read_only",
                "cockroach": "SELECT node_id::text, 'false' FROM [SHOW node_id]"}


def rw_split(engine: Engine, proxy: Target, sz: Sizes, n: int = 40) -> dict[str, Any]:
    """Through a load-balancing proxy: which backend answers reads, and do writes succeed?
    (A TCP round-robin proxy cannot route by statement type; a SQL-aware proxy can.)"""
    fam = engine.dialect.family
    sql = IDENTITY_SQL.get(engine.dialect.name) or IDENTITY_SQL.get(fam)
    if not sql:
        return {"status": "n/a"}
    reads: dict[str, int] = {}
    writes_ok: dict[str, int] = {}
    writes_rejected: dict[str, int] = {}
    codes: dict[str, int] = {}
    for i in range(n):
        try:
            c = engine.connect(proxy, timeout=10)
        except Exception as e:  # noqa: BLE001
            codes[f"connect: {short_err(e)[:60]}"] = codes.get(f"connect: {short_err(e)[:60]}", 0) + 1
            continue
        try:
            r = c.execute(sql, rendered=True)[0]
            key = f"{r[0]}/{r[1]}"
            reads[key] = reads.get(key, 0) + 1
            try:
                c.execute("UPDATE customers SET tier = tier WHERE id = 1", rendered=True, fetch=False)
                writes_ok[key] = writes_ok.get(key, 0) + 1
            except Exception as e:  # noqa: BLE001
                writes_rejected[key] = writes_rejected.get(key, 0) + 1
                code = engine.error_code(e) or short_err(e)[:60]
                codes[code] = codes.get(code, 0) + 1
                try:
                    c.rollback()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            c.close()
    n_ok = sum(writes_ok.values())
    n_rej = sum(writes_rejected.values())
    ro_reads = sum(v for k, v in reads.items() if k.split("/")[-1].lower() in ("1", "true", "t"))
    if n_rej == 0 and n_ok and ro_reads:
        interp = f"SQL-aware read/write split: {ro_reads}/{n} reads were served by a read-only replica while every write succeeded (routed to the primary)"
    elif n_rej == 0 and n_ok and len(reads) > 1:
        interp = "reads spread over several writable backends and every write succeeded (multi-active cluster behind a TCP balancer)"
    elif n_rej == 0 and n_ok:
        interp = "all traffic served by one writable backend (no read scaling through this proxy)"
    elif n_ok and n_rej:
        interp = f"TCP-level balancing without SQL awareness: {n_rej}/{n} writes failed because the connection landed on a read-only replica"
    else:
        interp = "no successful writes through the proxy"
    return {"proxy": proxy.name, "connections": n, "reads_by_backend": reads, "writes_ok": n_ok, "writes_rejected": n_rej,
            "writes_rejected_on_backend": writes_rejected, "write_error_codes": codes, "interpretation": interp}


def run_connections(engine: Engine, cfg: StackConfig, scale: float, *, log=print, storms: list[int] | None = None) -> dict[str, Any]:
    sz = Sizes.for_scale(scale)
    out: dict[str, Any] = {}
    primary = cfg.primary
    c = engine.connect_primary()
    try:
        out["max_connections"] = engine.max_connections(c)
    finally:
        c.close()
    log(f"  max_connections={out['max_connections']}")
    targets = [primary] + cfg.proxies
    out["connect_latency"] = {}
    for t in targets:
        r = connect_latency(engine, t)
        out["connect_latency"][t.name] = r
        log(f"  connect latency via {t.name:10s} p50={r['ms'].get('p50', 0):.2f} ms p95={r['ms'].get('p95', 0):.2f} ms errors={r['error_count']}")
    if cfg.features.get("connection_storm", True):
        storm_sizes = storms or [10, 50, 100, 200]
        mx = out["max_connections"]
        if mx and 0 < mx < 5000:
            storm_sizes = [s for s in storm_sizes if s < mx] + [mx + 10]
        out["storm"] = {}
        for t in targets:
            out["storm"][t.name] = []
            for n in storm_sizes:
                if t.role == "proxy" and n > 500:
                    continue
                r = connection_storm(engine, t, n)
                out["storm"][t.name].append(r)
                log(f"  storm via {t.name:10s} n={n:4d} opened={r['opened']:4d} failed={r['failed']:3d} wall={r['wall_ms_to_open_all']:8.1f} ms "
                    f"connect p95={r['connect_ms'].get('p95', 0):.1f} ms codes={r['error_codes']}")
    out["concurrency"] = {}
    for t in targets:
        r = concurrency_scaling(engine, t, sz, [1, 4, 16, 32, 64])
        out["concurrency"][t.name] = r
        for x in r:
            if "qps" in x:
                log(f"  workers={x['workers']:3d} via {t.name:10s} qps={x['qps']:7d} p50={x['latency_ms'].get('p50', 0):.3f} ms p99={x['latency_ms'].get('p99', 0):.3f} ms errors={x['errors']} {x.get('first_error') or ''}")
            else:
                log(f"  workers={x['workers']:3d} via {t.name:10s} ERR {x['error'][:80]}")
    try:
        r = pool_saturation(engine, primary, sz)
        out["pool_saturation"] = r
        log(f"  pool 8 conns / 32 workers: qps={r['qps']} checkout wait p50={r['checkout_wait_ms'].get('p50', 0):.3f} ms p99={r['checkout_wait_ms'].get('p99', 0):.3f} ms")
    except Exception as e:  # noqa: BLE001
        out["pool_saturation"] = {"error": short_err(e)}
    try:
        out["transactions"] = transaction_conflicts(engine, primary)
        for k, v in out["transactions"].items():
            log(f"  {k}: {str({kk: vv for kk, vv in v.items() if kk not in ('error',)})[:160]}" if isinstance(v, dict) else f"  {k}: {v}")
    except Exception as e:  # noqa: BLE001
        out["transactions"] = {"error": short_err(e)}
    for p in cfg.proxies:
        if p.extra.get("splits_reads"):
            out.setdefault("rw_split", {})[p.name] = rw_split(engine, p, sz)
            log(f"  rw split via {p.name}: {out['rw_split'][p.name]}")
    return out
