"""Persist per-engine results and build the cross-engine summary (results/SUMMARY.md + summary.json)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .queries import CATALOG
from .util import RESULTS, dump_json, load_json, now_iso
from .engines.base import looks_unsupported


def _status(rec: dict) -> str:
    st = rec.get("status")
    if st == "error" and looks_unsupported(rec.get("error")):
        return "unsupported"
    return st


def save_result(key: str, run_id: str, result: dict[str, Any]) -> Path:
    d = RESULTS / key
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{run_id}.json"
    dump_json(p, result)
    latest = d / "latest.json"
    # merge phases into latest so partial re-runs keep earlier phases
    merged = load_json(latest) if latest.exists() else {}
    merged.update({k: v for k, v in result.items() if k != "phases"})
    merged.setdefault("phases", {}).update(result.get("phases", {}))
    dump_json(latest, merged)
    return p


def _fmt_ms(v) -> str:
    if v is None:
        return "-"
    if v >= 1000:
        return f"{v/1000:.2f} s"
    if v >= 10:
        return f"{v:.0f}"
    return f"{v:.2f}"


def load_all() -> dict[str, dict[str, Any]]:
    out = {}
    if not RESULTS.exists():
        return out
    for d in sorted(RESULTS.iterdir()):
        f = d / "latest.json"
        if f.exists():
            out[d.name] = load_json(f)
    return out


def build_summary() -> tuple[Path, Path]:
    all_res = load_all()
    lines: list[str] = []
    keys = list(all_res)
    lines.append("# rd-databases — cross-engine results\n")
    runtime = "the k3s runtime (`RDLAB_PLATFORM=k3s`, ports via k8s/ports.json)" if RESULTS.name != "results" else "the Docker Compose runtime"
    lines.append(f"Generated {now_iso()} from `{RESULTS.name}/<engine>/latest.json` ({runtime}). Timings are client-observed p50 latencies in milliseconds "
                 "(warm cache, single connection, 10 iterations unless noted). `--` = unsupported syntax, `ERR` = failed at run time.\n")
    # --- engine table
    lines.append("## Engines\n")
    lines.append("| engine | version | category | image | load rows/s (events) | schema+load s | replication |")
    lines.append("|---|---|---|---|---:|---:|---|")
    for k in keys:
        r = all_res[k]
        ld = r.get("phases", {}).get("load", {})
        ev = ld.get("tables", {}).get("events", {})
        rep = r.get("phases", {}).get("replication", {})
        lines.append(f"| {r.get('display', k)} | {str(ld.get('server_version', r.get('server_version', '')))[:60]} | {r.get('category', '')} | `{r.get('image', '')}` | "
                     f"{ev.get('rows_per_s') or '-'} | {ld.get('load_seconds', '-')} | {rep.get('kind', '-')} |")
    # --- query matrix
    lines.append("\n## Query capability & latency matrix (p50 ms)\n")
    header = "| query | " + " | ".join(all_res[k].get("display", k) for k in keys) + " |"
    lines.append(header)
    lines.append("|---|" + "---:|" * len(keys))
    for q in CATALOG:
        row = [f"`{q.id}`"]
        for k in keys:
            b = all_res[k].get("phases", {}).get("bench", {}).get(q.id)
            if not b:
                row.append("·")
            elif b.get("status") == "ok":
                row.append(_fmt_ms(b["timing"].get("p50")))
            elif _status(b) == "unsupported":
                row.append("--")
            else:
                row.append("ERR")
        lines.append("| " + " | ".join(row) + " |")
    # --- capability probes
    lines.append("\n## Feature probes\n")
    probe_names: list[str] = []
    for k in keys:
        for n in all_res[k].get("phases", {}).get("capabilities", {}):
            if n.startswith("_"):
                continue
            if n not in probe_names:
                probe_names.append(n)
    lines.append("| feature | " + " | ".join(all_res[k].get("display", k) for k in keys) + " |")
    lines.append("|---|" + ":---:|" * len(keys))
    sym = {"supported": "✅", "unsupported": "❌", "error": "⚠️"}
    for n in probe_names:
        row = [n]
        for k in keys:
            c = all_res[k].get("phases", {}).get("capabilities", {}).get(n)
            row.append(sym.get(_status(c), "?") if isinstance(c, dict) else "·")
        lines.append("| " + " | ".join(row) + " |")
    # --- optimisation
    lines.append("\n## Optimisation experiments (speed-up of best variant vs first variant, p50)\n")
    exp_ids: list[str] = []
    for k in keys:
        for e in all_res[k].get("phases", {}).get("optimize", {}):
            if e.startswith("_"):
                continue
            if e not in exp_ids:
                exp_ids.append(e)
    lines.append("| experiment | " + " | ".join(all_res[k].get("display", k) for k in keys) + " |")
    lines.append("|---|" + "---:|" * len(keys))
    for e in exp_ids:
        row = [f"`{e}`"]
        for k in keys:
            x = all_res[k].get("phases", {}).get("optimize", {}).get(e)
            if not isinstance(x, dict):
                row.append("·")
            elif "speedup_vs_first" in x and x["speedup_vs_first"]:
                row.append(f"x{x['speedup_vs_first']} ({x['best_variant']})")
            elif "variants" in x and x["variants"] and "rows_per_s" in x["variants"][0]:
                row.append(" / ".join(f"{v.get('rows_per_s') or 'ERR'}" for v in x["variants"]) + " rows/s")
            elif x.get("status") == "n/a":
                row.append("n/a")
            elif "variants" in x and x["variants"] and "p50_ms" in x["variants"][0]:
                row.append(" / ".join(f"{v.get('p50_ms', '-')}" for v in x["variants"]) + " ms")
            else:
                row.append("-")
        lines.append("| " + " | ".join(row) + " |")
    # --- connections
    lines.append("\n## Connections\n")
    lines.append("| engine | max conn | connect p50 ms (direct) | connect p50 ms (proxy) | storm 200: opened/failed | 1 worker qps | 32 workers qps | 32w p99 ms | deadlock detected (ms) | RR write conflict |")
    lines.append("|---|---:|---:|---:|---|---:|---:|---:|---|---|")
    for k in keys:
        c = all_res[k].get("phases", {}).get("connections", {})
        if not c:
            continue
        cl = c.get("connect_latency", {})
        direct = next((v for kk, v in cl.items() if kk == "primary" or kk == "embedded"), next(iter(cl.values()), {}))
        proxy = next((v for kk, v in cl.items() if kk not in ("primary", "embedded")), None)
        storms = c.get("storm", {}).get("primary", []) or next(iter(c.get("storm", {}).values()), []) if c.get("storm") else []
        s200 = next((s for s in storms if s["requested"] == 200), storms[-1] if storms else None)
        conc = c.get("concurrency", {}).get("primary") or next(iter(c.get("concurrency", {}).values()), [])
        w1 = next((x for x in conc if x.get("workers") == 1), {})
        w32 = next((x for x in conc if x.get("workers") == 32), {})
        tx = c.get("transactions", {})
        dl = tx.get("deadlock", {}) if isinstance(tx, dict) else {}
        rr = tx.get("write_conflict_repeatable_read", {}) if isinstance(tx, dict) else {}
        lines.append(f"| {all_res[k].get('display', k)} | {c.get('max_connections', '-')} | {_fmt_ms(direct.get('ms', {}).get('p50'))} | "
                     f"{_fmt_ms(proxy['ms'].get('p50')) if proxy else '-'} | {f'{s200['opened']}/{s200['failed']}' if s200 else '-'} | "
                     f"{w1.get('qps', '-')} | {w32.get('qps', '-')} | {_fmt_ms(w32.get('latency_ms', {}).get('p99'))} | "
                     f"{'yes ' + str(dl.get('detection_ms')) if dl.get('detected') else ('no' if dl else '-')} | {rr.get('t1_update_after_t2_commit', '-')} |")
    # --- replication
    lines.append("\n## Replication / scaling\n")
    lines.append("| engine | kind | visibility lag p50 / p95 / max ms | replica rejects writes | catch-up after 20k rows (ms) | read scaling gain | failover downtime ms |")
    lines.append("|---|---|---|---|---|---:|---:|")
    for k in keys:
        r = all_res[k].get("phases", {}).get("replication", {})
        if not r:
            continue
        vl = r.get("visibility_lag", {})
        v = next(iter(vl.values()), {}) if vl else {}
        lag = v.get("lag_ms", {}) if isinstance(v, dict) else {}
        wr = r.get("write_rejection", {})
        w = next(iter(wr.values()), {}) if wr else {}
        lul = r.get("lag_under_load", {})
        cu = lul.get("catchup_ms_after_last_commit", {}) if isinstance(lul, dict) else {}
        rs = r.get("read_scaling", {})
        fo = r.get("failover", {})
        lines.append(f"| {all_res[k].get('display', k)} | {r.get('kind', '-')} | "
                     f"{(_fmt_ms(lag.get('p50')) + ' / ' + _fmt_ms(lag.get('p95')) + ' / ' + _fmt_ms(lag.get('max'))) if lag else (r.get('status') or '-')} | "
                     f"{('yes ' + str(w.get('error_code'))) if w.get('writes_rejected') else ('no' if w else '-')} | "
                     f"{next(iter(cu.values()), '-') if cu else '-'} | {rs.get('gain', '-') if isinstance(rs, dict) else '-'} | {fo.get('downtime_ms', '-') if isinstance(fo, dict) else '-'} |")
    # --- load test
    lt_keys = [k for k in keys if isinstance(all_res[k].get("phases", {}).get("loadtest"), dict) and all_res[k]["phases"]["loadtest"].get("runs")]
    if lt_keys:
        lines.append("\n## Multi-connection bulk-insert load test (rows/s; batch size and duration per engine in latest.json)\n")
        lines.append("| engine | mode | entry point | connections | rows/s | batch p50 ms | errors | busiest container (avg cores during inserts) |")
        lines.append("|---|---|---|---:|---:|---:|---:|---|")
        for k in lt_keys:
            for r in all_res[k]["phases"]["loadtest"]["runs"]:
                usage = r.get("container_usage") or {}
                busiest = max(usage.items(), key=lambda kv: kv[1].get("cpu_cores_avg_during_inserts", 0), default=(None, {}))
                b = f"{busiest[0].replace('rdlab-', '')} {busiest[1].get('cpu_cores_avg_during_inserts', 0):.2f}" if busiest[0] else "-"
                lines.append(f"| {all_res[k].get('display', k)} | {r.get('mode', 'values')} | {r.get('target')} | {r.get('workers')} | {r.get('rows_per_s')} | "
                             f"{_fmt_ms(r.get('batch_ms', {}).get('p50'))} | {r.get('errors')} | {b} |")
    # --- backup & recovery
    bk_keys = [k for k in keys if isinstance(all_res[k].get("phases", {}).get("backup"), dict) and all_res[k]["phases"]["backup"].get("strategies")]
    if bk_keys:
        lines.append("\n## Backup & recovery drills (backup -> restore elsewhere -> fingerprint of every table; PITR = point-in-time / incremental probe)\n")
        lines.append("| engine | strategy | kind | tool | backup s | size MB | restore s | verified | PITR / probe | notes |")
        lines.append("|---|---|---|---|---:|---:|---:|:---:|:---:|---|")
        for k in bk_keys:
            b = all_res[k]["phases"]["backup"]
            for name in b.get("order") or list(b["strategies"]):
                st = b["strategies"].get(name) or {}
                if st.get("status") == "ok":
                    ver = "✅" if st.get("verify", {}).get("match") else ("·" if st.get("verify", {}).get("match") is None else "❌")
                    pitr = ("✅" if st["pitr"].get("match") else "❌") if st.get("pitr") else "·"
                    lines.append(f"| {all_res[k].get('display', k)} | `{name}` | {st.get('kind', '')} | {st.get('tool', '')} | {st.get('backup_seconds', '-')} | "
                                 f"{st.get('backup_mb', '-')} | {st.get('restore_seconds', '-')} | {ver} | {pitr} | {(st.get('notes') or '')[:140]} |")
                elif st.get("status") == "n/a":
                    lines.append(f"| {all_res[k].get('display', k)} | `{name}` | {st.get('kind', '')} | {st.get('tool', '')} | n/a | | | · | · | {(st.get('notes') or '')[:140]} |")
                else:
                    lines.append(f"| {all_res[k].get('display', k)} | `{name}` | {st.get('kind', '')} | {st.get('tool', '')} | ERR | | | ❌ | · | {(st.get('error') or '')[:140]} |")
    # --- logging
    lg_keys = [k for k in keys if isinstance(all_res[k].get("phases", {}).get("logging"), dict) and all_res[k]["phases"]["logging"].get("slow_query")]
    if lg_keys:
        outer = "kubelet container logs (`/var/log/pods`, rotated by container-log-max-size/files)" if RESULTS.name != "results" else "Docker json-file with rotation"
        lines.append(f"\n## Logging (slow-query capture, audit trail, cost of logging every statement; container logs = {outer})\n")
        lines.append("| engine | slow-query sink | slow query captured (after s) | fast query filtered | audit mechanism | audit event found | full statement logging: qps baseline -> logged (overhead %) | bytes / statement |")
        lines.append("|---|---|:---:|:---:|---|:---:|---|---:|")
        for k in lg_keys:
            l = all_res[k]["phases"]["logging"]
            sq, au, ov = l.get("slow_query", {}), l.get("audit", {}), l.get("overhead", {})
            cap = f"✅ ({sq.get('seen_after_s')})" if sq.get("slow_logged") else ("ERR" if sq.get("error") else "❌")
            fast = "✅" if sq.get("fast_logged") is False else ("❌ (no threshold)" if sq.get("fast_logged") else "·")
            aud = "✅" if au.get("event_found") else ("n/a" if au.get("status") == "n/a" else ("ERR" if au.get("status") == "error" else "❌"))
            if ov.get("status") == "ok":
                ovs = f"{ov['baseline']['qps']} -> {ov['full']['qps']} ({ov.get('overhead_pct')} %)"
            else:
                ovs = ov.get("status", "-") if ov.get("status") != "error" else "ERR"
            lines.append(f"| {all_res[k].get('display', k)} | {l.get('sink', '')[:70]} | {cap} | {fast} | {(au.get('mechanism') or '')[:60]} | {aud} | {ovs} | {ov.get('bytes_per_statement', '-')} |")
    lines.append("\n## Notes per engine\n")
    for k in keys:
        r = all_res[k]
        notes = r.get("notes") or ""
        errs = r.get("phases", {}).get("load", {}).get("errors", [])
        lines.append(f"- **{r.get('display', k)}**: {notes} {('load errors: ' + '; '.join(errs)) if errs else ''}")
    md = RESULTS / "SUMMARY.md"
    md.write_text("\n".join(lines) + "\n")
    js = RESULTS / "summary.json"
    dump_json(js, {"generated": now_iso(), "engines": {k: {kk: vv for kk, vv in v.items() if kk != "phases"} | {"phases": {p: _strip(pv) for p, pv in v.get("phases", {}).items()}} for k, v in all_res.items()}})
    return md, js


def _strip(v: Any) -> Any:
    """Drop bulky plan text / samples from the JSON summary."""
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if k not in ("plan", "plan_analyze", "samples", "samples_ms", "sql", "ddl", "setup")}
    if isinstance(v, list):
        return [_strip(x) for x in v]
    return v
