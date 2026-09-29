#!/usr/bin/env python3
"""Print estimated + actual plans and a 10-run timing for a query against a running rd-databases stack.

Usage: uv run --project harness python skills/sql-query-optimization/scripts/explain.py <stack> "<sql>" [param ...]
Example: uv run --project harness python skills/sql-query-optimization/scripts/explain.py postgres "SELECT * FROM orders WHERE id = ?" 42
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness"))
from rdlab.config import load_stack            # noqa: E402
from rdlab.engines import make_engine         # noqa: E402
from rdlab.timing import measure              # noqa: E402


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    stack, sql, params = sys.argv[1], sys.argv[2], tuple(_coerce(p) for p in sys.argv[3:])
    cfg = load_stack(stack)
    eng = make_engine(cfg)
    conn = eng.connect_primary()
    try:
        print(f"== {cfg.display} ({conn.server_version()[:80]})")
        for analyze in (False, True):
            try:
                plan = conn.explain(sql, params or None, analyze)
                print(f"\n== {'ACTUAL' if analyze else 'ESTIMATED'} PLAN\n{plan}")
            except Exception as e:  # noqa: BLE001
                print(f"\n== {'ACTUAL' if analyze else 'ESTIMATED'} PLAN: not available ({e})")
        t = measure("q", lambda: conn.execute(sql, params or None), warmup=2, iters=10)
        print(f"\n== TIMING: p50={t.stats.get('p50', 0):.3f} ms p95={t.stats.get('p95', 0):.3f} ms rows={t.rows} n={t.n}" if t.ok else f"\n== ERROR {t.error}")
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
