"""Phase: timed execution of the query catalog with plan capture."""
from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from ..config import StackConfig
from ..datagen import Sizes
from ..dialects import Unsupported, render
from ..engines import Engine
from ..queries import CATALOG, Query, fill_tokens
from ..timing import measure
from ..util import short_err


def resolve_sql(engine: Engine, q: Query, sz: Sizes, rng: random.Random) -> list[str] | None:
    ov = engine.dialect.query_overrides()
    sql = ov[q.id] if q.id in ov else q.sql
    if sql is None:
        return None
    stmts = sql if isinstance(sql, list) else [sql]
    return [fill_tokens(s, sz, rng) for s in stmts]


def run_query_timed(engine: Engine, conn, q: Query, stmts: list[str], params: list, *, iters: int | None = None,
                    warmup: int | None = None, max_seconds: float | None = None):
    """Time one catalog query on an open connection (params cycled per iteration)."""
    it = {"i": 0}

    def next_params():
        if not params:
            return None
        p = params[it["i"] % len(params)]
        it["i"] += 1
        return p

    if q.kind == "txn":
        def fn():
            ps = next_params() or [None] * len(stmts)
            conn.set_autocommit(False)
            try:
                for s, p in zip(stmts, ps):
                    conn.execute(s, p, fetch=False)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.set_autocommit(True)
            return None
    else:
        s0 = stmts[0]
        def fn():
            return conn.execute(s0, next_params(), fetch=True)

    return measure(q.id, fn, warmup=warmup if warmup is not None else q.warmup, iters=iters if iters is not None else q.iters,
                   max_seconds=max_seconds if max_seconds is not None else q.max_seconds)


def run_bench(engine: Engine, cfg: StackConfig, scale: float, *, plans_dir: Path | None = None, only: set[str] | None = None,
              log=print) -> dict[str, Any]:
    sz = Sizes.for_scale(scale)
    rng = random.Random(7)
    out: dict[str, Any] = {}
    conn = engine.connect_primary()
    try:
        for q in CATALOG:
            if only and q.id not in only:
                continue
            rec: dict[str, Any] = {"title": q.title, "tags": list(q.tags), "kind": q.kind}
            try:
                stmts = resolve_sql(engine, q, sz, rng)
            except Exception as e:  # noqa: BLE001
                stmts = None
                rec["error"] = short_err(e)
            if stmts is None:
                rec["status"] = "unsupported"
                rec.setdefault("error", "no syntax for this dialect")
                out[q.id] = rec
                log(f"  --  {q.id:24s} unsupported")
                continue
            try:
                rendered = [render(s, engine.dialect) for s in stmts]
            except Unsupported as e:
                rec["status"] = "unsupported"
                rec["error"] = short_err(e)
                out[q.id] = rec
                log(f"  --  {q.id:24s} unsupported ({e})")
                continue
            rec["sql"] = rendered if len(rendered) > 1 else rendered[0]
            params = q.params(sz, random.Random(11)) if q.params else []
            t = run_query_timed(engine, conn, q, stmts, params)
            rec["status"] = "ok" if t.ok else "error"
            if not t.ok and getattr(t, "exc", None) is not None and engine.is_unsupported_error(t.exc):
                rec["status"] = "unsupported"
            rec["timing"] = {"n": t.n, **{k: round(v, 3) for k, v in t.stats.items() if k != "n"}, "first_ms": round(t.first_ms, 3) if t.first_ms else None}
            rec["rows"] = t.rows
            rec["checksum"] = t.checksum
            rec["error"] = t.error
            rec["error_code"] = None
            if not t.ok:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            # plan capture (read queries only)
            if t.ok and q.kind == "read":
                p0 = params[0] if params else None
                for kind, analyze in (("plan", False), ("plan_analyze", True)):
                    try:
                        plan = conn.explain(stmts[0], p0, analyze)
                        if plan:
                            rec[kind] = plan[:6000]
                            if plans_dir:
                                plans_dir.mkdir(parents=True, exist_ok=True)
                                (plans_dir / f"{q.id}.{kind}.txt").write_text(plan)
                    except Exception as e:  # noqa: BLE001
                        rec[kind + "_error"] = short_err(e)
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
            for c in q.cleanup:
                try:
                    conn.execute(c, fetch=False)
                except Exception:  # noqa: BLE001
                    pass
            out[q.id] = rec
            if t.ok:
                log(f"  OK  {q.id:24s} p50={t.stats['p50']:9.2f} ms  p95={t.stats['p95']:9.2f} ms  rows={t.rows}")
            elif rec["status"] == "unsupported":
                log(f"  --  {q.id:24s} unsupported ({t.error[:90]})")
            else:
                log(f"  ERR {q.id:24s} {t.error[:100]}")
    finally:
        conn.close()
    return out
