"""Phase: logging — effective DB logging config, slow-query capture proof, audit trail proof, the throughput
cost of logging every statement, Docker log-driver settings, and a copy of the engine's log files."""
from __future__ import annotations

import time
from typing import Any

from .. import dockerctl
from ..config import StackConfig
from ..datagen import Sizes
from ..dblogs import collect_logs, marker, probe_for, wait_found
from ..engines import Engine
from ..util import RESULTS, short_err
from ..workers import run_workers

WORKLOAD_SQL = "SELECT id, name, email FROM customers WHERE id = ?"


def run_dblogging(engine: Engine, cfg: StackConfig, scale: float, *, log=print) -> dict[str, Any]:
    out: dict[str, Any] = {}
    probe = probe_for(engine, cfg, log=log)
    if probe is None:
        out["status"] = "n/a"
        log("  no logging probe implemented for this dialect")
        return out
    out["sink"] = probe.sink
    out["slow_threshold_ms"] = probe.slow_threshold_ms
    # 1. effective settings
    try:
        out["settings"] = probe.settings()
        log(f"  settings: {str(out['settings'])[:200]}")
    except Exception as e:  # noqa: BLE001
        out["settings"] = {"error": short_err(e)}
    # 2. slow query capture
    sq: dict[str, Any] = {"threshold_ms": probe.slow_threshold_ms, "sink": probe.sink}
    try:
        mk_fast, mk_slow = marker(), marker()
        probe.run_query(probe.fast_query(mk_fast))
        t0 = time.perf_counter()
        probe.run_query(probe.slow_query(mk_slow))
        sq["slow_query_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        found, secs = wait_found(lambda: probe.find_slow(mk_slow))
        sq["slow_logged"] = bool(found.get("found"))
        sq["seen_after_s"] = round(secs, 2)
        sq["sample"] = (found.get("sample") or "")[:600]
        sq.update({k: v for k, v in found.items() if k not in ("found", "sample")})
        fast = probe.find_slow(mk_fast)
        sq["fast_logged"] = bool(fast.get("found"))
        log(f"  slow query ({sq['slow_query_ms']} ms) logged={sq['slow_logged']} after {sq['seen_after_s']}s; fast query logged={sq['fast_logged']}")
    except Exception as e:  # noqa: BLE001
        sq["error"] = short_err(e)
        log(f"  slow query test ERR {short_err(e)}")
    out["slow_query"] = sq
    # 3. audit
    au: dict[str, Any] = {"mechanism": probe.audit_mechanism}
    try:
        desc = probe.audit_setup()
        if desc is None:
            au["status"] = "n/a"
            log("  audit: not available in this engine/edition")
        else:
            au["setup"] = desc
            mk = marker()
            for s in probe.audit_actions(mk):
                probe.q(s, fetch=False)
            found, secs = wait_found(lambda: probe.audit_find(mk), timeout=20)
            au["event_found"] = bool(found.get("found"))
            au["seen_after_s"] = round(secs, 2)
            au["sample"] = (found.get("sample") or "")[:600]
            au.update({k: v for k, v in found.items() if k not in ("found", "sample")})
            au["status"] = "ok" if au["event_found"] else "not-found"
            log(f"  audit ({probe.audit_mechanism}): event found={au['event_found']} after {au['seen_after_s']}s")
    except Exception as e:  # noqa: BLE001
        au["status"] = "error"
        au["error"] = short_err(e)
        log(f"  audit test ERR {short_err(e)}")
    finally:
        try:
            probe.audit_teardown()
        except Exception:  # noqa: BLE001
            pass
    out["audit"] = au
    # 4. overhead of logging everything
    ov: dict[str, Any] = {"workload": WORKLOAD_SQL, "workers": 8, "seconds": 4}
    try:
        sz = Sizes.for_scale(scale)
        tgt = [cfg.primary.name]
        run_workers(cfg.key, tgt, WORKLOAD_SQL, sz.customers, 8, 2, probe.workload_session_sql(False))     # warm-up, discarded
        base = run_workers(cfg.key, tgt, WORKLOAD_SQL, sz.customers, 8, 4, probe.workload_session_sql(False))
        ov["baseline"] = {"qps": base["qps"], "p50_ms": base["latency_ms"].get("p50"), "p99_ms": base["latency_ms"].get("p99"), "errors": base["errors"]}
        desc = probe.full_logging(True)
        if desc is None and probe.workload_session_sql(True) is None:
            ov["status"] = "n/a"
            log(f"  overhead: baseline {base['qps']} qps; engine has no 'log every statement' switch")
        else:
            ov["full_logging"] = desc or probe.workload_session_sql(True)
            time.sleep(probe.settle_s)
            b0 = probe.log_bytes()
            full = run_workers(cfg.key, tgt, WORKLOAD_SQL, sz.customers, 8, 4, probe.workload_session_sql(True))
            time.sleep(1.0)
            b1 = probe.log_bytes()
            ov["full"] = {"qps": full["qps"], "p50_ms": full["latency_ms"].get("p50"), "p99_ms": full["latency_ms"].get("p99"), "errors": full["errors"]}
            ov["log_bytes_written"] = max(0, b1 - b0)
            ov["bytes_per_statement"] = round((b1 - b0) / full["queries"], 1) if full["queries"] and b1 >= b0 else None
            ov["overhead_pct"] = round(100 * (1 - full["qps"] / base["qps"]), 1) if base["qps"] else None
            ov["status"] = "ok"
            log(f"  overhead of logging every statement: {base['qps']} -> {full['qps']} qps ({ov['overhead_pct']} %), "
                f"{ov['log_bytes_written']:,} log bytes for {full['queries']:,} statements")
    except Exception as e:  # noqa: BLE001
        ov["status"] = "error"
        ov["error"] = short_err(e)
        log(f"  overhead test ERR {short_err(e)}")
    finally:
        try:
            probe.full_logging(False)
        except Exception as e:  # noqa: BLE001
            log(f"  could not switch full logging off: {short_err(e)}")
    out["overhead"] = ov
    # 5. docker log driver per container + DB log files
    cl: dict[str, Any] = {}
    for t in cfg.targets:
        if t.container and t.container not in cl:
            cl[t.container] = dockerctl.log_config(t.container)
    out["container_logging"] = cl
    try:
        out["extra"] = probe.extra_report()
    except Exception as e:  # noqa: BLE001
        out["extra"] = {"error": short_err(e)}
    try:
        dest = RESULTS / cfg.key / "logs"
        files = collect_logs(probe, dest)
        out["collected"] = [str(f).replace(str(RESULTS.parent) + "/", "") for f in files]
        log(f"  collected {len(files)} log file(s) into {dest.relative_to(RESULTS.parent)}")
    except Exception as e:  # noqa: BLE001
        out["collected_error"] = short_err(e)
    return out
