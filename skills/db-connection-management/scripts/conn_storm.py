#!/usr/bin/env python3
"""Open N connections concurrently against a stack target; report connect-time percentiles, failures and error codes.

Usage: uv run --project harness python skills/db-connection-management/scripts/conn_storm.py <stack> [--target primary] [--n 200] [--hold 1.0]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness"))
from rdlab.config import load_stack                       # noqa: E402
from rdlab.engines import make_engine                    # noqa: E402
from rdlab.phases.connections import connection_storm    # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stack")
    ap.add_argument("--target", default=None)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--hold", type=float, default=1.0)
    a = ap.parse_args()
    cfg = load_stack(a.stack)
    eng = make_engine(cfg)
    target = cfg.by_name(a.target) if a.target else cfg.primary
    r = connection_storm(eng, target, a.n, hold_seconds=a.hold)
    print(json.dumps(r, indent=1, default=str))


if __name__ == "__main__":
    main()
