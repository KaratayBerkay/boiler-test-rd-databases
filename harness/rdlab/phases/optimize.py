"""Phase: optimisation experiments — measure a query, apply an index/rewrite, measure again."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import StackConfig
from ..datagen import Sizes
from ..dialects import Unsupported, render
from ..engines import Engine
from ..schema import TABLE_BY_NAME
from ..timing import measure
from ..util import short_err, summarize


@dataclass
class Variant:
    name: str
    sql: str
    params: list | None = None
    setup: list[Callable[[Any], str | None]] = field(default_factory=list)      # each returns a statement (already rendered) or None
    teardown: list[Callable[[Any], str | None]] = field(default_factory=list)
    note: str = ""


@dataclass
class Experiment:
    id: str
    title: str
    variants: list[Variant]
    what: str = ""


def _idx(name, table, cols, **kw):
    def f(d):
        return d.create_index(name, table, cols, **kw)
    return f


def _drop_idx(name, table):
    def f(d):
        return d.drop_index(name, table)
    return f


SKIP = "__skip__"


def _analyze(table):
    def f(d):
        return d.analyze_table(table) or SKIP        # engines without a stats command just skip this step
    return f


EVENTS_Q = ("SELECT COUNT(*) AS n, SUM(value_num) AS total FROM events WHERE event_type = 'checkout' "
            "AND occurred_at >= {ts(2025-06-01 00:00:00)} AND occurred_at < {ts(2025-07-01 00:00:00)}")
ITEMS_Q = "SELECT order_id, product_id, qty FROM order_items WHERE order_id >= ? AND order_id < ? ORDER BY order_id, product_id"
PENDING_Q = ("SELECT id, customer_id FROM orders WHERE status = 'pending' AND ordered_at >= {ts(2025-09-01 00:00:00)} "
             "AND ordered_at < {ts(2025-10-01 00:00:00)} ORDER BY ordered_at, id")


def experiments(sz: Sizes) -> list[Experiment]:
    r = random.Random(5)
    items_params = [(x, x + 1000) for x in (r.randint(1, max(1, sz.orders - 1000)) for _ in range(20))]
    or_params = [(r.randint(1, sz.customers), r.randint(1, sz.orders)) for _ in range(20)]
    return [
        Experiment("opt01_composite_index", "Range+equality filter on 1M-row table: no index vs composite index (both column orders)",
                   [Variant("no_index", EVENTS_Q),
                    Variant("index_(occurred_at,event_type)", EVENTS_Q, setup=[_idx("ix_ev_ts_type", "events", ["occurred_at", "event_type"]), _analyze("events")],
                            teardown=[_drop_idx("ix_ev_ts_type", "events")], note="range column first"),
                    Variant("index_(event_type,occurred_at)", EVENTS_Q, setup=[_idx("ix_ev_type_ts", "events", ["event_type", "occurred_at"]), _analyze("events")],
                            teardown=[_drop_idx("ix_ev_type_ts", "events")], note="equality column first (recommended)")],
                   what="composite index column ordering: equality predicates first, then the range column"),
        Experiment("opt02_covering_index", "Range fetch on order_items: plain index vs covering/INCLUDE index (index-only scan)",
                   [Variant("plain_index_order_id", ITEMS_Q, items_params),
                    Variant("covering_include", ITEMS_Q, items_params, setup=[_idx("ix_oi_cover", "order_items", ["order_id"], include=["product_id", "qty"]), _analyze("order_items")],
                            teardown=[_drop_idx("ix_oi_cover", "order_items")], note="INCLUDE columns (PG/MSSQL); falls back to composite index elsewhere")],
                   what="covering index lets the engine answer from the index without heap/table lookups"),
        Experiment("opt03_sargable_predicate", "YEAR()/MONTH() on column vs range predicate, then with index on ordered_at",
                   [Variant("nonsargable_no_index", "SELECT COUNT(*) FROM orders WHERE {year(ordered_at)} = 2025 AND {month(ordered_at)} = 5"),
                    Variant("sargable_no_index", "SELECT COUNT(*) FROM orders WHERE ordered_at >= {ts(2025-05-01 00:00:00)} AND ordered_at < {ts(2025-06-01 00:00:00)}"),
                    Variant("sargable_with_index", "SELECT COUNT(*) FROM orders WHERE ordered_at >= {ts(2025-05-01 00:00:00)} AND ordered_at < {ts(2025-06-01 00:00:00)}",
                            setup=[_idx("ix_orders_ordered_at", "orders", ["ordered_at"]), _analyze("orders")], teardown=[_drop_idx("ix_orders_ordered_at", "orders")]),
                    Variant("nonsargable_with_index", "SELECT COUNT(*) FROM orders WHERE {year(ordered_at)} = 2025 AND {month(ordered_at)} = 5",
                            setup=[_idx("ix_orders_ordered_at", "orders", ["ordered_at"]), _analyze("orders")], teardown=[_drop_idx("ix_orders_ordered_at", "orders")],
                            note="function on column cannot use the index")],
                   what="predicates must be written so the index can be used (no functions on the indexed column)"),
        Experiment("opt04_pagination", "OFFSET vs keyset pagination, with and without index on (ordered_at, id)",
                   [Variant("offset_no_index", "SELECT id, customer_id, ordered_at FROM orders ORDER BY ordered_at, id {offset_limit(" + str(sz.orders // 2) + ",50)}"),
                    Variant("keyset_no_index", "SELECT id, customer_id, ordered_at FROM orders WHERE (ordered_at > {ts(2025-01-01 00:00:00)} OR (ordered_at = {ts(2025-01-01 00:00:00)} AND id > 0)) ORDER BY ordered_at, id {limit(50)}"),
                    Variant("offset_with_index", "SELECT id, customer_id, ordered_at FROM orders ORDER BY ordered_at, id {offset_limit(" + str(sz.orders // 2) + ",50)}",
                            setup=[_idx("ix_orders_ts_id", "orders", ["ordered_at", "id"]), _analyze("orders")], teardown=[_drop_idx("ix_orders_ts_id", "orders")]),
                    Variant("keyset_with_index", "SELECT id, customer_id, ordered_at FROM orders WHERE (ordered_at > {ts(2025-01-01 00:00:00)} OR (ordered_at = {ts(2025-01-01 00:00:00)} AND id > 0)) ORDER BY ordered_at, id {limit(50)}",
                            setup=[_idx("ix_orders_ts_id", "orders", ["ordered_at", "id"]), _analyze("orders")], teardown=[_drop_idx("ix_orders_ts_id", "orders")]),
                    Variant("keyset_rowvalue_with_index", "SELECT id, customer_id, ordered_at FROM orders WHERE {keyset_pred(ordered_at,id,{ts(2025-01-01 00:00:00)},0)} ORDER BY ordered_at, id {limit(50)}",
                            setup=[_idx("ix_orders_ts_id", "orders", ["ordered_at", "id"]), _analyze("orders")], teardown=[_drop_idx("ix_orders_ts_id", "orders")],
                            note="row-value comparison (SQL standard) - lets the optimizer seek directly into the composite index")],
                   what="keyset pagination cost is independent of page depth; OFFSET still scans and discards rows"),
        Experiment("opt05_correlated_vs_join", "Correlated scalar subquery vs derived-table JOIN",
                   [Variant("correlated_subquery", "SELECT p.id, p.price, (SELECT AVG(p2.price) FROM products p2 WHERE p2.category_id = p.category_id) AS cat_avg FROM products p WHERE {bool_eq_true(p.active)} ORDER BY p.id {limit(500)}"),
                    Variant("join_rewrite", "SELECT p.id, p.price, a.cat_avg FROM products p JOIN (SELECT category_id, AVG(price) AS cat_avg FROM products GROUP BY category_id) a ON a.category_id = p.category_id WHERE {bool_eq_true(p.active)} ORDER BY p.id {limit(500)}")],
                   what="optimizers differ in de-correlating subqueries; an explicit aggregate join is portable and predictable"),
        Experiment("opt06_antijoin_forms", "Anti-join: LEFT JOIN IS NULL vs NOT EXISTS vs NOT IN",
                   [Variant("left_join_is_null", "SELECT p.id FROM products p LEFT JOIN order_items oi ON oi.product_id = p.id WHERE oi.id IS NULL ORDER BY p.id"),
                    Variant("not_exists", "SELECT p.id FROM products p WHERE NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.product_id = p.id) ORDER BY p.id"),
                    Variant("not_in", "SELECT p.id FROM products p WHERE p.id NOT IN (SELECT oi.product_id FROM order_items oi) ORDER BY p.id", note="NOT IN is NULL-unsafe and often slower")],
                   what="NOT EXISTS is the safest anti-join; NOT IN changes semantics with NULLs and may block hash anti-join plans"),
        Experiment("opt07_or_vs_union", "OR across two different indexed columns vs UNION",
                   [Variant("or_predicate", "SELECT id FROM orders WHERE customer_id = ? OR id = ? ORDER BY id", or_params),
                    Variant("union_rewrite", "SELECT id FROM (SELECT id FROM orders WHERE customer_id = ? UNION SELECT id FROM orders WHERE id = ?) u ORDER BY id", or_params)],
                   what="OR over different columns often forces a scan; UNION lets each branch use its own index (some optimizers do this automatically: bitmap OR / index merge)"),
        Experiment("opt08_partial_index", "Filtered/partial index for a selective status value",
                   [Variant("no_partial_index", PENDING_Q),
                    Variant("full_index_(status,ordered_at)", PENDING_Q, setup=[_idx("ix_orders_status_ts", "orders", ["status", "ordered_at"]), _analyze("orders")],
                            teardown=[_drop_idx("ix_orders_status_ts", "orders")]),
                    Variant("partial_index_pending", PENDING_Q, setup=[_idx("ix_orders_pending_ts", "orders", ["ordered_at"], where="status = 'pending'"), _analyze("orders")],
                            teardown=[_drop_idx("ix_orders_pending_ts", "orders")], note="only PG / SQLite / SQL Server (filtered) / MSSQL")],
                   what="partial indexes are tiny and fast for hot subsets (queues, pending rows)"),
        Experiment("opt09_stats_refresh", "Plan quality with stale vs fresh statistics (after bulk insert)",
                   [Variant("after_bulk_insert_no_analyze", "SELECT c.country_code, COUNT(*) AS n FROM events e JOIN customers c ON c.id = e.customer_id WHERE e.event_type = 'bench_stats' GROUP BY c.country_code ORDER BY c.country_code"),
                    Variant("after_analyze", "SELECT c.country_code, COUNT(*) AS n FROM events e JOIN customers c ON c.id = e.customer_id WHERE e.event_type = 'bench_stats' GROUP BY c.country_code ORDER BY c.country_code", setup=[_analyze("events")])],
                   what="row-count estimates drive join/scan choices; refresh statistics after bulk changes"),
    ]


def _batch_insert_experiment(engine: Engine, conn, sz: Sizes, log) -> dict[str, Any]:
    """Write optimisation: single-row autocommit vs driver executemany vs explicit multi-row VALUES, in one transaction."""
    base = 20_000_000_000
    rng = random.Random(3)
    sql = "INSERT INTO events (id, customer_id, event_type, occurred_at, payload, value_num) VALUES (?, ?, 'bench_batch', {now}, NULL, ?)"
    d = engine.dialect
    res: dict[str, Any] = {"title": "Insert batching: 1-row autocommit vs executemany(1000) vs multi-row VALUES(1000), one transaction",
                           "what": "round-trips and per-statement commits dominate small writes; batching amortises both", "variants": []}
    n_rows = 3000
    for name, mode, batch in (("autocommit_1_row", "single", 1), ("txn_executemany_1000", "executemany", 1000), ("txn_multirow_values_1000", "multirow", 1000)):
        rows = [(base + rng.randint(1, 10**9) * 1000 + i, rng.randint(1, sz.customers), rng.randint(0, 100)) for i in range(n_rows)]
        t0 = time.perf_counter()
        err = None
        try:
            if mode == "single":
                for r in rows:
                    conn.execute(sql, r, fetch=False)
            elif mode == "executemany":
                conn.set_autocommit(False)
                for i in range(0, n_rows, batch):
                    conn.executemany(sql, rows[i:i + batch])
                conn.commit()
                conn.set_autocommit(True)
            else:
                if not getattr(d, "multirow_insert", True):
                    raise Unsupported(f"{d.name}: multi-row VALUES not supported")
                conn.set_autocommit(False)
                for i in range(0, n_rows, batch):
                    chunk = rows[i:i + batch]
                    values = ", ".join("(?, ?, 'bench_batch', {now}, NULL, ?)" for _ in chunk)
                    flat = [x for r in chunk for x in r]
                    conn.execute("INSERT INTO events (id, customer_id, event_type, occurred_at, payload, value_num) VALUES " + values, flat, fetch=False)
                conn.commit()
                conn.set_autocommit(True)
        except Unsupported as e:
            err = str(e)
        except Exception as e:  # noqa: BLE001
            err = short_err(e)
            try:
                conn.rollback()
                conn.set_autocommit(True)
            except Exception:  # noqa: BLE001
                pass
        secs = time.perf_counter() - t0
        v = {"name": name, "rows": n_rows, "seconds": round(secs, 3), "rows_per_s": round(n_rows / secs) if secs > 0 and not err else None, "error": err}
        res["variants"].append(v)
        log(f"    {name:26s} {v['rows_per_s'] or 0:>9,} rows/s {('ERR ' + err) if err else ''}")
    try:
        conn.execute("DELETE FROM events WHERE event_type = 'bench_batch'", fetch=False)
    except Exception:  # noqa: BLE001
        try:
            conn.execute("ALTER TABLE events DELETE WHERE event_type = 'bench_batch'", fetch=False)
        except Exception:  # noqa: BLE001
            pass
    return res


def _prepared_experiment(engine: Engine, cfg: StackConfig, sz: Sizes, log) -> dict[str, Any] | None:
    """Server-side prepared statements vs plain text queries (psycopg engines only)."""
    if engine.driver != "psycopg":
        log(f"    n/a (driver {engine.driver} has no server-side prepare toggle)")
        return {"title": "Prepared vs unprepared point lookups", "status": "n/a", "note": f"driver {engine.driver}: no server-side prepare toggle exposed"}
    res: dict[str, Any] = {"title": "Prepared vs unprepared point lookups (psycopg prepare=True)", "what": "server-side prepared statements skip parse/plan per execution", "variants": []}
    sql = "SELECT id, name, email FROM customers WHERE id = ?"
    ids = [(random.Random(9).randint(1, sz.customers),) for _ in range(200)]
    for name, prep in (("unprepared", False), ("prepared", True), ("unprepared_again", False)):
        conn = engine.connect_primary()
        conn.prepare = prep
        if not prep:
            conn.raw.prepare_threshold = None       # disable psycopg's automatic prepare-after-5-executions
        i = {"n": 0}
        def fn():
            p = ids[i["n"] % len(ids)]
            i["n"] += 1
            return conn.execute(sql, p)
        t = measure(name, fn, warmup=5, iters=300, max_seconds=10)
        conn.close()
        res["variants"].append({"name": name, "p50_ms": round(t.stats.get("p50", 0), 4), "p99_ms": round(t.stats.get("p99", 0), 4), "n": t.n, "error": t.error})
        log(f"    {name:16s} p50={t.stats.get('p50', 0):.4f} ms p99={t.stats.get('p99', 0):.4f} ms")
    return res


def run_optimize(engine: Engine, cfg: StackConfig, scale: float, *, log=print) -> dict[str, Any]:
    sz = Sizes.for_scale(scale)
    out: dict[str, Any] = {}
    conn = engine.connect_primary()
    d = engine.dialect
    try:
        # seed rows for the statistics experiment (bulk insert without ANALYZE)
        try:
            rng = random.Random(4)
            rows = [(30_000_000_000 + i, rng.randint(1, sz.customers), "bench_stats", None, None, 1.0) for i in range(20_000)]
            conn.set_autocommit(False)
            conn.executemany("INSERT INTO events (id, customer_id, event_type, occurred_at, payload, value_num) VALUES (?, ?, ?, {now}, ?, ?)",
                             [(a, b, c, e, f) for a, b, c, _, e, f in rows])
            conn.commit()
            conn.set_autocommit(True)
        except Exception as e:  # noqa: BLE001
            log(f"  (stats seed failed: {short_err(e)})")
            try:
                conn.rollback()
                conn.set_autocommit(True)
            except Exception:  # noqa: BLE001
                pass
        for ex in experiments(sz):
            rec: dict[str, Any] = {"title": ex.title, "what": ex.what, "variants": []}
            log(f"  {ex.id}: {ex.title}")
            for v in ex.variants:
                vrec: dict[str, Any] = {"name": v.name, "note": v.note}
                applied = []
                try:
                    for st in v.setup:
                        s = st(d)
                        if s is None:
                            raise Unsupported(f"{d.name}: setup step not supported")
                        if s == SKIP:
                            continue
                        for stmt in (s if isinstance(s, list) else [s]):
                            conn.execute(stmt, rendered=True, fetch=False)
                            applied.append(stmt)
                    vrec["setup"] = applied
                    rendered = render(v.sql, d)
                    vrec["sql"] = rendered
                    params = v.params or []
                    i = {"n": 0}
                    def fn(sql=v.sql, params=params):
                        p = params[i["n"] % len(params)] if params else None
                        i["n"] += 1
                        return conn.execute(sql, p)
                    t = measure(v.name, fn, warmup=2, iters=10, max_seconds=20)
                    vrec["ok"] = t.ok
                    vrec["error"] = t.error
                    vrec["p50_ms"] = round(t.stats.get("p50", 0), 3) if t.ok else None
                    vrec["p95_ms"] = round(t.stats.get("p95", 0), 3) if t.ok else None
                    vrec["rows"] = t.rows
                    if t.ok:
                        try:
                            plan = conn.explain(v.sql, params[0] if params else None, True)
                            vrec["plan_analyze"] = (plan or "")[:3000]
                        except Exception as e:  # noqa: BLE001
                            vrec["plan_error"] = short_err(e)
                            try:
                                conn.rollback()
                            except Exception:  # noqa: BLE001
                                pass
                        log(f"    {v.name:34s} p50={t.stats['p50']:9.3f} ms rows={t.rows}")
                    else:
                        log(f"    {v.name:34s} ERR {t.error[:80]}")
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                except Unsupported as e:
                    vrec["ok"] = False
                    vrec["status"] = "n/a"
                    vrec["error"] = str(e)
                    log(f"    {v.name:34s} n/a ({e})")
                except Exception as e:  # noqa: BLE001
                    vrec["ok"] = False
                    vrec["error"] = short_err(e)
                    log(f"    {v.name:34s} ERR {short_err(e)[:80]}")
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                finally:
                    for st in v.teardown:
                        try:
                            s = st(d)
                            if s and s != SKIP:
                                for stmt in (s if isinstance(s, list) else [s]):
                                    conn.execute(stmt, rendered=True, fetch=False)
                        except Exception:  # noqa: BLE001
                            pass
                rec["variants"].append(vrec)
            oks = [x for x in rec["variants"] if x.get("ok") and x.get("p50_ms")]
            if len(oks) >= 2:
                base = oks[0]["p50_ms"]
                best = min(oks, key=lambda x: x["p50_ms"])
                rec["best_variant"] = best["name"]
                rec["speedup_vs_first"] = round(base / best["p50_ms"], 2) if best["p50_ms"] else None
            out[ex.id] = rec
        try:
            conn.execute("DELETE FROM events WHERE event_type = 'bench_stats'", fetch=False)
        except Exception:  # noqa: BLE001
            try:
                conn.execute("ALTER TABLE events DELETE WHERE event_type = 'bench_stats'", fetch=False)
            except Exception:  # noqa: BLE001
                pass
        log("  opt10_batch_inserts")
        out["opt10_batch_inserts"] = _batch_insert_experiment(engine, conn, sz, log)
        log("  opt11_prepared_statements")
        out["opt11_prepared_statements"] = _prepared_experiment(engine, cfg, sz, log)
    finally:
        conn.close()
    return out
