"""Query catalog: portable SQL with dialect macros.

Each query exercises one or more SQL capabilities. `<<token>>` placeholders are filled from
the data sizes at run time (see `fill_tokens`), `{macro(args)}` tokens are dialect macros,
`?` are bind parameters.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

from .datagen import Sizes


@dataclass
class Query:
    id: str
    title: str
    tags: tuple[str, ...]
    sql: str | list[str]
    params: Callable[[Sizes, random.Random], list] | None = None   # -> list of param tuples (cycled per iteration)
    kind: str = "read"                                              # read | write | txn
    iters: int = 10
    warmup: int = 2
    max_seconds: float = 20.0
    note: str = ""
    cleanup: list[str] = field(default_factory=list)


def _ids(n: int, upper: int, seed: int) -> list[tuple]:
    r = random.Random(seed)
    return [(r.randint(1, upper),) for _ in range(n)]


CATALOG: list[Query] = [
    Query("q00_ping", "Round-trip floor (SELECT 1)", ("baseline",), "SELECT 1 {dual}", iters=30, warmup=3),
    Query("q01_pk_lookup", "Primary-key point lookup", ("baseline", "index"),
          "SELECT id, name, email, country_code, tier FROM customers WHERE id = ?",
          params=lambda sz, r: _ids(50, sz.customers, 1), iters=30, warmup=3),
    Query("q02_range_scan", "Range scan on unindexed timestamp + status filter", ("scan", "filter", "order"),
          "SELECT id, customer_id, status, total_amount FROM orders "
          "WHERE ordered_at >= {ts(2025-03-01 00:00:00)} AND ordered_at < {ts(2025-03-08 00:00:00)} AND status = 'delivered' "
          "ORDER BY ordered_at, id {limit(500)}"),
    Query("q03_join_agg", "3-way join + GROUP BY month/country + ORDER BY revenue", ("join", "aggregate", "date"),
          "SELECT c.country_code, {date_trunc_month(o.ordered_at)} AS month_start, COUNT(DISTINCT o.id) AS orders, SUM(oi.qty * oi.unit_price) AS revenue "
          "FROM orders o JOIN customers c ON c.id = o.customer_id JOIN order_items oi ON oi.order_id = o.id "
          "WHERE o.status = 'delivered' AND o.ordered_at >= {ts(2025-01-01 00:00:00)} AND o.ordered_at < {ts(2025-07-01 00:00:00)} "
          "GROUP BY c.country_code, {date_trunc_month(o.ordered_at)} "
          "ORDER BY revenue DESC, c.country_code, month_start {limit(50)}"),
    Query("q04_window_rank", "Top-3 products per category (ROW_NUMBER over aggregate)", ("window", "aggregate", "join"),
          "SELECT category_id, product_id, revenue, rn FROM ("
          "SELECT p.category_id, oi.product_id, SUM(oi.qty * oi.unit_price) AS revenue, "
          "ROW_NUMBER() OVER (PARTITION BY p.category_id ORDER BY SUM(oi.qty * oi.unit_price) DESC, oi.product_id) AS rn "
          "FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.category_id, oi.product_id"
          ") t WHERE rn <= 3 ORDER BY category_id, rn"),
    Query("q05_window_running", "Running total + LAG over daily revenue", ("window", "date"),
          "SELECT d, daily, SUM(daily) OVER (ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running, LAG(daily, 1) OVER (ORDER BY d) AS prev_day "
          "FROM (SELECT {date_trunc_day(ordered_at)} AS d, SUM(total_amount) AS daily FROM orders "
          "WHERE ordered_at >= {ts(2025-01-01 00:00:00)} AND ordered_at < {ts(2025-04-01 00:00:00)} GROUP BY {date_trunc_day(ordered_at)}) s "
          "ORDER BY d"),
    Query("q06_recursive_cte", "Recursive CTE over category tree (path strings)", ("recursive-cte", "hierarchy"),
          "{with_recursive} tree (id, parent_id, name, depth, path) AS ("
          "SELECT id, parent_id, name, 0, {cast_str(name,400)} FROM categories WHERE parent_id IS NULL "
          "UNION ALL "
          "SELECT c.id, c.parent_id, c.name, t.depth + 1, {cast_str({concat(t.path,' > ',c.name)},400)} FROM categories c JOIN tree t ON c.parent_id = t.id"
          ") SELECT id, depth, path FROM tree ORDER BY path"),
    Query("q07_cte_cohort", "Chained CTEs: cohort retention", ("cte", "aggregate", "join"),
          "WITH first_orders AS (SELECT customer_id, MIN(ordered_at) AS first_at FROM orders GROUP BY customer_id), "
          "cohorts AS (SELECT customer_id, {date_trunc_month(first_at)} AS cohort FROM first_orders), "
          "activity AS (SELECT o.customer_id, {date_trunc_month(o.ordered_at)} AS m FROM orders o GROUP BY o.customer_id, {date_trunc_month(o.ordered_at)}) "
          "SELECT c.cohort, COUNT(DISTINCT c.customer_id) AS cohort_size, COUNT(DISTINCT a.customer_id) AS active_later "
          "FROM cohorts c LEFT JOIN activity a ON a.customer_id = c.customer_id AND a.m > c.cohort "
          "GROUP BY c.cohort ORDER BY c.cohort"),
    Query("q08_exists_semijoin", "EXISTS / NOT EXISTS semi- and anti-joins", ("subquery", "semijoin"),
          "SELECT c.id, c.email FROM customers c "
          "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.ordered_at >= {ts(2025-11-01 00:00:00)}) "
          "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.customer_id = c.id AND e.event_type = 'churn') "
          "ORDER BY c.id {limit(200)}"),
    Query("q09_correlated_scalar", "Correlated scalar subquery per row", ("subquery", "correlated"),
          "SELECT p.id, p.price, (SELECT AVG(p2.price) FROM products p2 WHERE p2.category_id = p.category_id) AS cat_avg "
          "FROM products p WHERE {bool_eq_true(p.active)} ORDER BY p.id {limit(500)}"),
    Query("q10_lateral_topn", "LATERAL / CROSS APPLY top-N per customer", ("lateral", "topn"),
          "SELECT c.id AS customer_id, o.id AS order_id, o.ordered_at, o.total_amount FROM customers c {cross_lateral} ("
          "SELECT o2.id, o2.ordered_at, o2.total_amount FROM orders o2 WHERE o2.customer_id = c.id ORDER BY o2.ordered_at DESC, o2.id DESC {limit(3)}"
          ") o WHERE c.id <= 100 ORDER BY c.id, o.ordered_at DESC"),
    Query("q10b_topn_window", "Top-N per customer via ROW_NUMBER (portable alternative to LATERAL)", ("window", "topn"),
          "SELECT customer_id, id AS order_id, ordered_at, total_amount FROM ("
          "SELECT customer_id, id, ordered_at, total_amount, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY ordered_at DESC, id DESC) AS rn "
          "FROM orders WHERE customer_id <= 100) t WHERE rn <= 3 ORDER BY customer_id, ordered_at DESC"),
    Query("q11_grouping_sets", "GROUPING SETS / ROLLUP multi-level aggregate", ("grouping-sets", "aggregate"),
          "SELECT shipping_country, status, COUNT(*) AS n, SUM(total_amount) AS amt FROM orders "
          "WHERE ordered_at >= {ts(2025-06-01 00:00:00)} AND ordered_at < {ts(2025-09-01 00:00:00)} "
          "GROUP BY GROUPING SETS ((shipping_country, status), (shipping_country), ()) ORDER BY shipping_country, status"),
    Query("q12_json_filter", "Filter on JSON attributes (text + numeric extraction)", ("json",),
          "SELECT id, sku, price FROM products WHERE {json_get(attrs,color)} = 'red' AND {json_get_num(attrs,weight)} > 10 ORDER BY id {limit(100)}"),
    Query("q13_fulltext", "Full-text search on product descriptions", ("fulltext",),
          "SELECT id, name FROM products WHERE {fts(description,bluetooth waterproof)} ORDER BY id {limit(50)}"),
    Query("q14_upsert", "UPSERT (INSERT ... ON CONFLICT / ON DUPLICATE KEY / MERGE)", ("upsert", "write"),
          "INSERT INTO inventory (product_id, warehouse_id, qty, updated_at) VALUES (?, ?, ?, {now}) "
          "ON CONFLICT (product_id, warehouse_id) DO UPDATE SET qty = inventory.qty + EXCLUDED.qty, updated_at = {now}",
          params=lambda sz, r: [(r.randint(1, sz.products), r.randint(1, 4), r.randint(1, 5)) for _ in range(200)], kind="write", iters=30),
    Query("q15_merge", "SQL-standard MERGE with matched/not-matched branches", ("merge", "write"),
          "MERGE INTO inventory i USING (SELECT {cast_int(?)} AS product_id, {cast_int(?)} AS warehouse_id, {cast_int(?)} AS qty {dual}) s "
          "ON (i.product_id = s.product_id AND i.warehouse_id = s.warehouse_id) "
          "WHEN MATCHED THEN UPDATE SET qty = i.qty + s.qty, updated_at = {now} "
          "WHEN NOT MATCHED THEN INSERT (product_id, warehouse_id, qty, updated_at) VALUES (s.product_id, s.warehouse_id, s.qty, {now}){merge_end}",
          params=lambda sz, r: [(r.randint(1, sz.products), r.randint(1, 4), r.randint(1, 5)) for _ in range(200)], kind="write", iters=30),
    Query("q16_offset_pagination", "Deep OFFSET pagination (anti-pattern)", ("pagination", "order"),
          "SELECT id, customer_id, ordered_at FROM orders ORDER BY ordered_at, id {offset_limit(<<half_orders>>,50)}"),
    Query("q17_keyset_pagination", "Keyset pagination (expanded predicate)", ("pagination", "order", "index"),
          "SELECT id, customer_id, ordered_at FROM orders WHERE (ordered_at > {ts(2025-01-01 00:00:00)} OR (ordered_at = {ts(2025-01-01 00:00:00)} AND id > 0)) "
          "ORDER BY ordered_at, id {limit(50)}"),
    Query("q17b_keyset_rowvalue", "Keyset pagination with row-value comparison", ("pagination", "row-value"),
          "SELECT id, customer_id, ordered_at FROM orders WHERE (ordered_at, id) > ({ts(2025-01-01 00:00:00)}, 0) ORDER BY ordered_at, id {limit(50)}"),
    Query("q18_count_distinct", "COUNT(DISTINCT) over 1M-row table", ("aggregate", "distinct", "scan"),
          "SELECT COUNT(DISTINCT customer_id) AS uniq FROM events WHERE occurred_at >= {ts(2025-01-01 00:00:00)}"),
    Query("q19_anti_join", "Anti-join via LEFT JOIN ... IS NULL", ("antijoin", "join"),
          "SELECT p.id FROM products p LEFT JOIN order_items oi ON oi.product_id = p.id WHERE oi.id IS NULL ORDER BY p.id"),
    Query("q19b_not_exists", "Anti-join via NOT EXISTS", ("antijoin", "subquery"),
          "SELECT p.id FROM products p WHERE NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.product_id = p.id) ORDER BY p.id"),
    Query("q20_gaps_islands", "Sessionisation (gaps-and-islands with LAG)", ("window", "date"),
          "SELECT customer_id, COUNT(*) AS sessions FROM ("
          "SELECT customer_id, occurred_at, prev_at, CASE WHEN prev_at IS NULL OR {epoch_diff_seconds(occurred_at,prev_at)} > 1800 THEN 1 ELSE 0 END AS new_session "
          "FROM (SELECT customer_id, occurred_at, LAG(occurred_at) OVER (PARTITION BY customer_id ORDER BY occurred_at, id) AS prev_at FROM events WHERE customer_id <= 2000) w"
          ") s WHERE new_session = 1 GROUP BY customer_id ORDER BY customer_id"),
    Query("q21_percentile", "Median (PERCENTILE_CONT) per country", ("aggregate", "percentile"),
          "SELECT shipping_country, {percentile_cont(total_amount,0.5)} AS median FROM orders GROUP BY shipping_country ORDER BY shipping_country"),
    Query("q22_string_agg", "String aggregation (STRING_AGG / GROUP_CONCAT / LISTAGG)", ("aggregate", "string"),
          "SELECT o.id, {string_agg_ordered(p.sku,',')} AS skus FROM orders o JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id "
          "WHERE o.id <= 500 GROUP BY o.id ORDER BY o.id"),
    Query("q23_case_pivot", "Conditional aggregation pivot", ("aggregate", "pivot", "join"),
          "SELECT c.country_code, SUM(CASE WHEN o.status = 'delivered' THEN 1 ELSE 0 END) AS delivered, "
          "SUM(CASE WHEN o.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled, SUM(CASE WHEN o.status = 'returned' THEN 1 ELSE 0 END) AS returned, "
          "AVG(o.total_amount) AS avg_amount FROM orders o JOIN customers c ON c.id = o.customer_id GROUP BY c.country_code ORDER BY c.country_code"),
    Query("q24_or_predicate", "OR across two indexed columns", ("filter", "or"),
          "SELECT id FROM orders WHERE customer_id = ? OR id = ? ORDER BY id",
          params=lambda sz, r: [(r.randint(1, sz.customers), r.randint(1, sz.orders)) for _ in range(50)], iters=20),
    Query("q25_in_list", "IN list with 500 literals", ("filter", "in-list"),
          "SELECT id, total_amount FROM orders WHERE id IN (<<in_list_500>>) ORDER BY id"),
    Query("q26_nonsargable", "Non-sargable predicate: YEAR()/MONTH() on column", ("filter", "sargability", "scan"),
          "SELECT COUNT(*) FROM orders WHERE {year(ordered_at)} = 2025 AND {month(ordered_at)} = 5"),
    Query("q27_update_single", "Single-row UPDATE (autocommit)", ("write", "update"), "UPDATE customers SET tier = ? WHERE id = ?",
          params=lambda sz, r: [(r.choice(["free", "plus", "pro", "vip"]), r.randint(1, sz.customers)) for _ in range(500)], kind="write", iters=30),
    Query("q28_insert_single", "Single-row INSERT (autocommit)", ("write", "insert"),
          "INSERT INTO events (id, customer_id, event_type, occurred_at, payload, value_num) VALUES (?, ?, 'bench', {now}, NULL, ?)",
          params=lambda sz, r: [(10_000_000_000 + r.randint(1, 10**9) * 100 + i, r.randint(1, sz.customers), r.randint(0, 100)) for i in range(2000)],
          kind="write", iters=30, cleanup=["DELETE FROM events WHERE id >= 10000000000"]),
    Query("q29_txn_order", "Multi-statement transaction: order + 2 items + inventory update", ("write", "transaction"),
          ["INSERT INTO orders (id, customer_id, status, ordered_at, total_amount, shipping_country) VALUES (?, ?, 'pending', {now}, ?, 'US')",
           "INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (?, ?, ?, 1, ?)",
           "INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (?, ?, ?, 1, ?)",
           "UPDATE inventory SET qty = qty - 1, updated_at = {now} WHERE product_id = ? AND warehouse_id = 1"],
          params=lambda sz, r: [[(oid, r.randint(1, sz.customers), 99.0), (oid * 10 + 1, oid, p1, 49.5), (oid * 10 + 2, oid, p2, 49.5), (p1,)]
                                for oid, p1, p2 in ((100_000_000 + r.randint(0, 50_000_000) + i, r.randint(1, sz.products), r.randint(1, sz.products)) for i in range(2000))],
          kind="txn", iters=30, cleanup=["DELETE FROM order_items WHERE order_id >= 100000000", "DELETE FROM orders WHERE id >= 100000000"]),
]

CATALOG_BY_ID = {q.id: q for q in CATALOG}


def fill_tokens(sql: str, sz: Sizes, rng: random.Random) -> str:
    if "<<" not in sql:
        return sql
    ids = sorted(rng.sample(range(1, sz.orders + 1), 500))
    tokens = {
        "half_orders": str(sz.orders // 2),
        "in_list_500": ", ".join(str(i) for i in ids),
    }
    for k, v in tokens.items():
        sql = sql.replace(f"<<{k}>>", v)
    return sql
