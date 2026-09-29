"""Phase: SQL feature probes (DDL/DML capabilities that are not exercised by the timed queries)."""
from __future__ import annotations

import time
from typing import Any

from ..config import StackConfig
from ..dialects import Unsupported, render
from ..engines import Engine
from ..util import short_err


def run_capabilities(engine: Engine, cfg: StackConfig, *, log=print) -> dict[str, Any]:
    results: dict[str, Any] = {}
    probes = engine.dialect.probes()
    for p in probes:
        conn = None
        rec: dict[str, Any] = {"status": "unsupported", "note": p.note}
        t0 = time.perf_counter()
        try:
            conn = engine.connect_primary()
            in_txn = any("SAVEPOINT" in s.upper() or "SAVE TRANSACTION" in s.upper() or "DECLARE" in s.upper() for s in p.sql)
            if in_txn:
                conn.set_autocommit(False)
            last_rows = None
            err = None
            for i, s in enumerate(p.sql):
                try:
                    rendered = render(s, engine.dialect)
                except Unsupported as e:
                    err = e
                    break
                try:
                    rows = conn.execute(rendered, rendered=True, fetch=True)
                    if i == len(p.sql) - 1:
                        last_rows = rows
                except Exception as e:  # noqa: BLE001
                    err = e
                    if i == len(p.sql) - 1 and p.expect_error:
                        rec["error_code"] = engine.error_code(e)
                    break
            if in_txn:
                try:
                    conn.commit()
                except Exception:  # noqa: BLE001
                    conn.rollback()
            if p.expect_error:
                rec["status"] = "supported" if err is not None else "unsupported"
                rec["detail"] = short_err(err) if err else "no error raised"
            else:
                rec["status"] = "supported" if err is None else "unsupported"
                if err is not None:
                    rec["error"] = short_err(err)
                    rec["error_code"] = engine.error_code(err)
                    if not isinstance(err, Unsupported) and not engine.is_unsupported_error(err):
                        rec["status"] = "error"          # syntax accepted but failed at run time
                elif p.fetch_last and last_rows is not None:
                    rec["sample"] = str(last_rows[:2])[:200]
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["error"] = short_err(e)
        finally:
            if conn is not None:
                try:
                    conn.set_autocommit(True)
                except Exception:  # noqa: BLE001
                    pass
                for c in p.cleanup:
                    try:
                        conn.execute(render(c, engine.dialect), rendered=True, fetch=False)
                    except Exception:  # noqa: BLE001
                        pass
                conn.close()
        rec["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        results[p.name] = rec
        mark = {"supported": "OK ", "unsupported": "-- ", "error": "ERR"}[rec["status"]]
        log(f"  {mark} {p.name:26s} {rec.get('error', rec.get('detail', rec.get('sample', '')))[:90]}")
    return results
