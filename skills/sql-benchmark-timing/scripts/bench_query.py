#!/usr/bin/env python3
"""Time one SQL statement against a running rd-databases stack.

Usage: uv run --project harness python skills/sql-benchmark-timing/scripts/bench_query.py <stack> "<sql>" [--iters 20] [--warmup 3] [--target primary] [param ...]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness"))
from rdlab.config import load_stack            # noqa: E402
from rdlab.engines import make_engine         # noqa: E402
from rdlab.timing import measure              # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stack")
    ap.add_argument("sql")
    ap.add_argument("params", nargs="*")
    ap.add_argument("--iters", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--target", default=None)
    a = ap.parse_args()
    cfg = load_stack(a.stack)
    eng = make_engine(cfg)
    target = cfg.by_name(a.target) if a.target else cfg.primary
    conn = eng.connect(target)
    params = tuple(_coerce(p) for p in a.params) or None
    try:
        t = measure("q", lambda: conn.execute(a.sql, params), warmup=a.warmup, iters=a.iters, max_seconds=60)
        if not t.ok:
            print("ERROR", t.error)
            sys.exit(2)
        s = t.stats
        print(f"{cfg.display} via {target.name}: n={t.n} first={t.first_ms:.3f} ms  p50={s['p50']:.3f}  p95={s['p95']:.3f}  p99={s['p99']:.3f}  max={s['max']:.3f} ms  rows={t.rows} checksum={t.checksum}")
    finally:
        conn.close()


def _coerce(v: str):
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


if __name__ == "__main__":
    main()
