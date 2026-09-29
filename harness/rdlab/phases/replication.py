"""Phase: replication / scaling — topology, visibility lag, write rejection, lag under load,
read scaling across replicas, and (optionally) failover."""
from __future__ import annotations

import random
import threading
import time
from typing import Any

from .. import dockerctl
from ..config import StackConfig, Target
from ..datagen import Sizes
from ..engines import Engine
from ..util import short_err, summarize

from ..schema import Col, Table

PROBE_TABLE = Table("repl_probe", (Col("id", "int"), Col("ts_ms", "bigint")), pk=("id",), order_hint=("id",))


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def visibility_lag(engine: Engine, primary: Target, replica: Target, n: int = 30, timeout: float = 30.0, base_id: int = 1000) -> dict[str, Any]:
    p = engine.connect(primary)
    r = engine.connect(replica)
    lags, misses = [], 0
    try:
        for i in range(n):
            t0 = _now_ms()
            p.execute("INSERT INTO repl_probe (id, ts_ms) VALUES (?, ?)", (base_id + i, t0), fetch=False)
            t_commit = time.perf_counter()
            deadline = t_commit + timeout
            seen = False
            while time.perf_counter() < deadline:
                rows = r.execute("SELECT ts_ms FROM repl_probe WHERE id = ?", (base_id + i,))
                if rows:
                    lags.append((time.perf_counter() - t_commit) * 1000)
                    seen = True
                    break
                time.sleep(0.001)
            if not seen:
                misses += 1
            time.sleep(0.05)
    finally:
        p.close()
        r.close()
    return {"replica": replica.name, "n": n, "visible": n - misses, "timeouts": misses, "lag_ms": summarize(lags)}


def write_rejection(engine: Engine, replica: Target) -> dict[str, Any]:
    c = engine.connect(replica)
    try:
        try:
            c.execute("INSERT INTO repl_probe (id, ts_ms) VALUES (?, ?)", (999999, _now_ms()), fetch=False)
            try:
                c.execute("DELETE FROM repl_probe WHERE id = 999999", fetch=False)
            except Exception:  # noqa: BLE001
                pass
            return {"replica": replica.name, "writes_rejected": False, "note": "replica accepted a write (multi-master / node-level writable)"}
        except Exception as e:  # noqa: BLE001
            return {"replica": replica.name, "writes_rejected": True, "error_code": engine.error_code(e), "error": short_err(e)}
    finally:
        c.close()


def lag_under_load(engine: Engine, cfg: StackConfig, primary: Target, replicas: list[Target], rows: int = 20000, batch: int = 500,
                   sample_every: float = 0.25) -> dict[str, Any]:
    p = engine.connect(primary)
    ps = engine.connect(primary)          # sampler needs its own connection (drivers are not thread-safe)
    stop = threading.Event()
    samples: list[dict[str, Any]] = []
    rconns = [engine.connect(r) for r in replicas]

    def sampler():
        while not stop.is_set():
            snap = {"t": round(time.perf_counter(), 3)}
            try:
                st = engine.replication_status(ps)
                snap["primary"] = _compact_status(st)
            except Exception as e:  # noqa: BLE001
                snap["primary"] = short_err(e)
            for r, rc in zip(replicas, rconns):
                try:
                    snap[r.name] = _compact_status(engine.replication_status(rc))
                except Exception as e:  # noqa: BLE001
                    snap[r.name] = short_err(e)
            samples.append(snap)
            time.sleep(sample_every)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    t0 = time.perf_counter()
    base = 5_000_000
    rng = random.Random(8)
    try:
        p.set_autocommit(False)
        for i in range(0, rows, batch):
            p.executemany("INSERT INTO repl_probe (id, ts_ms) VALUES (?, ?)", [(base + i + j, _now_ms()) for j in range(min(batch, rows - i))])
            p.commit()
        p.set_autocommit(True)
    except Exception as e:  # noqa: BLE001
        try:
            p.rollback()
            p.set_autocommit(True)
        except Exception:  # noqa: BLE001
            pass
        stop.set()
        ps.close()
        return {"error": short_err(e)}
    write_secs = time.perf_counter() - t0
    # wait for replicas to catch up
    expected = engine.count(p, "repl_probe")
    catchup = {}
    for r, rc in zip(replicas, rconns):
        t1 = time.perf_counter()
        deadline = t1 + 120
        while time.perf_counter() < deadline:
            if engine.count(rc, "repl_probe") >= expected:
                break
            time.sleep(0.02)
        catchup[r.name] = round((time.perf_counter() - t1) * 1000, 1)
    stop.set()
    th.join(timeout=5)
    for rc in rconns:
        rc.close()
    p.close()
    ps.close()
    return {"rows": rows, "batch": batch, "write_seconds": round(write_secs, 2), "rows_per_s": round(rows / write_secs) if write_secs else None,
            "catchup_ms_after_last_commit": catchup, "samples": samples[:200]}


def _compact_status(st: dict[str, Any]) -> Any:
    if not st:
        return None
    if "pg_stat_replication" in st:
        return [{"app": r[0], "state": r[2], "sync": r[3], "replay_lag_bytes": r[4], "replay_lag_s": r[7]} for r in (st.get("pg_stat_replication") or [])]
    if "wal_receiver" in st:
        w = st.get("wal_receiver")
        return {"status": w[0] if w else None, "replay_delay_s": w[4] if w else None, "last_replay_lsn": st.get("last_replay_lsn")}
    if "replica_status" in st:
        rs = st.get("replica_status") or {}
        return {k: rs.get(k) for k in ("Seconds_Behind_Source", "Seconds_Behind_Master", "Replica_IO_Running", "Replica_SQL_Running", "Slave_IO_Running", "Slave_SQL_Running") if k in rs}
    if "replicas" in st:
        return st["replicas"][:6]
    return st


def read_scaling(engine: Engine, targets: list[Target], sz: Sizes, workers: int = 32, seconds: float = 4.0) -> dict[str, Any]:
    from ..workers import run_workers
    sql = "SELECT id, name, email FROM customers WHERE id = ?"
    r = run_workers(engine.cfg.key, [t.name for t in targets], sql, sz.customers, workers, seconds)
    return {"targets": [t.name for t in targets], "workers": workers, "queries": r["queries"], "qps": r["qps"], "errors": r["errors"], "latency_ms": r["latency_ms"]}


def _with_timeout(fn, seconds: float, what: str):
    """Run fn() in a daemon thread; raise TimeoutError if it does not return in time (drivers can hang on
    half-closed sockets after a node dies)."""
    box: dict[str, Any] = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001
            box["error"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=seconds)
    if th.is_alive():
        raise TimeoutError(f"{what} did not return within {seconds}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def failover(engine: Engine, cfg: StackConfig, *, log=print) -> dict[str, Any]:
    """Stop the primary container, promote a replica (or just keep writing to another node for
    multi-node engines), measure time until writes succeed again."""
    rep = cfg.replication or {}
    primary = cfg.by_name(rep.get("write_target")) or cfg.primary
    verify = cfg.by_name(rep.get("verify_target"))
    batch = int(rep.get("write_batch", 1))
    ptable = rep.get("failover_probe_table", "repl_probe")
    kind = rep.get("kind", "none")
    out: dict[str, Any] = {"kind": kind, "killed": primary.name, "probe_table": ptable}
    if not primary.container:
        return {"status": "n/a", "note": "primary has no container to stop"}
    candidates = cfg.replicas or [t for t in cfg.targets if t.role == "node" and t is not primary]
    if not candidates:
        return {"status": "n/a", "note": "no replica / secondary node configured"}
    new_primary = cfg.by_name(rep.get("promote", {}).get("target")) or candidates[0]
    poll_target = verify or new_primary

    def probe_write(conn, base):
        if batch <= 1:
            conn.execute(f"INSERT INTO {ptable} (id, ts_ms) VALUES (?, ?)", (base, _now_ms()), fetch=False)
        else:   # multi-row insert spanning many shards: succeeds only when every shard is writable
            vals = ", ".join("(?, ?)" for _ in range(batch))
            flat = [x for i in range(batch) for x in (base + i, _now_ms())]
            conn.execute(f"INSERT INTO {ptable} (id, ts_ms) VALUES " + vals, flat, fetch=False)
    log(f"  stopping primary container {primary.container} ...")
    t_stop = time.perf_counter()
    dockerctl.container_action(primary.container, "kill")
    out["stopped_at"] = 0.0
    # confirm writes fail (on the killed node, or through the verify target for sharded clusters)
    def _probe_once(base):
        c = engine.connect(poll_target, timeout=3)
        try:
            probe_write(c, base)
        finally:
            c.close()

    try:
        _with_timeout(lambda: _probe_once(7_000_000), 20, "post-kill write probe")
        out["writes_still_succeed_after_kill"] = True
    except Exception as e:  # noqa: BLE001
        out["write_error_after_kill"] = short_err(e)
    promote_method = rep.get("promote", {}).get("method", "engine")
    if promote_method != "none":
        try:
            t_p = time.perf_counter()
            out["promote_cmd"] = _with_timeout(lambda: engine.promote(new_primary), 90, "promote")
            out["promote_ms"] = round((time.perf_counter() - t_p) * 1000, 1)
            log(f"  promoted {new_primary.name} via: {out['promote_cmd']}")
        except Exception as e:  # noqa: BLE001
            out["promote_error"] = short_err(e)
            log(f"  promote failed: {short_err(e)}")
    # poll until a write succeeds on the new primary
    deadline = time.perf_counter() + 120
    attempts, first_ok = 0, None
    last_err = None
    while time.perf_counter() < deadline:
        attempts += 1
        try:
            _with_timeout(lambda: _probe_once(7_100_000 + attempts * max(batch, 1)), 15, "write probe")
            first_ok = time.perf_counter()
            break
        except Exception as e:  # noqa: BLE001
            last_err = short_err(e)
            time.sleep(0.2)
    out["write_attempts_until_success"] = attempts
    out["last_error_before_success"] = last_err
    out["downtime_ms"] = round((first_ok - t_stop) * 1000, 1) if first_ok else None
    out["new_primary"] = new_primary.name
    out["verified_via"] = poll_target.name
    try:
        c = engine.connect(new_primary)
        out["new_primary_role"] = c.role()
        out["new_primary_status"] = engine.replication_status(c)
        c.close()
    except Exception as e:  # noqa: BLE001
        out["new_primary_status_error"] = short_err(e)
    log(f"  downtime until first successful write via {poll_target.name}: {out['downtime_ms']} ms ({attempts} attempts)")
    if rep.get("restart_old_primary", True):
        try:
            dockerctl.container_action(primary.container, "start")
            out["old_primary_restarted"] = True
            out["note"] = "old primary restarted but NOT re-joined (diverged timeline); stack should be recreated for reuse"
        except Exception as e:  # noqa: BLE001
            out["old_primary_restart_error"] = short_err(e)
    return out


def run_replication(engine: Engine, cfg: StackConfig, scale: float, *, do_failover: bool = False, log=print) -> dict[str, Any]:
    sz = Sizes.for_scale(scale)
    rep = cfg.replication or {}
    out: dict[str, Any] = {"kind": rep.get("kind", "none")}
    primary = cfg.by_name(rep.get("write_target")) or cfg.primary
    replicas = cfg.replicas
    nodes = [t for t in cfg.targets if t.role == "node"]
    read_targets = [cfg.by_name(n) for n in rep.get("read_targets", [])] if rep.get("read_targets") else (replicas or [n for n in nodes if n is not primary])
    read_targets = [t for t in read_targets if t is not None]
    # topology
    topo = {}
    for t in cfg.targets:
        try:
            c = engine.connect(t, timeout=10)
            topo[t.name] = {"role_reported": c.role(), "role_configured": t.role, "version": c.server_version()[:120], "status": engine.replication_status(c)}
            c.close()
        except Exception as e:  # noqa: BLE001
            topo[t.name] = {"error": short_err(e)}
        log(f"  {t.name:10s} role={topo[t.name].get('role_reported')} {str(topo[t.name].get('status', ''))[:120]}")
    out["topology"] = topo
    if not read_targets:
        out["status"] = "single-node"
        log("  single node stack: no replication tests")
        return out
    p = engine.connect(primary)
    try:
        for stmt in (engine.dialect.drop_table_if_exists("repl_probe"), engine.dialect.drop_table("repl_probe")):
            try:
                p.execute(stmt, rendered=True, fetch=False)
                break
            except Exception:  # noqa: BLE001
                pass
        p.execute(engine.dialect.create_table(PROBE_TABLE), rendered=True, fetch=False)
    finally:
        p.close()
    if cfg.features.get("citus"):
        # the failover probe goes through the coordinator: a *distributed* probe table (different name, because Citus
        # syncs the shell table of every distributed table to the workers and would collide with the worker-local one)
        pt = rep.get("failover_probe_table", "repl_probe_dist")
        c = engine.connect(cfg.primary)
        try:
            c.execute(f"DROP TABLE IF EXISTS {pt}", rendered=True, fetch=False)
            c.execute(engine.dialect.create_table(PROBE_TABLE).replace("repl_probe", pt), rendered=True, fetch=False)
            c.execute(f"SELECT create_distributed_table('{pt}', 'id')", rendered=True)
        finally:
            c.close()
        # (the write_target worker keeps its own plain repl_probe table created above; shards have suffixed names)
    # wait for DDL to replicate
    for r in read_targets:
        try:
            c = engine.connect(r)
            dockerctl.wait_for(lambda: c.execute("SELECT COUNT(*) FROM repl_probe", rendered=True) is not None, timeout=60, desc="probe table on replica")
            c.close()
        except Exception as e:  # noqa: BLE001
            log(f"  probe table not visible on {r.name}: {short_err(e)}")
    out["visibility_lag"] = {}
    for ri, r in enumerate(read_targets):
        try:
            v = visibility_lag(engine, primary, r, base_id=1000 + 1000 * ri)
            out["visibility_lag"][r.name] = v
            log(f"  visibility lag -> {r.name}: p50={v['lag_ms'].get('p50', 0):.2f} ms p95={v['lag_ms'].get('p95', 0):.2f} ms max={v['lag_ms'].get('max', 0):.2f} ms timeouts={v['timeouts']}")
        except Exception as e:  # noqa: BLE001
            out["visibility_lag"][r.name] = {"error": short_err(e)}
            log(f"  visibility lag -> {r.name}: ERR {short_err(e)}")
    out["write_rejection"] = {}
    for r in read_targets:
        w = write_rejection(engine, r)
        out["write_rejection"][r.name] = w
        log(f"  write on {r.name}: {'rejected ' + str(w.get('error_code')) if w['writes_rejected'] else 'ACCEPTED'}")
    try:
        l = lag_under_load(engine, cfg, primary, read_targets)
        out["lag_under_load"] = l
        log(f"  lag under load: {l.get('rows')} rows in {l.get('write_seconds')}s, catch-up after last commit: {l.get('catchup_ms_after_last_commit')}")
    except Exception as e:  # noqa: BLE001
        out["lag_under_load"] = {"error": short_err(e)}
    try:
        base_t = cfg.by_name(rep.get("read_scaling_base")) or primary
        rs_targets = [cfg.by_name(n) for n in rep.get("read_scaling_targets", [])] or ([base_t] + read_targets)
        a = read_scaling(engine, [base_t], sz)
        b = read_scaling(engine, [t for t in rs_targets if t is not None], sz)
        out["read_scaling"] = {"primary_only": a, "primary_plus_replicas": b, "gain": round(b["qps"] / a["qps"], 2) if a["qps"] else None}
        log(f"  read scaling: primary only {a['qps']} qps -> primary+replicas {b['qps']} qps (x{out['read_scaling']['gain']})")
    except Exception as e:  # noqa: BLE001
        out["read_scaling"] = {"error": short_err(e)}
    if do_failover:
        try:
            out["failover"] = failover(engine, cfg, log=log)
        except Exception as e:  # noqa: BLE001
            out["failover"] = {"error": short_err(e)}
    return out
