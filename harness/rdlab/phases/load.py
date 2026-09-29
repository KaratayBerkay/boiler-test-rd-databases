"""Phase: schema creation + bulk data load."""
from __future__ import annotations

import time
from typing import Any

from ..config import StackConfig
from ..datagen import counts_for, ensure_csvs
from ..engines import Engine
from ..schema import LOAD_ORDER, TABLE_BY_NAME, TABLES
from ..timing import Stopwatch
from ..util import short_err


def run_load(engine: Engine, cfg: StackConfig, scale: float, *, log=print) -> dict[str, Any]:
    out: dict[str, Any] = {"scale": scale, "tables": {}, "ddl": [], "errors": []}
    paths = ensure_csvs(scale)
    expected = counts_for(scale)
    conn = engine.connect_primary()
    try:
        sw = Stopwatch()
        engine.drop_schema(conn, TABLES)
        out["ddl"] = engine.create_schema(conn, TABLES)
        out["schema_seconds"] = round(sw.ms() / 1000, 2)
        log(f"  schema created ({len(out['ddl'])} statements, {out['schema_seconds']}s)")
        total = Stopwatch()
        for name in LOAD_ORDER:
            t = TABLE_BY_NAME[name]
            sw = Stopwatch()
            try:
                n, method = engine.bulk_load(conn, t, paths[name])
                secs = sw.ms() / 1000
                actual = engine.count(conn, name)
                out["tables"][name] = {"rows": n, "counted": actual, "expected": expected.get(name), "seconds": round(secs, 2),
                                       "rows_per_s": round(n / secs) if secs > 0 else None, "method": method,
                                       "ok": actual == expected.get(name, n)}
                log(f"  {name:12s} {n:>9,} rows  {secs:7.2f}s  {out['tables'][name]['rows_per_s'] or 0:>9,} rows/s  [{method}]")
            except Exception as e:  # noqa: BLE001
                out["tables"][name] = {"error": short_err(e), "seconds": round(sw.ms() / 1000, 2)}
                out["errors"].append(f"{name}: {short_err(e)}")
                log(f"  {name:12s} FAILED: {short_err(e)}")
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
        out["load_seconds"] = round(total.ms() / 1000, 2)
        sw = Stopwatch()
        out["fts_setup"] = engine.fts_setup(conn)
        out["after_load"] = engine.after_load(conn, TABLES)
        out["post_seconds"] = round(sw.ms() / 1000, 2)
        out["server_version"] = conn.server_version()
    finally:
        conn.close()
    return out
