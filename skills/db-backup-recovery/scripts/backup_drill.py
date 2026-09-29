#!/usr/bin/env python3
"""Run one (or every) backup strategy of a running rd-databases stack as a full drill and print the result.

Usage: uv run --project harness python skills/db-backup-recovery/scripts/backup_drill.py <stack> [strategy ...] [--json]
       uv run --project harness python skills/db-backup-recovery/scripts/backup_drill.py postgres pg_dump pitr
The stack must be up and loaded (`rdlab run <stack> --phases load --keep`). Strategy names: see `strategies` in
harness/rdlab/backup/<engine>.py (pg_dump, pg_basebackup, incremental, pitr, mysqlsh_dump, clone, mariadb_backup, flashback, ...).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness"))
from rdlab.backup import Ctx, run_strategy, strategies_for   # noqa: E402
from rdlab.config import load_stack                          # noqa: E402
from rdlab.engines import make_engine                        # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stack")
    ap.add_argument("strategy", nargs="*", help="strategy names (default: all)")
    ap.add_argument("--json", action="store_true", help="print the full result records")
    a = ap.parse_args()
    cfg = load_stack(a.stack)
    eng = make_engine(cfg)
    ctx = Ctx(eng, cfg, log=print)
    todo = strategies_for(eng, cfg)
    if a.strategy:
        todo = [s for s in todo if s.name in a.strategy]
        if not todo:
            sys.exit(f"no such strategy; available: {[s.name for s in strategies_for(eng, cfg)]}")
    ctx.source_fp = ctx.fingerprint()
    print(f"source fingerprint: {sum(v.get('count', 0) for v in ctx.source_fp.values())} rows in {len(ctx.source_fp)} tables")
    out = {}
    for s in todo:
        out[s.name] = run_strategy(ctx, s)
    ctx.probe_drop()
    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        for name, r in out.items():
            print(f"{name:20s} {r['status']:6s} backup {r.get('backup_seconds')}s / {r.get('backup_mb')} MB, restore {r.get('restore_seconds')}s, "
                  f"verified={r.get('verify', {}).get('match')} pitr={r.get('pitr', {}).get('match', '-')}")


if __name__ == "__main__":
    main()
