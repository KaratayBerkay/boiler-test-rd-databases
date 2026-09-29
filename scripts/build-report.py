#!/usr/bin/env python3
"""Render results/<engine>/latest.json into a single self-contained HTML report (docs/report.html)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
from rdlab.queries import CATALOG          # noqa: E402
from rdlab.report import load_all, _strip  # noqa: E402
from rdlab.engines.base import looks_unsupported  # noqa: E402


def st(rec: dict) -> str:
    s = rec.get("status")
    return "unsupported" if s == "error" and looks_unsupported(rec.get("error")) else s


def compact(res: dict) -> dict:
    out = {}
    for k, r in res.items():
        ph = r.get("phases", {})
        bench = {qid: {"status": st(b), "p50": b.get("timing", {}).get("p50"), "p95": b.get("timing", {}).get("p95"), "rows": b.get("rows")}
                 for qid, b in ph.get("bench", {}).items() if not qid.startswith("_") and isinstance(b, dict)}
        caps = {n: st(c) for n, c in ph.get("capabilities", {}).items() if isinstance(c, dict)}
        opt = {}
        for e, x in ph.get("optimize", {}).items():
            if not isinstance(x, dict) or e.startswith("_"):
                continue
            opt[e] = {"title": x.get("title"), "variants": [{"name": v.get("name"), "p50_ms": v.get("p50_ms"), "rows_per_s": v.get("rows_per_s"),
                                                             "ok": v.get("ok", v.get("rows_per_s") is not None), "status": v.get("status")} for v in x.get("variants", [])],
                      "speedup": x.get("speedup_vs_first"), "best": x.get("best_variant")}
        conn = ph.get("connections", {})
        conn_c = {}
        if isinstance(conn, dict) and conn:
            conn_c = {"max_connections": conn.get("max_connections"),
                      "connect_latency": {t: v.get("ms", {}).get("p50") for t, v in conn.get("connect_latency", {}).items()},
                      "storm": {t: [{"n": s["requested"], "opened": s["opened"], "failed": s["failed"], "codes": s.get("error_codes")} for s in ss] for t, ss in conn.get("storm", {}).items()},
                      "concurrency": {t: [{"workers": x.get("workers"), "qps": x.get("qps"), "p50": x.get("latency_ms", {}).get("p50"), "p99": x.get("latency_ms", {}).get("p99"), "errors": x.get("errors")} for x in xs] for t, xs in conn.get("concurrency", {}).items()},
                      "transactions": conn.get("transactions"), "rw_split": conn.get("rw_split")}
        rep = ph.get("replication", {})
        rep_c = {}
        if isinstance(rep, dict) and rep:
            rep_c = {"kind": rep.get("kind"), "status": rep.get("status"),
                     "visibility": {t: v.get("lag_ms") for t, v in rep.get("visibility_lag", {}).items() if isinstance(v, dict)},
                     "write_rejection": rep.get("write_rejection"), "catchup": (rep.get("lag_under_load") or {}).get("catchup_ms_after_last_commit"),
                     "read_scaling": {k: (v.get("qps") if isinstance(v, dict) else v) for k, v in (rep.get("read_scaling") or {}).items()},
                     "failover": {k: v for k, v in (rep.get("failover") or {}).items() if k in ("downtime_ms", "new_primary", "verified_via", "promote_cmd", "write_attempts_until_success", "kind", "killed", "promote_error", "note", "error")}}
        lt = ph.get("loadtest", {})
        lt_c = {}
        if isinstance(lt, dict) and lt.get("runs"):
            lt_c = {"batch_rows": lt.get("batch_rows"), "seconds": lt.get("seconds_per_run"), "best": lt.get("best"),
                    "runs": [{"mode": r.get("mode"), "target": r.get("target"), "workers": r.get("workers"), "rows_per_s": r.get("rows_per_s"), "errors": r.get("errors"),
                              "p50": r.get("batch_ms", {}).get("p50"), "p95": r.get("batch_ms", {}).get("p95"),
                              "usage": {n.replace("rdlab-", ""): {"cores": u.get("cpu_cores_avg_during_inserts", u.get("cpu_cores_avg")), "limit": u.get("cpu_limit_cores"), "mem_peak_mb": u.get("mem_peak_mb")}
                                        for n, u in (r.get("container_usage") or {}).items()}} for r in lt["runs"]]}
        bk = ph.get("backup", {})
        bk_c = []
        if isinstance(bk, dict) and bk.get("strategies"):
            for name in bk.get("order") or list(bk["strategies"]):
                sb = bk["strategies"].get(name) or {}
                bk_c.append({"name": name, "status": sb.get("status"), "kind": sb.get("kind"), "tool": sb.get("tool"), "backup_s": sb.get("backup_seconds"),
                             "mb": sb.get("backup_mb"), "restore_s": sb.get("restore_seconds"), "verified": (sb.get("verify") or {}).get("match"),
                             "pitr": (sb.get("pitr") or {}).get("match") if sb.get("pitr") else None, "notes": (sb.get("notes") or sb.get("error") or "")[:220]})
        lg = ph.get("logging", {})
        lg_c = {}
        if isinstance(lg, dict) and lg.get("slow_query"):
            sq, au, ov = lg.get("slow_query", {}), lg.get("audit", {}), lg.get("overhead", {})
            lg_c = {"sink": lg.get("sink"), "threshold_ms": lg.get("slow_threshold_ms"), "slow_logged": sq.get("slow_logged"), "seen_after_s": sq.get("seen_after_s"),
                    "slow_ms": sq.get("slow_query_ms"), "fast_logged": sq.get("fast_logged"), "audit_mechanism": au.get("mechanism"), "audit_status": au.get("status"),
                    "audit_found": au.get("event_found"), "overhead_status": ov.get("status"), "baseline_qps": (ov.get("baseline") or {}).get("qps"),
                    "full_qps": (ov.get("full") or {}).get("qps"), "overhead_pct": ov.get("overhead_pct"), "bytes_per_stmt": ov.get("bytes_per_statement"),
                    "full_logging": ov.get("full_logging"), "containers": len(lg.get("container_logging") or {}),
                    "docker_driver": next(iter((lg.get("container_logging") or {}).values()), {}).get("driver"), "collected": len(lg.get("collected") or [])}
        load = ph.get("load", {})
        out[k] = {"display": r.get("display", k), "category": r.get("category"), "image": r.get("image"), "version": str(load.get("server_version") or r.get("server_version") or "")[:70],
                  "notes": r.get("notes"), "load": {t: {"rows": v.get("rows"), "rows_per_s": v.get("rows_per_s"), "method": v.get("method"), "error": v.get("error")} for t, v in load.get("tables", {}).items()},
                  "bench": bench, "caps": caps, "opt": opt, "conn": conn_c, "rep": rep_c, "loadtest": lt_c, "backup": bk_c, "logging": lg_c}
    return out


def main() -> None:
    res = load_all()
    data = {"generated": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"), "queries": [{"id": q.id, "title": q.title, "tags": list(q.tags), "kind": q.kind} for q in CATALOG],
            "engines": compact(res)}
    tpl = (ROOT / "scripts" / "report-template.html").read_text()
    html = tpl.replace("/*__DATA__*/", "const DATA = " + json.dumps(data, default=str) + ";")
    out = ROOT / "docs" / "report.html"
    out.write_text(html)
    print(f"wrote {out} ({out.stat().st_size/1024:.0f} KB, {len(data['engines'])} engines)")


if __name__ == "__main__":
    main()
