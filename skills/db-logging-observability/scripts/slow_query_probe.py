#!/usr/bin/env python3
"""Prove that a running rd-databases stack captures a slow statement in its slow-query sink (and not a fast one),
optionally run the audit check too, and print the effective logging settings.

Usage: uv run --project harness python skills/db-logging-observability/scripts/slow_query_probe.py <stack> [--audit] [--settings]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness"))
from rdlab.config import load_stack                        # noqa: E402
from rdlab.dblogs import marker, probe_for, wait_found    # noqa: E402
from rdlab.engines import make_engine                      # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stack")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--settings", action="store_true")
    a = ap.parse_args()
    cfg = load_stack(a.stack)
    eng = make_engine(cfg)
    p = probe_for(eng, cfg, log=print)
    if p is None:
        sys.exit("no logging probe for this dialect")
    print(f"sink: {p.sink}\nthreshold: {p.slow_threshold_ms} ms")
    if a.settings:
        print(json.dumps(p.settings(), indent=2, default=str))
    mk_fast, mk_slow = marker(), marker()
    p.run_query(p.fast_query(mk_fast))
    t0 = time.perf_counter()
    p.run_query(p.slow_query(mk_slow))
    print(f"slow statement took {(time.perf_counter() - t0) * 1000:.1f} ms client-side")
    found, secs = wait_found(lambda: p.find_slow(mk_slow))
    print(f"slow statement in the sink: {found.get('found')} after {secs:.2f}s -> {found.get('sample', '')[:300]}")
    print(f"fast statement in the sink (should be False when the sink has a threshold): {p.find_slow(mk_fast).get('found')}")
    if a.audit:
        desc = p.audit_setup()
        print(f"audit: {desc}")
        mk = marker()
        for s in p.audit_actions(mk):
            p.q(s, fetch=False)
        found, secs = wait_found(lambda: p.audit_find(mk), timeout=20)
        print(f"audit event found: {found.get('found')} after {secs:.2f}s -> {found.get('sample', '')[:300]}")
        p.audit_teardown()


if __name__ == "__main__":
    main()
