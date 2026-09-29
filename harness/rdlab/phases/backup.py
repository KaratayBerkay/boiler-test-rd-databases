"""Phase: backup & recovery — run every backup strategy the engine supports as a full drill
(backup -> size -> restore elsewhere -> fingerprint check -> point-in-time test where the engine can)."""
from __future__ import annotations

import time
from typing import Any

from ..backup import Ctx, run_strategy, strategies_for
from ..config import StackConfig
from ..engines import Engine
from ..util import short_err


def run_backup(engine: Engine, cfg: StackConfig, scale: float, *, log=print) -> dict[str, Any]:
    ctx = Ctx(engine, cfg, log=log)
    out: dict[str, Any] = {"backup_dir": ctx.backup_dir, "strategies": {}, "order": []}
    strategies = strategies_for(engine, cfg)
    if not strategies:
        out["status"] = "n/a"
        log("  no backup strategies implemented for this dialect")
        return out
    t0 = time.perf_counter()
    ctx.source_fp = ctx.fingerprint()
    out["source_fingerprint"] = ctx.source_fp
    out["fingerprint_seconds"] = round(time.perf_counter() - t0, 2)
    rows = sum(v.get("count", 0) for v in ctx.source_fp.values() if "count" in v)
    log(f"  source fingerprint: {len(ctx.source_fp)} tables, {rows:,} rows ({out['fingerprint_seconds']}s)")
    for s in strategies:
        out["order"].append(s.name)
        out["strategies"][s.name] = run_strategy(ctx, s)
    try:
        ctx.probe_drop()
    except Exception as e:  # noqa: BLE001
        log(f"  probe cleanup: {short_err(e)}")
    ok = [k for k, v in out["strategies"].items() if v.get("status") == "ok"]
    out["summary"] = {"strategies": len(strategies), "ok": len(ok), "verified": [k for k in ok if out["strategies"][k].get("verify", {}).get("match")],
                      "pitr_ok": [k for k in ok if out["strategies"][k].get("pitr", {}).get("match")]}
    return out
