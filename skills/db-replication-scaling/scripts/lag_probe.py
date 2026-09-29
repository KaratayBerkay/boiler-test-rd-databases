#!/usr/bin/env python3
"""Measure visibility lag between a primary and a replica target of a running rd-databases stack.

Usage: uv run --project harness python skills/db-replication-scaling/scripts/lag_probe.py <stack> [--replica replica] [--n 30]
Creates/drops a small `repl_probe` table on the primary.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness"))
from rdlab.config import load_stack                                  # noqa: E402
from rdlab.engines import make_engine                               # noqa: E402
from rdlab.phases.replication import PROBE_TABLE, visibility_lag, write_rejection   # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stack")
    ap.add_argument("--replica", default=None)
    ap.add_argument("--n", type=int, default=30)
    a = ap.parse_args()
    cfg = load_stack(a.stack)
    eng = make_engine(cfg)
    replica = cfg.by_name(a.replica) if a.replica else (cfg.replicas or [t for t in cfg.targets if t.role == "node" and t is not cfg.primary])[0]
    c = eng.connect_primary()
    try:
        for stmt in (eng.dialect.drop_table_if_exists("repl_probe"),):
            try:
                c.execute(stmt, rendered=True, fetch=False)
            except Exception:  # noqa: BLE001
                pass
        c.execute(eng.dialect.create_table(PROBE_TABLE), rendered=True, fetch=False)
    finally:
        c.close()
    print(json.dumps({"visibility_lag": visibility_lag(eng, cfg.primary, replica, n=a.n), "write_rejection": write_rejection(eng, replica)}, indent=1, default=str))


if __name__ == "__main__":
    main()
