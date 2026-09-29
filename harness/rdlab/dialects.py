"""SQL dialect knowledge: type maps, DDL, macro rendering, EXPLAIN syntax, capability probes.

Queries in the catalog are written once in portable SQL with `{macro(args)}` tokens and `?`
parameter placeholders. `render()` expands macros for a dialect, `bind()` converts the
placeholders to the driver's paramstyle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema import Col, Table, parse_ptype

_MACRO = re.compile(r"\{([a-z_][a-z0-9_]*)(?:\(([^{}]*)\))?\}")


class Unsupported(Exception):
    """Raised when a dialect has no syntax for a construct."""


def split_args(s: str) -> list[str]:
    out, depth, cur, quote = [], 0, [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur or out:
        out.append("".join(cur).strip())
    return out


def render(sql: str, d: "Dialect") -> str:
    """Expand {macro(args)} tokens innermost-first."""
    for _ in range(20):
        m = _MACRO.search(sql)
        if not m:
            return sql
        def repl(mm: re.Match) -> str:
            name, args = mm.group(1), mm.group(2)
            fn = getattr(d, "m_" + name, None)
            if fn is None:
                raise Unsupported(f"{d.name}: unknown macro {name}")
            res = fn(*split_args(args)) if args is not None else fn()
            if res is None:
                raise Unsupported(f"{d.name}: no syntax for {name}")
            return res
        sql = _MACRO.sub(repl, sql)
    return sql


def bind(sql: str, paramstyle: str, nparams: int = 0) -> str:
    """Convert `?` placeholders to the driver paramstyle (escaping % where needed)."""
    if paramstyle == "qmark":
        return sql
    if paramstyle in ("format", "pyformat"):
        # drivers only interpret % when parameters are passed, so only escape then
        if nparams > 0:
            sql = sql.replace("%", "%%")
        if paramstyle == "format":
            return sql.replace("?", "%s") if nparams > 0 else sql
        i = [0]
        def rp(_m):
            i[0] += 1
            return f"%(p{i[0]})s"
        return re.sub(r"\?", rp, sql)
    if paramstyle == "numeric":
        i = [0]
        def rn(_m):
            i[0] += 1
            return f":{i[0]}"
        return re.sub(r"\?", rn, sql)
    if paramstyle == "named":
        i = [0]
        def rd(_m):
            i[0] += 1
            return f":p{i[0]}"
        return re.sub(r"\?", rd, sql)
    raise ValueError(paramstyle)


@dataclass
class Probe:
    name: str
    sql: list[str]                     # statements executed in order (each may be a macro'd string)
    cleanup: list[str] = field(default_factory=list)
    expect_error: bool = False         # probe passes if the LAST statement raises
    note: str = ""
    fetch_last: bool = False


class Dialect:
    name = "ansi"
    family = "ansi"
    paramstyle = "qmark"
    supports_fk = True
    supports_pk = True
    supports_create_index = True
    supports_transactions = True
    with_recursive = "WITH RECURSIVE"
    cross_lateral: str | None = "CROSS JOIN LATERAL"
    merge_terminator = ""
    multirow_insert = True
    type_map: dict[str, str] = {
        "int": "INTEGER", "bigint": "BIGINT", "varchar": "VARCHAR({n})", "text": "VARCHAR(4000)",
        "decimal": "DECIMAL({p},{s})", "bool": "BOOLEAN", "timestamp": "TIMESTAMP", "json": "VARCHAR(4000)",
    }
    explain_plain: str | None = "EXPLAIN {sql}"
    explain_analyze: str | None = None
    fts_kind = "like-fallback"          # native | like-fallback | none

    # ---------- types & DDL ----------
    def sql_type(self, col: Col) -> str:
        base, args = parse_ptype(col.ptype)
        fmt = self.type_map[base]
        if base == "varchar":
            return fmt.format(n=args[0])
        if base == "decimal":
            return fmt.format(p=args[0], s=args[1])
        return fmt

    def col_sql(self, col: Col, table: Table) -> str:
        s = f"{col.name} {self.sql_type(col)}"
        if not col.nullable:
            s += " NOT NULL"
        return s

    def table_suffix(self, table: Table) -> str:
        return ""

    def create_table(self, table: Table) -> str:
        parts = [self.col_sql(c, table) for c in table.cols]
        if self.supports_pk and table.pk:
            parts.append(f"PRIMARY KEY ({', '.join(table.pk)})")
        if self.supports_fk:
            for col, rt, rc in table.fks:
                parts.append(f"FOREIGN KEY ({col}) REFERENCES {rt} ({rc})")
        return f"CREATE TABLE {table.name} (\n  " + ",\n  ".join(parts) + "\n)" + self.table_suffix(table)

    def create_index(self, name: str, table: str, cols: list[str], *, unique: bool = False,
                     where: str | None = None, include: list[str] | None = None) -> str | None:
        if not self.supports_create_index:
            return None
        u = "UNIQUE " if unique else ""
        s = f"CREATE {u}INDEX {name} ON {table} ({', '.join(cols)})"
        if include:
            s += self.index_include(include)
        if where:
            s += self.index_where(where)
        return s

    def index_include(self, cols: list[str]) -> str:
        raise Unsupported(f"{self.name}: INCLUDE columns not supported")

    def index_where(self, where: str) -> str:
        raise Unsupported(f"{self.name}: partial indexes not supported")

    def drop_index(self, name: str, table: str) -> str:
        return f"DROP INDEX {name}"

    def drop_table(self, table: str) -> str:
        return f"DROP TABLE {table}"

    def drop_table_if_exists(self, table: str) -> str:
        return f"DROP TABLE IF EXISTS {table}"

    def schema_ddl(self, tables: list[Table]) -> list[str]:
        stmts: list[str] = []
        for t in tables:
            stmts.append(self.create_table(t))
        for t in tables:
            for i, cols in enumerate(t.uniques):
                s = self.create_index(f"ux_{t.name}_{'_'.join(cols)}", t.name, list(cols), unique=True)
                if s:
                    stmts.extend(s if isinstance(s, list) else [s])
            for cols in t.indexes:
                s = self.create_index(f"ix_{t.name}_{'_'.join(cols)}", t.name, list(cols))
                if s:
                    stmts.extend(s if isinstance(s, list) else [s])
        return stmts

    def analyze_table(self, table: str) -> str | None:
        return None

    def truncate(self, table: str) -> str:
        return f"TRUNCATE TABLE {table}"

    # ---------- EXPLAIN ----------
    def explain(self, sql: str, analyze: bool) -> str | None:
        tpl = self.explain_analyze if analyze else self.explain_plain
        return tpl.format(sql=sql) if tpl else None

    # ---------- macros ----------
    def m_limit(self, n): return f"FETCH FIRST {n} ROWS ONLY"
    def m_offset_limit(self, off, n): return f"OFFSET {off} ROWS FETCH NEXT {n} ROWS ONLY"
    def m_json_get(self, col, key): return f"JSON_VALUE({col}, '$.{key}')"
    def m_json_get_num(self, col, key): return f"CAST(JSON_VALUE({col}, '$.{key}') AS DECIMAL(12,2))"
    def m_date_trunc_day(self, col): return f"CAST({col} AS DATE)"
    def m_date_trunc_month(self, col): return f"CAST(SUBSTRING(CAST({col} AS VARCHAR(30)) FROM 1 FOR 7) || '-01' AS DATE)"
    def m_year(self, col): return f"EXTRACT(YEAR FROM {col})"
    def m_month(self, col): return f"EXTRACT(MONTH FROM {col})"
    def m_date_sub_days(self, col, n): return f"{col} - INTERVAL '{n}' DAY"
    def m_epoch_diff_seconds(self, a, b): return None
    def m_string_agg_ordered(self, col, sep): return f"LISTAGG({col}, {sep}) WITHIN GROUP (ORDER BY {col})"
    def m_concat(self, *args): return " || ".join(args)
    def m_cast_str(self, expr, n): return f"CAST({expr} AS VARCHAR({n}))"
    def m_cast_int(self, expr): return f"CAST({expr} AS INTEGER)"
    def m_ts(self, lit): return f"TIMESTAMP '{lit}'"
    def m_now(self): return "CURRENT_TIMESTAMP"
    def m_true(self): return "TRUE"
    def m_false(self): return "FALSE"
    def m_mod(self, a, b): return f"MOD({a}, {b})"
    def m_dual(self): return ""
    def m_with_recursive(self): return self.with_recursive
    def m_cross_lateral(self): return self.cross_lateral
    def m_percentile_cont(self, col, q): return f"PERCENTILE_CONT({q}) WITHIN GROUP (ORDER BY {col})"
    def m_fts(self, col, words): return " AND ".join(f"{col} LIKE '%{w}%'" for w in words.split())
    def m_bool_eq_true(self, col): return f"{col} = TRUE"
    def m_sleep(self, seconds): return None
    def m_merge_end(self): return self.merge_terminator
    def m_keyset_pred(self, c1, c2, v1, v2): return f"({c1}, {c2}) > ({v1}, {v2})"

    # ---------- query overrides & probes ----------
    def query_overrides(self) -> dict[str, str | None]:
        """qid -> full SQL override, or None to mark unsupported."""
        return {}

    def probes(self) -> list[Probe]:
        return []


# =====================================================================================
class Postgres(Dialect):
    name = "postgres"
    family = "postgres"
    paramstyle = "format"
    type_map = Dialect.type_map | {"text": "TEXT", "json": "JSONB", "timestamp": "TIMESTAMP(6)"}
    explain_plain = "EXPLAIN (FORMAT TEXT) {sql}"
    explain_analyze = "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}"
    fts_kind = "native"

    def index_include(self, cols): return f" INCLUDE ({', '.join(cols)})"
    def index_where(self, where): return f" WHERE {where}"
    def analyze_table(self, table): return f"ANALYZE {table}"
    def m_limit(self, n): return f"LIMIT {n}"
    def m_offset_limit(self, off, n): return f"LIMIT {n} OFFSET {off}"
    def m_json_get(self, col, key): return f"({col}->>'{key}')"
    def m_json_get_num(self, col, key): return f"(({col}->>'{key}')::numeric)"
    def m_date_trunc_day(self, col): return f"DATE_TRUNC('day', {col})"
    def m_date_trunc_month(self, col): return f"DATE_TRUNC('month', {col})"
    def m_date_sub_days(self, col, n): return f"{col} - INTERVAL '{n} days'"
    def m_epoch_diff_seconds(self, a, b): return f"EXTRACT(EPOCH FROM ({a} - {b}))"
    def m_string_agg_ordered(self, col, sep): return f"STRING_AGG({col}, {sep} ORDER BY {col})"
    def m_fts(self, col, words): return f"to_tsvector('english', {col}) @@ plainto_tsquery('english', '{words}')"
    def m_sleep(self, seconds): return f"SELECT pg_sleep({seconds})"

    def query_overrides(self):
        return {
            "q14_upsert": ("INSERT INTO inventory (product_id, warehouse_id, qty, updated_at) VALUES (?, ?, ?, {now}) "
                           "ON CONFLICT (product_id, warehouse_id) DO UPDATE SET qty = inventory.qty + EXCLUDED.qty, updated_at = {now}"),
            "q17b_keyset_rowvalue": ("SELECT id, customer_id, ordered_at FROM orders WHERE (ordered_at, id) > ({ts(2025-01-01 00:00:00)}, 0) "
                                     "ORDER BY ordered_at, id {limit(50)}"),
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab AS SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code",
                                        "REFRESH MATERIALIZED VIEW mv_lab", "SELECT COUNT(*) FROM mv_lab"], ["DROP MATERIALIZED VIEW mv_lab"], fetch_last=True),
            Probe("partitioning", ["CREATE TABLE part_lab (id INT, d DATE) PARTITION BY RANGE (d)",
                                   "CREATE TABLE part_lab_2025 PARTITION OF part_lab FOR VALUES FROM ('2025-01-01') TO ('2026-01-01')",
                                   "INSERT INTO part_lab VALUES (1, '2025-06-01')"], ["DROP TABLE part_lab"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a INT, b INT GENERATED ALWAYS AS (a * 2) STORED)", "INSERT INTO gen_lab (a) VALUES (21)", "SELECT b FROM gen_lab"], ["DROP TABLE gen_lab"], fetch_last=True),
            Probe("identity", ["CREATE TABLE id_lab (id INT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, v INT)", "INSERT INTO id_lab (v) VALUES (1)", "SELECT id FROM id_lab"], ["DROP TABLE id_lab"], fetch_last=True),
            Probe("returning", ["CREATE TABLE ret_lab (id INT GENERATED BY DEFAULT AS IDENTITY, v INT)", "INSERT INTO ret_lab (v) VALUES (7) RETURNING id, v"], ["DROP TABLE ret_lab"], fetch_last=True),
            Probe("sequence", ["CREATE SEQUENCE seq_lab", "SELECT nextval('seq_lab')"], ["DROP SEQUENCE seq_lab"], fetch_last=True),
            Probe("arrays", ["SELECT ARRAY[1,2,3] || ARRAY[4]"], fetch_last=True),
            Probe("vector_type", ["CREATE EXTENSION IF NOT EXISTS vector", "CREATE TABLE vec_lab (e vector(3))", "INSERT INTO vec_lab VALUES ('[1,2,3]')",
                                  "SELECT e <-> '[3,1,2]' FROM vec_lab"], ["DROP TABLE vec_lab"], fetch_last=True, note="requires pgvector extension"),
            Probe("statement_timeout", ["SET statement_timeout = 200", "SELECT pg_sleep(2)"], ["SET statement_timeout = 0"], expect_error=True, note="expects SQLSTATE 57014"),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders WHERE customer_id <= 20 ORDER BY customer_id, ordered_at DESC"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v INT CHECK (v > 0))", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab"], expect_error=True),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["DELETE FROM order_items WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["CREATE TABLE sp_lab (v INT)", "INSERT INTO sp_lab VALUES (1)", "SAVEPOINT s1", "INSERT INTO sp_lab VALUES (2)", "ROLLBACK TO SAVEPOINT s1", "SELECT COUNT(*) FROM sp_lab"], ["DROP TABLE sp_lab"], fetch_last=True, note="expects 1"),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF SYSTEM TIME '-5s'"], fetch_last=True, note="time-travel"),
            Probe("json_aggregate", ["SELECT json_agg(t) FROM (SELECT id, name FROM categories WHERE id <= 3) t"], fetch_last=True),
            Probe("isolation_serializable", ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", "SELECT 1"], fetch_last=True),
            Probe("isolation_repeatable_read", ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", "SELECT 1"], fetch_last=True),
            Probe("isolation_read_uncommitted", ["SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED", "SELECT 1"], fetch_last=True),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE name ~* '^ultra'"], fetch_last=True),
            Probe("cte_dml", ["WITH moved AS (DELETE FROM inventory WHERE product_id = -1 RETURNING *) SELECT COUNT(*) FROM moved"], fetch_last=True),
            Probe("server_cursor", ["DECLARE cur_lab CURSOR FOR SELECT id FROM orders ORDER BY id", "FETCH 5 FROM cur_lab", "CLOSE cur_lab"], note="in-transaction cursor"),
        ]


class Cockroach(Postgres):
    name = "cockroach"
    explain_plain = "EXPLAIN {sql}"
    explain_analyze = "EXPLAIN ANALYZE {sql}"
    def analyze_table(self, table): return f"ANALYZE {table}"
    def m_sleep(self, seconds): return f"SELECT pg_sleep({seconds})"
    def m_percentile_cont(self, col, q): return f"PERCENTILE_CONT({q}) WITHIN GROUP (ORDER BY {col}::FLOAT8)"

    def query_overrides(self):
        o = super().query_overrides()
        o["q11_grouping_sets"] = None      # GROUPING SETS / ROLLUP / CUBE not implemented
        o["q15_merge"] = None              # no MERGE statement (use INSERT ... ON CONFLICT)
        return o

    def probes(self):
        ps = [p for p in super().probes() if p.name not in {"vector_type", "server_cursor", "materialized_view", "partitioning"}]
        ps += [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab AS SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code",
                                        "REFRESH MATERIALIZED VIEW mv_lab", "SELECT COUNT(*) FROM mv_lab"], ["DROP MATERIALIZED VIEW mv_lab"], fetch_last=True),
            Probe("partitioning", ["CREATE TABLE part_lab (id INT, d DATE, PRIMARY KEY (d, id)) PARTITION BY RANGE (d) (PARTITION p2025 VALUES FROM ('2025-01-01') TO ('2026-01-01'), PARTITION pother VALUES FROM ('2026-01-01') TO (MAXVALUE))",
                                   "INSERT INTO part_lab VALUES (1, '2025-06-01')"], ["DROP TABLE part_lab"], note="enterprise feature (trial license)"),
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3))", "INSERT INTO vec_lab VALUES ('[1,2,3]')", "SELECT e <-> '[3,1,2]' FROM vec_lab"], ["DROP TABLE vec_lab"], fetch_last=True),
            Probe("follower_read", ["SELECT COUNT(*) FROM orders AS OF SYSTEM TIME follower_read_timestamp()"], fetch_last=True),
        ]
        return ps


class Yugabyte(Postgres):
    name = "yugabyte"
    explain_analyze = "EXPLAIN (ANALYZE, DIST) {sql}"


class Citus(Postgres):
    """PostgreSQL + Citus: same syntax, but foreign keys between non-colocated distributed tables are not allowed,
    so the lab schema is created without FKs and then distributed (see PsycopgEngine.create_schema)."""
    name = "citus"
    supports_fk = False
    # Citus requires PRIMARY KEY / UNIQUE constraints to include the distribution column, so every table is
    # hash-distributed on its primary key (joins on other columns become repartition joins).
    DISTRIBUTION = [("categories", None), ("products", None),                     # reference tables (replicated to every worker)
                    ("customers", "id"), ("orders", "id"), ("events", "id"), ("order_items", "id"), ("inventory", "product_id")]

    def create_index(self, name, table, cols, *, unique=False, where=None, include=None):
        # UNIQUE(email) on customers cannot be enforced across shards: keep it as a plain index
        return super().create_index(name, table, cols, unique=False, where=where, include=include)

    def distribute_statements(self) -> list[str]:
        out = []
        for t, col in self.DISTRIBUTION:
            out.append(f"SELECT create_reference_table('{t}')" if col is None else f"SELECT create_distributed_table('{t}', '{col}')")
        return out

    def probes(self):
        return [p for p in super().probes() if p.name not in {"partitioning", "server_cursor", "cte_dml"}] + [
            Probe("citus_shards", ["SELECT count(*) FROM citus_shards"], fetch_last=True, note="distributed shard count"),
            Probe("citus_secondary_nodes", ["SELECT count(*) FROM pg_dist_node WHERE noderole = 'secondary' AND isactive"], fetch_last=True, note="replica nodes registered for reads (use options='-c citus.use_secondary_nodes=always' at connect time)"),
        ]


# =====================================================================================
class MySQL(Dialect):
    name = "mysql"
    family = "mysql"
    paramstyle = "format"
    cross_lateral = "CROSS JOIN LATERAL"
    type_map = Dialect.type_map | {"text": "TEXT", "json": "JSON", "timestamp": "DATETIME(6)", "bool": "BOOLEAN"}
    explain_plain = "EXPLAIN FORMAT=TREE {sql}"
    explain_analyze = "EXPLAIN ANALYZE {sql}"
    fts_kind = "native"

    def table_suffix(self, table): return " ENGINE=InnoDB"
    def drop_index(self, name, table): return f"DROP INDEX {name} ON {table}"
    def analyze_table(self, table): return f"ANALYZE TABLE {table}"
    def m_limit(self, n): return f"LIMIT {n}"
    def m_offset_limit(self, off, n): return f"LIMIT {n} OFFSET {off}"
    def m_json_get(self, col, key): return f"JSON_UNQUOTE(JSON_EXTRACT({col}, '$.{key}'))"
    def m_json_get_num(self, col, key): return f"CAST(JSON_EXTRACT({col}, '$.{key}') AS DECIMAL(12,2))"
    def m_date_trunc_day(self, col): return f"DATE({col})"
    def m_date_trunc_month(self, col): return f"DATE_FORMAT({col}, '%Y-%m-01')"
    def m_year(self, col): return f"YEAR({col})"
    def m_month(self, col): return f"MONTH({col})"
    def m_date_sub_days(self, col, n): return f"{col} - INTERVAL {n} DAY"
    def m_epoch_diff_seconds(self, a, b): return f"TIMESTAMPDIFF(SECOND, {b}, {a})"
    def m_string_agg_ordered(self, col, sep): return f"GROUP_CONCAT({col} ORDER BY {col} SEPARATOR {sep})"
    def m_concat(self, *args): return f"CONCAT({', '.join(args)})"
    def m_cast_str(self, expr, n): return f"CAST({expr} AS CHAR({n}))"
    def m_cast_int(self, expr): return f"CAST({expr} AS SIGNED)"
    def m_percentile_cont(self, col, q): return None
    def m_fts(self, col, words): return f"MATCH({col}) AGAINST('{words}' IN NATURAL LANGUAGE MODE)"
    def m_dual(self): return "FROM DUAL"
    def m_sleep(self, seconds): return f"SELECT SLEEP({seconds})"

    def query_overrides(self):
        return {
            "q11_grouping_sets": ("SELECT shipping_country, status, COUNT(*) AS n, SUM(total_amount) AS amt FROM orders "
                                  "WHERE ordered_at >= {ts(2025-06-01 00:00:00)} AND ordered_at < {ts(2025-09-01 00:00:00)} "
                                  "GROUP BY shipping_country, status WITH ROLLUP"),
            "q14_upsert": ("INSERT INTO inventory (product_id, warehouse_id, qty, updated_at) VALUES (?, ?, ?, {now}) AS new "
                           "ON DUPLICATE KEY UPDATE qty = inventory.qty + new.qty, updated_at = {now}"),
            "q15_merge": None,
            "q21_percentile": None,
            "q17b_keyset_rowvalue": ("SELECT id, customer_id, ordered_at FROM orders WHERE (ordered_at, id) > ({ts(2025-01-01 00:00:00)}, 0) "
                                     "ORDER BY ordered_at, id {limit(50)}"),
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab AS SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code"], ["DROP MATERIALIZED VIEW mv_lab"]),
            Probe("partitioning", ["CREATE TABLE part_lab (id INT, d DATE) PARTITION BY RANGE (YEAR(d)) (PARTITION p2025 VALUES LESS THAN (2026), PARTITION pmax VALUES LESS THAN MAXVALUE)",
                                   "INSERT INTO part_lab VALUES (1, '2025-06-01')"], ["DROP TABLE part_lab"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a INT, b INT GENERATED ALWAYS AS (a * 2) STORED)", "INSERT INTO gen_lab (a) VALUES (21)", "SELECT b FROM gen_lab"], ["DROP TABLE gen_lab"], fetch_last=True),
            Probe("identity", ["CREATE TABLE id_lab (id INT AUTO_INCREMENT PRIMARY KEY, v INT)", "INSERT INTO id_lab (v) VALUES (1)", "SELECT id FROM id_lab"], ["DROP TABLE id_lab"], fetch_last=True),
            Probe("returning", ["CREATE TABLE ret_lab (id INT AUTO_INCREMENT PRIMARY KEY, v INT)", "INSERT INTO ret_lab (v) VALUES (7) RETURNING id, v"], ["DROP TABLE ret_lab"], fetch_last=True),
            Probe("sequence", ["CREATE SEQUENCE seq_lab", "SELECT NEXT VALUE FOR seq_lab"], ["DROP SEQUENCE seq_lab"], fetch_last=True),
            Probe("arrays", ["SELECT ARRAY[1,2,3]"], fetch_last=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3))", "INSERT INTO vec_lab VALUES (STRING_TO_VECTOR('[1,2,3]'))", "SELECT DISTANCE(e, STRING_TO_VECTOR('[3,1,2]'), 'COSINE') FROM vec_lab"], ["DROP TABLE vec_lab"], fetch_last=True),
            Probe("statement_timeout", ["SET SESSION max_execution_time = 200", "SELECT SLEEP(2)"], ["SET SESSION max_execution_time = 0"], expect_error=True, note="expects error 3024"),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders WHERE customer_id <= 20 ORDER BY customer_id, ordered_at DESC"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v INT CHECK (v > 0))", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab"], expect_error=True),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["DELETE FROM order_items WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["CREATE TABLE sp_lab (v INT)", "INSERT INTO sp_lab VALUES (1)", "SAVEPOINT s1", "INSERT INTO sp_lab VALUES (2)", "ROLLBACK TO SAVEPOINT s1", "SELECT COUNT(*) FROM sp_lab"], ["DROP TABLE sp_lab"], fetch_last=True),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF TIMESTAMP NOW() - INTERVAL 5 SECOND"], fetch_last=True, note="TiDB stale read syntax"),
            Probe("json_aggregate", ["SELECT JSON_ARRAYAGG(JSON_OBJECT('id', id, 'name', name)) FROM categories WHERE id <= 3"], fetch_last=True),
            Probe("isolation_serializable", ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", "SELECT 1"], fetch_last=True),
            Probe("isolation_repeatable_read", ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", "SELECT 1"], fetch_last=True),
            Probe("isolation_read_uncommitted", ["SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED", "SELECT 1"], fetch_last=True),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE name REGEXP '^Ultra'"], fetch_last=True),
            Probe("invisible_index", ["CREATE INDEX ix_inv_lab ON customers (tier) INVISIBLE", "ALTER TABLE customers ALTER INDEX ix_inv_lab VISIBLE"], ["DROP INDEX ix_inv_lab ON customers"]),
        ]


class MariaDB(MySQL):
    name = "mariadb"
    cross_lateral = None
    explain_plain = "EXPLAIN {sql}"
    explain_analyze = "ANALYZE FORMAT=JSON {sql}"
    def m_percentile_cont(self, col, q): return None
    def m_sleep(self, seconds): return f"SELECT SLEEP({seconds})"

    def query_overrides(self):
        o = super().query_overrides()
        o["q14_upsert"] = ("INSERT INTO inventory (product_id, warehouse_id, qty, updated_at) VALUES (?, ?, ?, {now}) "
                           "ON DUPLICATE KEY UPDATE qty = qty + VALUES(qty), updated_at = {now}")
        o["q21_percentile"] = ("SELECT DISTINCT shipping_country, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_amount) OVER (PARTITION BY shipping_country) AS median "
                               "FROM orders ORDER BY shipping_country")
        return o

    def probes(self):
        ps = [p for p in super().probes() if p.name not in {"statement_timeout", "temporal_query", "vector_type", "invisible_index"}]
        ps += [
            Probe("statement_timeout", ["SET SESSION max_statement_time = 0.2", "SELECT SLEEP(2)"], ["SET SESSION max_statement_time = 0"], expect_error=True, note="expects error 1969"),
            Probe("temporal_query", ["CREATE TABLE sv_lab (v INT) WITH SYSTEM VERSIONING", "INSERT INTO sv_lab VALUES (1)", "UPDATE sv_lab SET v = 2",
                                     "SELECT COUNT(*) FROM sv_lab FOR SYSTEM_TIME ALL"], ["DROP TABLE sv_lab"], fetch_last=True, note="system-versioned tables"),
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3) NOT NULL)", "INSERT INTO vec_lab VALUES (VEC_FromText('[1,2,3]'))", "SELECT VEC_DISTANCE_EUCLIDEAN(e, VEC_FromText('[3,1,2]')) FROM vec_lab"], ["DROP TABLE vec_lab"], fetch_last=True),
            Probe("invisible_index", ["CREATE INDEX ix_inv_lab ON customers (tier)", "ALTER TABLE customers ALTER INDEX ix_inv_lab IGNORED"], ["DROP INDEX ix_inv_lab ON customers"]),
        ]
        return ps


class TiDB(MySQL):
    name = "tidb"
    explain_plain = "EXPLAIN {sql}"
    explain_analyze = "EXPLAIN ANALYZE {sql}"

    def probes(self):
        ps = [p for p in super().probes() if p.name not in {"vector_type", "temporal_query"}]
        ps += [
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3))", "INSERT INTO vec_lab VALUES ('[1,2,3]')", "SELECT VEC_COSINE_DISTANCE(e, '[3,1,2]') FROM vec_lab"], ["DROP TABLE vec_lab"], fetch_last=True, note="TiDB 8.4+ VECTOR"),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF TIMESTAMP NOW() - INTERVAL 5 SECOND"], fetch_last=True, note="TiDB stale read"),
        ]
        return ps


# =====================================================================================
class TSQL(Dialect):
    name = "tsql"
    family = "tsql"
    paramstyle = "format"
    cross_lateral = "CROSS APPLY"
    with_recursive = "WITH"
    merge_terminator = ";"
    type_map = {"int": "INT", "bigint": "BIGINT", "varchar": "NVARCHAR({n})", "text": "NVARCHAR(MAX)",
                "decimal": "DECIMAL({p},{s})", "bool": "BIT", "timestamp": "DATETIME2(6)", "json": "NVARCHAR(MAX)"}
    explain_plain = None       # handled by the engine adapter (SET SHOWPLAN_XML / STATISTICS XML)
    explain_analyze = None
    fts_kind = "native"

    def index_include(self, cols): return f" INCLUDE ({', '.join(cols)})"
    def index_where(self, where): return f" WHERE {where}"
    def drop_index(self, name, table): return f"DROP INDEX {name} ON {table}"
    def analyze_table(self, table): return f"UPDATE STATISTICS {table}"
    def m_keyset_pred(self, c1, c2, v1, v2): return None
    def m_limit(self, n): return f"OFFSET 0 ROWS FETCH NEXT {n} ROWS ONLY"
    def m_offset_limit(self, off, n): return f"OFFSET {off} ROWS FETCH NEXT {n} ROWS ONLY"
    def m_json_get(self, col, key): return f"JSON_VALUE({col}, '$.{key}')"
    def m_json_get_num(self, col, key): return f"TRY_CAST(JSON_VALUE({col}, '$.{key}') AS DECIMAL(12,2))"
    def m_date_trunc_day(self, col): return f"CAST({col} AS DATE)"
    def m_date_trunc_month(self, col): return f"DATEFROMPARTS(YEAR({col}), MONTH({col}), 1)"
    def m_year(self, col): return f"YEAR({col})"
    def m_month(self, col): return f"MONTH({col})"
    def m_date_sub_days(self, col, n): return f"DATEADD(day, -{n}, {col})"
    def m_epoch_diff_seconds(self, a, b): return f"DATEDIFF(SECOND, {b}, {a})"
    def m_string_agg_ordered(self, col, sep): return f"STRING_AGG({col}, {sep}) WITHIN GROUP (ORDER BY {col})"
    def m_concat(self, *args): return f"CONCAT({', '.join(args)})"
    def m_cast_str(self, expr, n): return f"CAST({expr} AS NVARCHAR({n}))"
    def m_cast_int(self, expr): return f"CAST({expr} AS INT)"
    def m_ts(self, lit): return f"CAST('{lit}' AS DATETIME2)"
    def m_true(self): return "1"
    def m_false(self): return "0"
    def m_mod(self, a, b): return f"({a} % {b})"
    def m_percentile_cont(self, col, q): return None
    def m_fts(self, col, words): return f"CONTAINS({col}, '{' AND '.join(words.split())}')"
    def m_bool_eq_true(self, col): return f"{col} = 1"
    def m_sleep(self, seconds): return f"WAITFOR DELAY '00:00:0{seconds}'"

    def query_overrides(self):
        return {
            "q14_upsert": ("MERGE inventory AS i USING (SELECT {cast_int(?)} AS product_id, {cast_int(?)} AS warehouse_id, {cast_int(?)} AS qty) AS s "
                           "ON (i.product_id = s.product_id AND i.warehouse_id = s.warehouse_id) "
                           "WHEN MATCHED THEN UPDATE SET qty = i.qty + s.qty, updated_at = {now} "
                           "WHEN NOT MATCHED THEN INSERT (product_id, warehouse_id, qty, updated_at) VALUES (s.product_id, s.warehouse_id, s.qty, {now});"),
            "q21_percentile": ("SELECT DISTINCT shipping_country, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_amount) OVER (PARTITION BY shipping_country) AS median "
                               "FROM orders ORDER BY shipping_country"),
            "q17b_keyset_rowvalue": None,
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE VIEW mv_lab WITH SCHEMABINDING AS SELECT country_code, COUNT_BIG(*) AS n FROM dbo.customers GROUP BY country_code",
                                        "CREATE UNIQUE CLUSTERED INDEX ux_mv_lab ON mv_lab (country_code)", "SELECT COUNT(*) FROM mv_lab WITH (NOEXPAND)"],
                  ["DROP VIEW mv_lab"], fetch_last=True, note="indexed view"),
            Probe("partitioning", ["CREATE PARTITION FUNCTION pf_lab (date) AS RANGE RIGHT FOR VALUES ('2025-01-01', '2026-01-01')",
                                   "CREATE PARTITION SCHEME ps_lab AS PARTITION pf_lab ALL TO ([PRIMARY])",
                                   "CREATE TABLE part_lab (id INT, d DATE) ON ps_lab(d)", "INSERT INTO part_lab VALUES (1, '2025-06-01')"],
                  ["DROP TABLE part_lab", "DROP PARTITION SCHEME ps_lab", "DROP PARTITION FUNCTION pf_lab"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a INT, b AS (a * 2) PERSISTED)", "INSERT INTO gen_lab (a) VALUES (21)", "SELECT b FROM gen_lab"], ["DROP TABLE gen_lab"], fetch_last=True),
            Probe("identity", ["CREATE TABLE id_lab (id INT IDENTITY(1,1) PRIMARY KEY, v INT)", "INSERT INTO id_lab (v) VALUES (1)", "SELECT id FROM id_lab"], ["DROP TABLE id_lab"], fetch_last=True),
            Probe("returning", ["CREATE TABLE ret_lab (id INT IDENTITY(1,1), v INT)", "INSERT INTO ret_lab (v) OUTPUT INSERTED.id, INSERTED.v VALUES (7)"], ["DROP TABLE ret_lab"], fetch_last=True, note="OUTPUT clause"),
            Probe("sequence", ["CREATE SEQUENCE seq_lab START WITH 1", "SELECT NEXT VALUE FOR seq_lab"], ["DROP SEQUENCE seq_lab"], fetch_last=True),
            Probe("arrays", ["SELECT ARRAY[1,2,3]"], fetch_last=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3))", "INSERT INTO vec_lab VALUES ('[1,2,3]')", "SELECT VECTOR_DISTANCE('cosine', e, CAST('[3,1,2]' AS VECTOR(3))) FROM vec_lab"], ["DROP TABLE vec_lab"], fetch_last=True, note="SQL Server 2025"),
            Probe("statement_timeout", ["SET LOCK_TIMEOUT 200", "WAITFOR DELAY '00:00:02'"], [], expect_error=True, note="no server-side statement timeout; client timeout only"),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders ORDER BY customer_id, ordered_at DESC"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v INT CHECK (v > 0))", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab"], expect_error=True),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["DELETE FROM order_items WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["CREATE TABLE sp_lab (v INT)", "INSERT INTO sp_lab VALUES (1)", "SAVE TRANSACTION s1", "INSERT INTO sp_lab VALUES (2)", "ROLLBACK TRANSACTION s1", "SELECT COUNT(*) FROM sp_lab"], ["DROP TABLE sp_lab"], fetch_last=True),
            Probe("temporal_query", ["CREATE TABLE sv_lab (id INT PRIMARY KEY, v INT, sys_start DATETIME2 GENERATED ALWAYS AS ROW START, sys_end DATETIME2 GENERATED ALWAYS AS ROW END, PERIOD FOR SYSTEM_TIME (sys_start, sys_end)) WITH (SYSTEM_VERSIONING = ON)",
                                     "INSERT INTO sv_lab (id, v) VALUES (1, 1)", "UPDATE sv_lab SET v = 2", "SELECT COUNT(*) FROM sv_lab FOR SYSTEM_TIME ALL"],
                  ["ALTER TABLE sv_lab SET (SYSTEM_VERSIONING = OFF)", "DROP TABLE sv_lab"], fetch_last=True, note="system-versioned temporal table"),
            Probe("json_aggregate", ["SELECT JSON_ARRAYAGG(JSON_OBJECT('id': id, 'name': name)) FROM categories WHERE id <= 3"], fetch_last=True, note="SQL Server 2025"),
            Probe("isolation_serializable", ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", "SELECT 1"], fetch_last=True),
            Probe("isolation_repeatable_read", ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", "SELECT 1"], fetch_last=True),
            Probe("isolation_read_uncommitted", ["SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED", "SELECT 1"], fetch_last=True),
            Probe("isolation_snapshot", ["ALTER DATABASE CURRENT SET ALLOW_SNAPSHOT_ISOLATION ON", "SET TRANSACTION ISOLATION LEVEL SNAPSHOT", "SELECT 1"], fetch_last=True),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE REGEXP_LIKE(name, '^Ultra')"], fetch_last=True, note="SQL Server 2025"),
            Probe("columnstore_index", ["CREATE NONCLUSTERED COLUMNSTORE INDEX ncci_lab ON events (customer_id, event_type, occurred_at, value_num)"], ["DROP INDEX ncci_lab ON events"]),
        ]


# =====================================================================================
class Oracle(Dialect):
    name = "oracle"
    family = "oracle"
    multirow_insert = False
    paramstyle = "numeric"
    with_recursive = "WITH"
    cross_lateral = "CROSS JOIN LATERAL"
    type_map = {"int": "NUMBER(10)", "bigint": "NUMBER(19)", "varchar": "VARCHAR2({n})", "text": "VARCHAR2(4000)",
                "decimal": "NUMBER({p},{s})", "bool": "BOOLEAN", "timestamp": "TIMESTAMP(6)", "json": "JSON"}
    explain_plain = None       # engine adapter uses DBMS_XPLAN
    explain_analyze = None
    fts_kind = "native"

    def analyze_table(self, table): return f"BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, '{table.upper()}'); END;"
    def drop_index(self, name, table): return f"DROP INDEX {name}"
    def m_keyset_pred(self, c1, c2, v1, v2): return None
    def m_limit(self, n): return f"FETCH FIRST {n} ROWS ONLY"
    def m_offset_limit(self, off, n): return f"OFFSET {off} ROWS FETCH NEXT {n} ROWS ONLY"
    def m_json_get(self, col, key): return f"JSON_VALUE({col}, '$.{key}')"
    def m_json_get_num(self, col, key): return f"JSON_VALUE({col}, '$.{key}' RETURNING NUMBER)"
    def m_date_trunc_day(self, col): return f"TRUNC({col})"
    def m_date_trunc_month(self, col): return f"TRUNC({col}, 'MM')"
    def m_date_sub_days(self, col, n): return f"{col} - INTERVAL '{n}' DAY"
    def m_epoch_diff_seconds(self, a, b): return f"((CAST({a} AS DATE) - CAST({b} AS DATE)) * 86400)"
    def m_cast_str(self, expr, n): return f"CAST({expr} AS VARCHAR2({n}))"
    def m_cast_int(self, expr): return f"CAST({expr} AS NUMBER(10))"
    def m_fts(self, col, words): return f"CONTAINS({col}, '{' AND '.join(words.split())}') > 0"
    def m_dual(self): return "FROM DUAL"
    def m_now(self): return "SYSTIMESTAMP"
    def m_sleep(self, seconds): return f"BEGIN DBMS_SESSION.SLEEP({seconds}); END;"

    def query_overrides(self):
        return {
            "q14_upsert": ("MERGE INTO inventory i USING (SELECT ? AS product_id, ? AS warehouse_id, ? AS qty FROM DUAL) s "
                           "ON (i.product_id = s.product_id AND i.warehouse_id = s.warehouse_id) "
                           "WHEN MATCHED THEN UPDATE SET qty = i.qty + s.qty, updated_at = SYSTIMESTAMP "
                           "WHEN NOT MATCHED THEN INSERT (product_id, warehouse_id, qty, updated_at) VALUES (s.product_id, s.warehouse_id, s.qty, SYSTIMESTAMP)"),
            "q17b_keyset_rowvalue": None,
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab BUILD IMMEDIATE AS SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code",
                                        "BEGIN DBMS_MVIEW.REFRESH('MV_LAB', 'C'); END;", "SELECT COUNT(*) FROM mv_lab"], ["DROP MATERIALIZED VIEW mv_lab"], fetch_last=True),
            Probe("partitioning", ["CREATE TABLE part_lab (id NUMBER, d DATE) PARTITION BY RANGE (d) (PARTITION p2025 VALUES LESS THAN (DATE '2026-01-01'), PARTITION pmax VALUES LESS THAN (MAXVALUE))",
                                   "INSERT INTO part_lab VALUES (1, DATE '2025-06-01')"], ["DROP TABLE part_lab PURGE"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a NUMBER, b AS (a * 2))", "INSERT INTO gen_lab (a) VALUES (21)", "SELECT b FROM gen_lab"], ["DROP TABLE gen_lab PURGE"], fetch_last=True, note="virtual column"),
            Probe("identity", ["CREATE TABLE id_lab (id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, v NUMBER)", "INSERT INTO id_lab (v) VALUES (1)", "SELECT id FROM id_lab"], ["DROP TABLE id_lab PURGE"], fetch_last=True),
            Probe("returning", ["CREATE TABLE ret_lab (id NUMBER GENERATED BY DEFAULT AS IDENTITY, v NUMBER)", "INSERT INTO ret_lab (v) VALUES (7) RETURNING id INTO ?"], ["DROP TABLE ret_lab PURGE"], fetch_last=True, note="RETURNING ... INTO an OUT bind variable"),
            Probe("sequence", ["CREATE SEQUENCE seq_lab", "SELECT seq_lab.NEXTVAL FROM DUAL"], ["DROP SEQUENCE seq_lab"], fetch_last=True),
            Probe("arrays", ["SELECT ARRAY[1,2,3] FROM DUAL"], fetch_last=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3, FLOAT32))", "INSERT INTO vec_lab VALUES ('[1,2,3]')", "SELECT VECTOR_DISTANCE(e, TO_VECTOR('[3,1,2]'), COSINE) FROM vec_lab"], ["DROP TABLE vec_lab PURGE"], fetch_last=True, note="Oracle 23ai AI Vector Search"),
            Probe("statement_timeout", ["BEGIN DBMS_SESSION.SLEEP(2); END;"], [], expect_error=True, note="no session-level statement timeout (needs resource manager)"),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders ORDER BY customer_id"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v NUMBER CHECK (v > 0))", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab PURGE"], expect_error=True),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["DELETE FROM order_items WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["CREATE TABLE sp_lab (v NUMBER)", "INSERT INTO sp_lab VALUES (1)", "SAVEPOINT s1", "INSERT INTO sp_lab VALUES (2)", "ROLLBACK TO SAVEPOINT s1", "SELECT COUNT(*) FROM sp_lab"], ["DROP TABLE sp_lab PURGE"], fetch_last=True),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF TIMESTAMP (SYSTIMESTAMP - INTERVAL '5' SECOND)"], fetch_last=True, note="Flashback Query"),
            Probe("json_aggregate", ["SELECT JSON_ARRAYAGG(JSON_OBJECT('id' VALUE id, 'name' VALUE name)) FROM categories WHERE id <= 3"], fetch_last=True),
            Probe("isolation_serializable", ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", "SELECT 1 FROM DUAL"], fetch_last=True),
            Probe("isolation_repeatable_read", ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", "SELECT 1 FROM DUAL"], fetch_last=True),
            Probe("isolation_read_uncommitted", ["SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED", "SELECT 1 FROM DUAL"], fetch_last=True),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE REGEXP_LIKE(name, '^Ultra')"], fetch_last=True),
            Probe("boolean_type", ["CREATE TABLE bool_lab (b BOOLEAN)", "INSERT INTO bool_lab VALUES (TRUE)", "SELECT COUNT(*) FROM bool_lab WHERE b"], ["DROP TABLE bool_lab PURGE"], fetch_last=True, note="23ai"),
        ]


# =====================================================================================
class Db2(Dialect):
    name = "db2"
    family = "db2"
    paramstyle = "qmark"
    with_recursive = "WITH"
    cross_lateral = ", LATERAL"          # Db2 syntax: FROM t, LATERAL (subquery) AS x
    type_map = {"int": "INTEGER", "bigint": "BIGINT", "varchar": "VARCHAR({n})", "text": "VARCHAR(4000)",
                "decimal": "DECIMAL({p},{s})", "bool": "BOOLEAN", "timestamp": "TIMESTAMP(6)", "json": "VARCHAR(4000)"}
    explain_plain = None
    explain_analyze = None

    def analyze_table(self, table): return f"CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE {table.upper()} WITH DISTRIBUTION AND INDEXES ALL')"
    def m_keyset_pred(self, c1, c2, v1, v2): return None
    def m_limit(self, n): return f"FETCH FIRST {n} ROWS ONLY"
    def m_offset_limit(self, off, n): return f"OFFSET {off} ROWS FETCH NEXT {n} ROWS ONLY"
    def m_json_get(self, col, key): return f"JSON_VALUE({col}, '$.{key}')"
    def m_json_get_num(self, col, key): return f"JSON_VALUE({col}, '$.{key}' RETURNING DECIMAL(12,2))"
    def m_date_trunc_day(self, col): return f"DATE({col})"
    def m_date_trunc_month(self, col): return f"DATE_TRUNC('MONTH', {col})"
    def m_date_sub_days(self, col, n): return f"{col} - {n} DAYS"
    def m_epoch_diff_seconds(self, a, b): return f"((DAYS({a}) - DAYS({b})) * 86400 + (MIDNIGHT_SECONDS({a}) - MIDNIGHT_SECONDS({b})))"
    def m_dual(self): return "FROM SYSIBM.SYSDUMMY1"
    def m_sleep(self, seconds): return None

    def query_overrides(self):
        return {
            "q14_upsert": ("MERGE INTO inventory i USING (SELECT CAST(? AS INTEGER) AS product_id, CAST(? AS INTEGER) AS warehouse_id, CAST(? AS INTEGER) AS qty FROM SYSIBM.SYSDUMMY1) s "
                           "ON (i.product_id = s.product_id AND i.warehouse_id = s.warehouse_id) "
                           "WHEN MATCHED THEN UPDATE SET qty = i.qty + s.qty, updated_at = CURRENT TIMESTAMP "
                           "WHEN NOT MATCHED THEN INSERT (product_id, warehouse_id, qty, updated_at) VALUES (s.product_id, s.warehouse_id, s.qty, CURRENT TIMESTAMP)"),
            "q17b_keyset_rowvalue": None,
            # Db2 rejects an explicit JOIN in the recursive member (SQL0345N); the comma-join form is accepted
            "q06_recursive_cte": ("WITH tree (id, parent_id, name, depth, path) AS ("
                                  "SELECT id, parent_id, name, 0, CAST(name AS VARCHAR(400)) FROM categories WHERE parent_id IS NULL "
                                  "UNION ALL SELECT c.id, c.parent_id, c.name, t.depth + 1, CAST(t.path || ' > ' || c.name AS VARCHAR(400)) "
                                  "FROM tree t, categories c WHERE c.parent_id = t.id) SELECT id, depth, path FROM tree ORDER BY path"),
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE TABLE mv_lab AS (SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code) DATA INITIALLY DEFERRED REFRESH DEFERRED",
                                        "REFRESH TABLE mv_lab", "SELECT COUNT(*) FROM mv_lab"], ["DROP TABLE mv_lab"], fetch_last=True, note="MQT"),
            Probe("partitioning", ["CREATE TABLE part_lab (id INT, d DATE) PARTITION BY RANGE (d) (STARTING '2025-01-01' ENDING '2026-12-31' EVERY 1 YEAR)", "INSERT INTO part_lab VALUES (1, '2025-06-01')"], ["DROP TABLE part_lab"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a INT, b INT GENERATED ALWAYS AS (a * 2))", "INSERT INTO gen_lab (a) VALUES (21)", "SELECT b FROM gen_lab"], ["DROP TABLE gen_lab"], fetch_last=True),
            Probe("identity", ["CREATE TABLE id_lab (id INT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY NOT NULL, v INT)", "INSERT INTO id_lab (v) VALUES (1)", "SELECT id FROM id_lab"], ["DROP TABLE id_lab"], fetch_last=True),
            Probe("returning", ["CREATE TABLE ret_lab (id INT GENERATED BY DEFAULT AS IDENTITY NOT NULL, v INT)", "SELECT id, v FROM FINAL TABLE (INSERT INTO ret_lab (v) VALUES (7))"], ["DROP TABLE ret_lab"], fetch_last=True, note="data-change-table-reference"),
            Probe("sequence", ["CREATE SEQUENCE seq_lab", "SELECT NEXT VALUE FOR seq_lab FROM SYSIBM.SYSDUMMY1"], ["DROP SEQUENCE seq_lab"], fetch_last=True),
            Probe("arrays", ["SELECT ARRAY[1,2,3] FROM SYSIBM.SYSDUMMY1"], fetch_last=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3, FLOAT32))", "INSERT INTO vec_lab VALUES (VECTOR('[1,2,3]', 3, FLOAT32))", "SELECT VECTOR_DISTANCE(e, VECTOR('[3,1,2]', 3, FLOAT32), COSINE) FROM vec_lab"], ["DROP TABLE vec_lab"], fetch_last=True, note="Db2 12.1.2+"),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders ORDER BY customer_id"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v INT CHECK (v > 0))", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab"], expect_error=True),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["DELETE FROM order_items WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["CREATE TABLE sp_lab (v INT)", "INSERT INTO sp_lab VALUES (1)", "SAVEPOINT s1 ON ROLLBACK RETAIN CURSORS", "INSERT INTO sp_lab VALUES (2)", "ROLLBACK TO SAVEPOINT s1", "SELECT COUNT(*) FROM sp_lab"], ["DROP TABLE sp_lab"], fetch_last=True),
            Probe("temporal_query", ["CREATE TABLE sv_lab (id INT NOT NULL PRIMARY KEY, v INT, sys_start TIMESTAMP(12) NOT NULL GENERATED ALWAYS AS ROW BEGIN, sys_end TIMESTAMP(12) NOT NULL GENERATED ALWAYS AS ROW END, ts_id TIMESTAMP(12) NOT NULL GENERATED ALWAYS AS TRANSACTION START ID, PERIOD SYSTEM_TIME (sys_start, sys_end))",
                                     "CREATE TABLE sv_lab_hist LIKE sv_lab", "ALTER TABLE sv_lab ADD VERSIONING USE HISTORY TABLE sv_lab_hist", "INSERT INTO sv_lab (id, v) VALUES (1, 1)", "UPDATE sv_lab SET v = 2",
                                     "SELECT COUNT(*) FROM sv_lab FOR SYSTEM_TIME FROM '0001-01-01' TO '9999-12-30'"],
                  ["ALTER TABLE sv_lab DROP VERSIONING", "DROP TABLE sv_lab_hist", "DROP TABLE sv_lab"], fetch_last=True),
            Probe("json_aggregate", ["SELECT JSON_ARRAYAGG(JSON_OBJECT('id' VALUE id, 'name' VALUE name)) FROM categories WHERE id <= 3"], fetch_last=True),
            Probe("isolation_serializable", ["SET CURRENT ISOLATION = RR", "SELECT 1 FROM SYSIBM.SYSDUMMY1"], fetch_last=True, note="RR"),
            Probe("isolation_repeatable_read", ["SET CURRENT ISOLATION = RS", "SELECT 1 FROM SYSIBM.SYSDUMMY1"], fetch_last=True, note="RS"),
            Probe("isolation_read_uncommitted", ["SET CURRENT ISOLATION = UR", "SELECT 1 FROM SYSIBM.SYSDUMMY1"], fetch_last=True, note="UR"),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE REGEXP_LIKE(name, '^Ultra')"], fetch_last=True),
        ]


# =====================================================================================
class SQLite(Dialect):
    name = "sqlite"
    family = "sqlite"
    paramstyle = "qmark"
    cross_lateral = None
    type_map = {"int": "INTEGER", "bigint": "INTEGER", "varchar": "TEXT", "text": "TEXT",
                "decimal": "NUMERIC", "bool": "INTEGER", "timestamp": "TEXT", "json": "TEXT"}
    explain_plain = "EXPLAIN QUERY PLAN {sql}"
    explain_analyze = None
    fts_kind = "native"

    def index_where(self, where): return f" WHERE {where}"
    def analyze_table(self, table): return f"ANALYZE {table}"
    def truncate(self, table): return f"DELETE FROM {table}"
    def m_limit(self, n): return f"LIMIT {n}"
    def m_offset_limit(self, off, n): return f"LIMIT {n} OFFSET {off}"
    def m_json_get(self, col, key): return f"json_extract({col}, '$.{key}')"
    def m_json_get_num(self, col, key): return f"CAST(json_extract({col}, '$.{key}') AS REAL)"
    def m_date_trunc_day(self, col): return f"date({col})"
    def m_date_trunc_month(self, col): return f"strftime('%Y-%m-01', {col})"
    def m_year(self, col): return f"CAST(strftime('%Y', {col}) AS INTEGER)"
    def m_month(self, col): return f"CAST(strftime('%m', {col}) AS INTEGER)"
    def m_date_sub_days(self, col, n): return f"datetime({col}, '-{n} days')"
    def m_epoch_diff_seconds(self, a, b): return f"((julianday({a}) - julianday({b})) * 86400)"
    def m_string_agg_ordered(self, col, sep): return f"GROUP_CONCAT({col}, {sep} ORDER BY {col})"
    def m_cast_str(self, expr, n): return f"CAST({expr} AS TEXT)"
    def m_ts(self, lit): return f"'{lit}'"
    def m_true(self): return "1"
    def m_false(self): return "0"
    def m_mod(self, a, b): return f"({a} % {b})"
    def m_percentile_cont(self, col, q): return None
    def m_fts(self, col, words): return f"rowid IN (SELECT rowid FROM products_fts WHERE products_fts MATCH '{words}')"
    def m_bool_eq_true(self, col): return f"{col} = 1"

    def query_overrides(self):
        return {
            "q14_upsert": ("INSERT INTO inventory (product_id, warehouse_id, qty, updated_at) VALUES (?, ?, ?, {now}) "
                           "ON CONFLICT (product_id, warehouse_id) DO UPDATE SET qty = inventory.qty + excluded.qty, updated_at = {now}"),
            "q15_merge": None, "q21_percentile": None, "q10_lateral_topn": None,
            "q17b_keyset_rowvalue": ("SELECT id, customer_id, ordered_at FROM orders WHERE (ordered_at, id) > ('2025-01-01 00:00:00', 0) ORDER BY ordered_at, id LIMIT 50"),
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab AS SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code"], ["DROP MATERIALIZED VIEW mv_lab"]),
            Probe("partitioning", ["CREATE TABLE part_lab (id INT, d DATE) PARTITION BY RANGE (d)"], ["DROP TABLE part_lab"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a INT, b INT GENERATED ALWAYS AS (a * 2) STORED)", "INSERT INTO gen_lab (a) VALUES (21)", "SELECT b FROM gen_lab"], ["DROP TABLE gen_lab"], fetch_last=True),
            Probe("identity", ["CREATE TABLE id_lab (id INTEGER PRIMARY KEY AUTOINCREMENT, v INT)", "INSERT INTO id_lab (v) VALUES (1)", "SELECT id FROM id_lab"], ["DROP TABLE id_lab"], fetch_last=True),
            Probe("returning", ["CREATE TABLE ret_lab (id INTEGER PRIMARY KEY, v INT)", "INSERT INTO ret_lab (v) VALUES (7) RETURNING id, v"], ["DROP TABLE ret_lab"], fetch_last=True),
            Probe("sequence", ["CREATE SEQUENCE seq_lab"], ["DROP SEQUENCE seq_lab"]),
            Probe("arrays", ["SELECT ARRAY[1,2,3]"], fetch_last=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3))"], ["DROP TABLE vec_lab"], note="needs sqlite-vec extension"),
            Probe("statement_timeout", ["SET statement_timeout = 200"], [], note="no server-side timeout (progress handler only)"),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders ORDER BY customer_id"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v INT CHECK (v > 0))", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab"], expect_error=True),
            Probe("fk_enforced", ["PRAGMA foreign_keys = ON", "INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["DELETE FROM order_items WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["CREATE TABLE sp_lab (v INT)", "INSERT INTO sp_lab VALUES (1)", "SAVEPOINT s1", "INSERT INTO sp_lab VALUES (2)", "ROLLBACK TO SAVEPOINT s1", "RELEASE SAVEPOINT s1", "SELECT COUNT(*) FROM sp_lab"], ["DROP TABLE sp_lab"], fetch_last=True),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF SYSTEM TIME '-5s'"], fetch_last=True),
            Probe("json_aggregate", ["SELECT json_group_array(json_object('id', id, 'name', name)) FROM (SELECT id, name FROM categories WHERE id <= 3)"], fetch_last=True),
            Probe("isolation_serializable", ["PRAGMA read_uncommitted = 0", "SELECT 1"], fetch_last=True, note="SQLite is always serializable"),
            Probe("isolation_read_uncommitted", ["PRAGMA read_uncommitted = 1", "SELECT 1"], ["PRAGMA read_uncommitted = 0"], fetch_last=True, note="shared-cache mode only"),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE name REGEXP '^Ultra'"], fetch_last=True, note="needs REGEXP function registered"),
            Probe("strict_table", ["CREATE TABLE strict_lab (a INT, b TEXT) STRICT", "INSERT INTO strict_lab VALUES ('x', 'y')"], ["DROP TABLE strict_lab"], expect_error=True, note="STRICT tables reject type mismatch"),
        ]


class DuckDB(Dialect):
    name = "duckdb"
    family = "duckdb"
    paramstyle = "qmark"
    cross_lateral = "CROSS JOIN LATERAL"
    type_map = {"int": "INTEGER", "bigint": "BIGINT", "varchar": "VARCHAR", "text": "VARCHAR",
                "decimal": "DECIMAL({p},{s})", "bool": "BOOLEAN", "timestamp": "TIMESTAMP", "json": "JSON"}
    explain_plain = "EXPLAIN {sql}"
    explain_analyze = "EXPLAIN ANALYZE {sql}"
    fts_kind = "native"

    def analyze_table(self, table): return "ANALYZE"
    def m_limit(self, n): return f"LIMIT {n}"
    def m_offset_limit(self, off, n): return f"LIMIT {n} OFFSET {off}"
    def m_json_get(self, col, key): return f"({col}->>'$.{key}')"
    def m_json_get_num(self, col, key): return f"CAST(({col}->>'$.{key}') AS DOUBLE)"
    def m_date_trunc_day(self, col): return f"DATE_TRUNC('day', {col})"
    def m_date_trunc_month(self, col): return f"DATE_TRUNC('month', {col})"
    def m_date_sub_days(self, col, n): return f"{col} - INTERVAL {n} DAY"
    def m_epoch_diff_seconds(self, a, b): return f"date_diff('second', {b}, {a})"
    def m_string_agg_ordered(self, col, sep): return f"STRING_AGG({col}, {sep} ORDER BY {col})"
    def m_percentile_cont(self, col, q): return f"quantile_cont({col}, {q})"
    def m_fts(self, col, words): return f"fts_main_products.match_bm25(id, '{words}') IS NOT NULL"
    def m_now(self): return "now()"

    def query_overrides(self):
        return {
            "q14_upsert": ("INSERT INTO inventory (product_id, warehouse_id, qty, updated_at) VALUES (?, ?, ?, {now}) "
                           "ON CONFLICT (product_id, warehouse_id) DO UPDATE SET qty = inventory.qty + excluded.qty, updated_at = {now}"),
            "q17b_keyset_rowvalue": ("SELECT id, customer_id, ordered_at FROM orders WHERE (ordered_at, id) > ({ts(2025-01-01 00:00:00)}, 0) ORDER BY ordered_at, id LIMIT 50"),
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab AS SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code"], ["DROP MATERIALIZED VIEW mv_lab"]),
            Probe("partitioning", ["CREATE TABLE part_lab (id INT, d DATE) PARTITION BY RANGE (d)"], ["DROP TABLE part_lab"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a INT, b INT GENERATED ALWAYS AS (a * 2) VIRTUAL)", "INSERT INTO gen_lab (a) VALUES (21)", "SELECT b FROM gen_lab"], ["DROP TABLE gen_lab"], fetch_last=True),
            Probe("identity", ["CREATE SEQUENCE id_lab_seq", "CREATE TABLE id_lab (id INT DEFAULT nextval('id_lab_seq') PRIMARY KEY, v INT)", "INSERT INTO id_lab (v) VALUES (1)", "SELECT id FROM id_lab"], ["DROP TABLE id_lab", "DROP SEQUENCE id_lab_seq"], fetch_last=True, note="sequence default"),
            Probe("returning", ["CREATE TABLE ret_lab (id INT, v INT)", "INSERT INTO ret_lab VALUES (1, 7) RETURNING id, v"], ["DROP TABLE ret_lab"], fetch_last=True),
            Probe("sequence", ["CREATE SEQUENCE seq_lab", "SELECT nextval('seq_lab')"], ["DROP SEQUENCE seq_lab"], fetch_last=True),
            Probe("arrays", ["SELECT [1,2,3] || [4]"], fetch_last=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e FLOAT[3])", "INSERT INTO vec_lab VALUES ([1,2,3])", "SELECT array_cosine_similarity(e, [3.0,1.0,2.0]::FLOAT[3]) FROM vec_lab"], ["DROP TABLE vec_lab"], fetch_last=True, note="fixed-size arrays"),
            Probe("statement_timeout", ["SET statement_timeout = 200"], [], note="no server-side timeout (interrupt only)"),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders WHERE customer_id <= 20 ORDER BY customer_id, ordered_at DESC"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v INT CHECK (v > 0))", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab"], expect_error=True),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["DELETE FROM order_items WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["SAVEPOINT s1"], [], note="DuckDB has no savepoints"),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF SYSTEM TIME '-5s'"], fetch_last=True),
            Probe("json_aggregate", ["SELECT json_group_array(json_object('id', id, 'name', name)) FROM (SELECT id, name FROM categories WHERE id <= 3)"], fetch_last=True),
            Probe("isolation_serializable", ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"], [], note="DuckDB: snapshot isolation only"),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE regexp_matches(name, '^Ultra')"], fetch_last=True),
            Probe("pivot_statement", ["PIVOT orders ON status USING COUNT(*) GROUP BY shipping_country"], fetch_last=True),
        ]


# =====================================================================================
class ClickHouse(Dialect):
    name = "clickhouse"
    family = "clickhouse"
    paramstyle = "qmark"          # adapter inlines literals
    supports_fk = False
    supports_pk = False           # ORDER BY key in engine clause instead
    supports_transactions = False
    cross_lateral = None
    type_map = {"int": "Int32", "bigint": "Int64", "varchar": "String", "text": "String",
                "decimal": "Decimal({p},{s})", "bool": "Bool", "timestamp": "DateTime64(6)", "json": "String"}
    explain_plain = "EXPLAIN indexes = 1 {sql}"
    explain_analyze = None        # adapter reads system.query_log
    fts_kind = "native"

    def col_sql(self, col, table):
        t = self.sql_type(col)
        if col.nullable:
            t = f"Nullable({t})"
        return f"{col.name} {t}"

    def table_suffix(self, table):
        key = ", ".join(table.pk or table.order_hint)
        return f" ENGINE = MergeTree ORDER BY ({key})"

    def create_index(self, name, table, cols, *, unique=False, where=None, include=None):
        if unique or where:
            return None
        # data-skipping index (bloom filter) is the ClickHouse analogue of a secondary index; MATERIALIZE applies it to existing parts
        return [f"ALTER TABLE {table} ADD INDEX {name} ({', '.join(cols)}) TYPE bloom_filter GRANULARITY 4",
                f"ALTER TABLE {table} MATERIALIZE INDEX {name}"]

    def drop_index(self, name, table): return f"ALTER TABLE {table} DROP INDEX {name}"
    def analyze_table(self, table): return None
    def m_limit(self, n): return f"LIMIT {n}"
    def m_offset_limit(self, off, n): return f"LIMIT {n} OFFSET {off}"
    def m_json_get(self, col, key): return f"JSONExtractString({col}, '{key}')"
    def m_json_get_num(self, col, key): return f"JSONExtractFloat({col}, '{key}')"
    def m_date_trunc_day(self, col): return f"toDate({col})"
    def m_date_trunc_month(self, col): return f"toStartOfMonth({col})"
    def m_year(self, col): return f"toYear({col})"
    def m_month(self, col): return f"toMonth({col})"
    def m_date_sub_days(self, col, n): return f"{col} - INTERVAL {n} DAY"
    def m_epoch_diff_seconds(self, a, b): return f"dateDiff('second', {b}, {a})"
    def m_string_agg_ordered(self, col, sep): return f"arrayStringConcat(arraySort(groupArray({col})), {sep})"
    def m_concat(self, *args): return f"concat({', '.join(args)})"
    def m_cast_str(self, expr, n): return f"toString({expr})"
    def m_cast_int(self, expr): return f"toInt32({expr})"
    def m_ts(self, lit): return f"toDateTime64('{lit}', 6)"
    def m_now(self): return "now64(6)"
    def m_true(self): return "true"
    def m_false(self): return "false"
    def m_mod(self, a, b): return f"({a} % {b})"
    def m_percentile_cont(self, col, q): return f"quantileExact({q})({col})"
    def m_fts(self, col, words): return " AND ".join(f"hasToken({col}, '{w}')" for w in words.split())
    def m_bool_eq_true(self, col): return f"{col} = true"
    def m_sleep(self, seconds): return f"SELECT sleep({seconds})"

    def query_overrides(self):
        return {
            "q14_upsert": None, "q15_merge": None, "q10_lateral_topn": None, "q17b_keyset_rowvalue": None,
            "q06_recursive_cte": ("WITH RECURSIVE tree AS (SELECT id, parent_id, name, 0 AS depth, name AS path FROM categories WHERE parent_id IS NULL "
                                  "UNION ALL SELECT c.id, c.parent_id, c.name, t.depth + 1, concat(t.path, ' > ', c.name) FROM categories c JOIN tree t ON c.parent_id = t.id) "
                                  "SELECT id, depth, path FROM tree ORDER BY path"),
            "q27_update_single": "ALTER TABLE customers UPDATE tier = ? WHERE id = ?",
            "q29_txn_order": None,
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab ENGINE = SummingMergeTree ORDER BY country_code POPULATE AS SELECT country_code, count() AS n FROM customers GROUP BY country_code",
                                        "SELECT count() FROM mv_lab"], ["DROP VIEW mv_lab"], fetch_last=True, note="incremental MV (insert trigger)"),
            Probe("partitioning", ["CREATE TABLE part_lab (id Int32, d Date) ENGINE = MergeTree PARTITION BY toYYYYMM(d) ORDER BY id", "INSERT INTO part_lab VALUES (1, '2025-06-01')"], ["DROP TABLE part_lab"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a Int32, b Int32 MATERIALIZED a * 2) ENGINE = MergeTree ORDER BY a", "INSERT INTO gen_lab (a) VALUES (21)", "SELECT b FROM gen_lab"], ["DROP TABLE gen_lab"], fetch_last=True),
            Probe("identity", ["CREATE TABLE id_lab (id Int32 DEFAULT generateSerialID('id_lab'), v Int32) ENGINE = MergeTree ORDER BY id"], ["DROP TABLE id_lab"], note="no identity columns (generateSerialID needs Keeper)"),
            Probe("returning", ["CREATE TABLE ret_lab (id Int32, v Int32) ENGINE = MergeTree ORDER BY id", "INSERT INTO ret_lab VALUES (1, 7) RETURNING id"], ["DROP TABLE ret_lab"]),
            Probe("sequence", ["CREATE SEQUENCE seq_lab"], []),
            Probe("arrays", ["SELECT arrayConcat([1,2,3], [4])"], fetch_last=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e Array(Float32)) ENGINE = MergeTree ORDER BY tuple()", "INSERT INTO vec_lab VALUES ([1,2,3])", "SELECT cosineDistance(e, [3,1,2]) FROM vec_lab"], ["DROP TABLE vec_lab"], fetch_last=True, note="Array(Float32) + distance functions"),
            Probe("statement_timeout", ["SELECT sleep(2) SETTINGS max_execution_time = 0.2"], [], expect_error=True, note="error 159 TIMEOUT_EXCEEDED (query-level SETTINGS; HTTP sessions are stateless)"),
            Probe("filter_clause", ["SELECT countIf(status = 'delivered') FROM orders"], fetch_last=True, note="countIf combinator (no FILTER clause)"),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders WHERE customer_id <= 20 ORDER BY customer_id, ordered_at DESC"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY row_number() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v Int32, CONSTRAINT c1 CHECK v > 0) ENGINE = MergeTree ORDER BY v", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab"], expect_error=True),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["ALTER TABLE order_items DELETE WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["SAVEPOINT s1"], []),
            Probe("temporal_query", ["SELECT count() FROM orders AS OF SYSTEM TIME '-5s'"], fetch_last=True),
            Probe("json_aggregate", ["SELECT toJSONString(groupArray(tuple(id, name))) FROM categories WHERE id <= 3"], fetch_last=True),
            Probe("isolation_serializable", ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"], [], note="no isolation levels"),
            Probe("regex_match", ["SELECT count() FROM products WHERE match(name, '^Ultra')"], fetch_last=True),
            Probe("json_type", ["CREATE TABLE json_lab (j JSON) ENGINE = MergeTree ORDER BY tuple()", "INSERT INTO json_lab VALUES ('{\"a\": 1, \"b\": {\"c\": \"x\"}}')", "SELECT j.b.c FROM json_lab"], ["DROP TABLE json_lab"], fetch_last=True, note="native JSON type"),
        ]


# =====================================================================================
class Firebird(Dialect):
    name = "firebird"
    family = "firebird"
    multirow_insert = False
    paramstyle = "qmark"
    cross_lateral = None
    # decimals are stored as DOUBLE PRECISION: SUM() over NUMERIC(12,x) yields NUMERIC(38,x) (INT128) in Firebird 4+,
    # which the Python firebird-driver cannot describe ("Data type unknown")
    type_map = {"int": "INTEGER", "bigint": "BIGINT", "varchar": "VARCHAR({n})", "text": "VARCHAR(4000)",
                "decimal": "DOUBLE PRECISION", "bool": "BOOLEAN", "timestamp": "TIMESTAMP", "json": "VARCHAR(2000)"}
    explain_plain = None        # adapter reads cursor.plan / detailed plan
    explain_analyze = None
    fts_kind = "like-fallback"

    def analyze_table(self, table): return None
    def truncate(self, table): return f"DELETE FROM {table}"
    def m_keyset_pred(self, c1, c2, v1, v2): return None
    def m_limit(self, n): return f"FETCH FIRST {n} ROWS ONLY"
    def m_offset_limit(self, off, n): return f"OFFSET {off} ROWS FETCH NEXT {n} ROWS ONLY"
    def m_json_get(self, col, key): return None
    def m_json_get_num(self, col, key): return None
    def m_date_trunc_day(self, col): return f"CAST({col} AS DATE)"
    def m_date_trunc_month(self, col): return f"CAST(EXTRACT(YEAR FROM {col}) || '-' || LPAD(EXTRACT(MONTH FROM {col}), 2, '0') || '-01' AS DATE)"
    def m_date_sub_days(self, col, n): return f"DATEADD(-{n} DAY TO {col})"
    def m_epoch_diff_seconds(self, a, b): return f"DATEDIFF(SECOND, {b}, {a})"
    def m_string_agg_ordered(self, col, sep): return f"LIST({col}, {sep})"
    def m_percentile_cont(self, col, q): return None
    def m_dual(self): return "FROM RDB$DATABASE"
    def m_sleep(self, seconds): return None

    def query_overrides(self):
        return {
            "q14_upsert": ("UPDATE OR INSERT INTO inventory (product_id, warehouse_id, qty, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) MATCHING (product_id, warehouse_id)"),
            "q15_merge": ("MERGE INTO inventory i USING (SELECT CAST(? AS INTEGER) AS product_id, CAST(? AS INTEGER) AS warehouse_id, CAST(? AS INTEGER) AS qty FROM RDB$DATABASE) s "
                          "ON (i.product_id = s.product_id AND i.warehouse_id = s.warehouse_id) "
                          "WHEN MATCHED THEN UPDATE SET qty = i.qty + s.qty, updated_at = CURRENT_TIMESTAMP "
                          "WHEN NOT MATCHED THEN INSERT (product_id, warehouse_id, qty, updated_at) VALUES (s.product_id, s.warehouse_id, s.qty, CURRENT_TIMESTAMP)"),
            "q10_lateral_topn": None, "q12_json_filter": None, "q21_percentile": None, "q17b_keyset_rowvalue": None,
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab AS SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code"], ["DROP MATERIALIZED VIEW mv_lab"]),
            Probe("partitioning", ["CREATE TABLE part_lab (id INT, d DATE) PARTITION BY RANGE (d)"], ["DROP TABLE part_lab"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a INT, b COMPUTED BY (a * 2))", "INSERT INTO gen_lab (a) VALUES (21)", "SELECT b FROM gen_lab"], ["DROP TABLE gen_lab"], fetch_last=True),
            Probe("identity", ["CREATE TABLE id_lab (id INT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, v INT)", "INSERT INTO id_lab (v) VALUES (1)", "SELECT id FROM id_lab"], ["DROP TABLE id_lab"], fetch_last=True),
            Probe("returning", ["CREATE TABLE ret_lab (id INT GENERATED BY DEFAULT AS IDENTITY, v INT)", "INSERT INTO ret_lab (v) VALUES (7) RETURNING id, v"], ["DROP TABLE ret_lab"], fetch_last=True),
            Probe("sequence", ["CREATE SEQUENCE seq_lab", "SELECT NEXT VALUE FOR seq_lab FROM RDB$DATABASE"], ["DROP SEQUENCE seq_lab"], fetch_last=True),
            Probe("arrays", ["SELECT ARRAY[1,2,3] FROM RDB$DATABASE"], fetch_last=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3))"], ["DROP TABLE vec_lab"]),
            Probe("statement_timeout", ["SET STATEMENT TIMEOUT 200 MILLISECOND", "SELECT COUNT(*) FROM events e1, events e2 WHERE e1.id = e2.id + 1"], ["SET STATEMENT TIMEOUT 0"], expect_error=True, note="Firebird 4+ statement timeout"),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders ORDER BY customer_id"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v INT CHECK (v > 0))", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab"], expect_error=True),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["DELETE FROM order_items WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["CREATE TABLE sp_lab (v INT)", "INSERT INTO sp_lab VALUES (1)", "SAVEPOINT s1", "INSERT INTO sp_lab VALUES (2)", "ROLLBACK TO SAVEPOINT s1", "SELECT COUNT(*) FROM sp_lab"], ["DROP TABLE sp_lab"], fetch_last=True),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF SYSTEM TIME '-5s'"], fetch_last=True),
            Probe("json_aggregate", ["SELECT JSON_ARRAYAGG(id) FROM categories WHERE id <= 3"], fetch_last=True),
            Probe("isolation_serializable", ["SET TRANSACTION ISOLATION LEVEL SNAPSHOT TABLE STABILITY", "SELECT 1 FROM RDB$DATABASE"], [], fetch_last=True, note="Firebird: SNAPSHOT TABLE STABILITY (set through the driver's TPB)"),
            Probe("isolation_repeatable_read", ["SET TRANSACTION ISOLATION LEVEL SNAPSHOT", "SELECT 1 FROM RDB$DATABASE"], [], fetch_last=True, note="Firebird: SNAPSHOT"),
            Probe("isolation_read_uncommitted", ["SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED"], [], note="no dirty reads in Firebird"),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE name SIMILAR TO 'Ultra%'"], fetch_last=True),
        ]


# =====================================================================================
class H2(Postgres):
    """H2 in PostgreSQL-compatibility mode (speaks the PG wire protocol)."""
    name = "h2"
    family = "h2"
    type_map = Dialect.type_map | {"text": "VARCHAR(4000)", "json": "VARCHAR(2000)", "timestamp": "TIMESTAMP(6)"}
    explain_plain = "EXPLAIN {sql}"
    explain_analyze = "EXPLAIN ANALYZE {sql}"
    cross_lateral = None
    fts_kind = "like-fallback"

    def index_include(self, cols): raise Unsupported("h2: INCLUDE not supported")
    def index_where(self, where): raise Unsupported("h2: partial indexes not supported")
    def analyze_table(self, table): return f"ANALYZE TABLE {table}"
    def m_json_get(self, col, key): return None
    def m_json_get_num(self, col, key): return None
    def m_date_trunc_day(self, col): return f"DATE_TRUNC('DAY', {col})"
    def m_date_trunc_month(self, col): return f"DATE_TRUNC('MONTH', {col})"
    def m_date_sub_days(self, col, n): return f"DATEADD('DAY', -{n}, {col})"
    def m_epoch_diff_seconds(self, a, b): return f"DATEDIFF('SECOND', {b}, {a})"
    def m_string_agg_ordered(self, col, sep): return f"LISTAGG({col}, {sep}) WITHIN GROUP (ORDER BY {col})"
    def m_fts(self, col, words): return " AND ".join(f"{col} LIKE '%{w}%'" for w in words.split())
    def m_sleep(self, seconds): return None

    def query_overrides(self):
        return {
            "q14_upsert": ("MERGE INTO inventory (product_id, warehouse_id, qty, updated_at) KEY (product_id, warehouse_id) VALUES (?, ?, ?, CURRENT_TIMESTAMP)"),
            "q12_json_filter": None, "q10_lateral_topn": None, "q17b_keyset_rowvalue": None,
        }

    def probes(self):
        keep = {"generated_column", "identity", "sequence", "arrays", "filter_clause", "distinct_on", "qualify", "check_constraint", "fk_enforced",
                "savepoint", "isolation_serializable", "isolation_repeatable_read", "isolation_read_uncommitted"}
        ps = [p for p in super().probes() if p.name in keep]
        ps += [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab AS SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code"], ["DROP MATERIALIZED VIEW mv_lab"]),
            Probe("partitioning", ["CREATE TABLE part_lab (id INT, d DATE) PARTITION BY RANGE (d)"], ["DROP TABLE part_lab"]),
            Probe("returning", ["CREATE TABLE ret_lab (id INT GENERATED BY DEFAULT AS IDENTITY, v INT)", "SELECT id, v FROM FINAL TABLE (INSERT INTO ret_lab (v) VALUES (7))"], ["DROP TABLE ret_lab"], fetch_last=True, note="FINAL TABLE"),
            Probe("statement_timeout", ["SET QUERY_TIMEOUT 200", "SELECT COUNT(*) FROM events e1, events e2 WHERE e1.id = e2.id + 1"], ["SET QUERY_TIMEOUT 0"], expect_error=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3))"], ["DROP TABLE vec_lab"]),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF SYSTEM TIME '-5s'"], fetch_last=True),
            Probe("json_aggregate", ["SELECT JSON_ARRAYAGG(JSON_OBJECT('id': id, 'name': name)) FROM categories WHERE id <= 3"], fetch_last=True),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE REGEXP_LIKE(name, '^Ultra')"], fetch_last=True),
        ]
        return ps


# =====================================================================================
class MonetDB(Dialect):
    name = "monetdb"
    family = "monetdb"
    paramstyle = "pyformat"
    cross_lateral = None
    type_map = {"int": "INT", "bigint": "BIGINT", "varchar": "VARCHAR({n})", "text": "TEXT",
                "decimal": "DECIMAL({p},{s})", "bool": "BOOLEAN", "timestamp": "TIMESTAMP", "json": "JSON"}
    explain_plain = "PLAN {sql}"
    explain_analyze = "TRACE {sql}"
    fts_kind = "like-fallback"

    def analyze_table(self, table): return f"ANALYZE sys.{table}"
    def m_keyset_pred(self, c1, c2, v1, v2): return None
    def m_keyset_pred(self, c1, c2, v1, v2): return None
    def m_limit(self, n): return f"LIMIT {n}"
    def m_offset_limit(self, off, n): return f"LIMIT {n} OFFSET {off}"
    def m_json_get(self, col, key): return f"json.text(json.filter({col}, '$.{key}'))"
    def m_json_get_num(self, col, key): return f"json.number(json.filter({col}, '$.{key}'))"
    def m_date_trunc_day(self, col): return f"CAST({col} AS DATE)"
    def m_date_trunc_month(self, col): return f"date_trunc('month', {col})"
    def m_date_sub_days(self, col, n): return f"{col} - INTERVAL '{n}' DAY"
    def m_epoch_diff_seconds(self, a, b): return f"(epoch({a}) - epoch({b}))"
    def m_string_agg_ordered(self, col, sep): return f"GROUP_CONCAT({col}, {sep})"
    def m_percentile_cont(self, col, q): return f"quantile({col}, {q})"
    def m_sleep(self, seconds): return None

    def query_overrides(self):
        return {
            "q14_upsert": ("MERGE INTO inventory i USING (SELECT CAST(? AS INT) AS product_id, CAST(? AS INT) AS warehouse_id, CAST(? AS INT) AS qty) s "
                           "ON (i.product_id = s.product_id AND i.warehouse_id = s.warehouse_id) "
                           "WHEN MATCHED THEN UPDATE SET qty = i.qty + s.qty, updated_at = CURRENT_TIMESTAMP "
                           "WHEN NOT MATCHED THEN INSERT (product_id, warehouse_id, qty, updated_at) VALUES (s.product_id, s.warehouse_id, s.qty, CURRENT_TIMESTAMP)"),
            "q10_lateral_topn": None, "q17b_keyset_rowvalue": None,
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab AS SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code"], ["DROP MATERIALIZED VIEW mv_lab"]),
            Probe("partitioning", ["CREATE MERGE TABLE part_lab (id INT, d DATE) PARTITION BY RANGE ON (d)", "CREATE TABLE part_lab_2025 (id INT, d DATE)",
                                   "ALTER TABLE part_lab ADD TABLE part_lab_2025 AS PARTITION FROM '2025-01-01' TO '2025-12-31'", "INSERT INTO part_lab VALUES (1, '2025-06-01')"],
                  ["ALTER TABLE part_lab DROP TABLE part_lab_2025", "DROP TABLE part_lab_2025", "DROP TABLE part_lab"], note="merge table partitions"),
            Probe("generated_column", ["CREATE TABLE gen_lab (a INT, b INT GENERATED ALWAYS AS (a * 2))"], ["DROP TABLE gen_lab"]),
            Probe("identity", ["CREATE TABLE id_lab (id INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, v INT)", "INSERT INTO id_lab (v) VALUES (1)", "SELECT id FROM id_lab"], ["DROP TABLE id_lab"], fetch_last=True),
            Probe("returning", ["CREATE TABLE ret_lab (id INT, v INT)", "INSERT INTO ret_lab VALUES (1, 7) RETURNING id"], ["DROP TABLE ret_lab"]),
            Probe("sequence", ["CREATE SEQUENCE seq_lab", "SELECT NEXT VALUE FOR seq_lab"], ["DROP SEQUENCE seq_lab"], fetch_last=True),
            Probe("arrays", ["SELECT ARRAY[1,2,3]"], fetch_last=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e VECTOR(3))"], ["DROP TABLE vec_lab"]),
            Probe("statement_timeout", ["CALL sys.setquerytimeout(1)", "SELECT COUNT(*) FROM events e1, events e2 WHERE e1.id = e2.id + 1"], ["CALL sys.setquerytimeout(0)"], expect_error=True),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders ORDER BY customer_id"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v INT CHECK (v > 0))", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab"], expect_error=True),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["DELETE FROM order_items WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["CREATE TABLE sp_lab (v INT)", "INSERT INTO sp_lab VALUES (1)", "SAVEPOINT s1", "INSERT INTO sp_lab VALUES (2)", "ROLLBACK TO SAVEPOINT s1", "SELECT COUNT(*) FROM sp_lab"], ["DROP TABLE sp_lab"], fetch_last=True),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF SYSTEM TIME '-5s'"], fetch_last=True),
            Probe("json_aggregate", ["SELECT json.tojsonarray(name) FROM categories WHERE id <= 3"], fetch_last=True),
            Probe("isolation_serializable", ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"], []),
            Probe("isolation_repeatable_read", ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"], []),
            Probe("isolation_read_uncommitted", ["SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED"], []),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE pcre_match(name, '^Ultra')"], fetch_last=True, note="pcre module"),
        ]


# =====================================================================================
class Crate(Dialect):
    name = "crate"
    family = "crate"
    paramstyle = "qmark"
    supports_fk = False
    supports_create_index = False
    supports_transactions = False
    cross_lateral = None
    with_recursive = "WITH"
    type_map = {"int": "INTEGER", "bigint": "BIGINT", "varchar": "TEXT", "text": "TEXT",
                "decimal": "DOUBLE PRECISION", "bool": "BOOLEAN", "timestamp": "TIMESTAMP WITHOUT TIME ZONE", "json": "OBJECT(DYNAMIC)"}
    explain_plain = "EXPLAIN {sql}"
    explain_analyze = "EXPLAIN ANALYZE {sql}"
    fts_kind = "native"

    def create_table(self, table):
        s = super().create_table(table)
        if table.name == "products":
            s = s.replace("\n)", ",\n  INDEX description_ft USING FULLTEXT (description) WITH (analyzer = 'english')\n)")
        return s

    def analyze_table(self, table): return "ANALYZE"
    def truncate(self, table): return f"DELETE FROM {table}"
    def m_keyset_pred(self, c1, c2, v1, v2): return None
    def m_limit(self, n): return f"LIMIT {n}"
    def m_offset_limit(self, off, n): return f"LIMIT {n} OFFSET {off}"
    def m_json_get(self, col, key): return f"{col}['{key}']"
    def m_json_get_num(self, col, key): return f"{col}['{key}']"
    def m_date_trunc_day(self, col): return f"DATE_TRUNC('day', {col})"
    def m_date_trunc_month(self, col): return f"DATE_TRUNC('month', {col})"
    def m_date_sub_days(self, col, n): return f"{col} - INTERVAL '{n}' DAY"
    def m_epoch_diff_seconds(self, a, b): return f"(EXTRACT(EPOCH FROM {a}) - EXTRACT(EPOCH FROM {b}))"
    def m_string_agg_ordered(self, col, sep): return f"ARRAY_TO_STRING(ARRAY_AGG({col}), {sep})"
    def m_cast_str(self, expr, n): return f"CAST({expr} AS TEXT)"
    def m_ts(self, lit): return f"'{lit}'::timestamp"
    def m_percentile_cont(self, col, q): return f"PERCENTILE({col}, {q})"
    def m_fts(self, col, words): return f"MATCH(description_ft, '{words}')"
    def m_sleep(self, seconds): return None

    def query_overrides(self):
        return {
            "q14_upsert": ("INSERT INTO inventory (product_id, warehouse_id, qty, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                           "ON CONFLICT (product_id, warehouse_id) DO UPDATE SET qty = inventory.qty + excluded.qty, updated_at = CURRENT_TIMESTAMP"),
            "q15_merge": None, "q10_lateral_topn": None, "q06_recursive_cte": None, "q17b_keyset_rowvalue": None, "q29_txn_order": None,
        }

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab AS SELECT country_code, COUNT(*) AS n FROM customers GROUP BY country_code"], ["DROP MATERIALIZED VIEW mv_lab"]),
            Probe("partitioning", ["CREATE TABLE part_lab (id INT, d TIMESTAMP, m TIMESTAMP GENERATED ALWAYS AS date_trunc('month', d)) PARTITIONED BY (m)", "INSERT INTO part_lab (id, d) VALUES (1, '2025-06-01')"], ["DROP TABLE part_lab"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a INT, b INT GENERATED ALWAYS AS (a * 2))", "INSERT INTO gen_lab (a) VALUES (21)", "REFRESH TABLE gen_lab", "SELECT b FROM gen_lab"], ["DROP TABLE gen_lab"], fetch_last=True),
            Probe("identity", ["CREATE TABLE id_lab (id INT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, v INT)"], ["DROP TABLE id_lab"]),
            Probe("returning", ["CREATE TABLE ret_lab (id INT, v INT)", "INSERT INTO ret_lab VALUES (1, 7) RETURNING id, v"], ["DROP TABLE ret_lab"], fetch_last=True),
            Probe("sequence", ["CREATE SEQUENCE seq_lab"], []),
            Probe("arrays", ["SELECT ARRAY_CAT([1,2,3], [4])"], fetch_last=True),
            Probe("vector_type", ["CREATE TABLE vec_lab (e FLOAT_VECTOR(3))", "INSERT INTO vec_lab VALUES ([1,2,3])", "REFRESH TABLE vec_lab", "SELECT _score FROM vec_lab WHERE KNN_MATCH(e, [3,1,2], 1)"], ["DROP TABLE vec_lab"], fetch_last=True),
            Probe("statement_timeout", ["SET statement_timeout = '200ms'", "SELECT COUNT(*) FROM events e1, events e2 WHERE e1.id = e2.id + 1"], ["SET statement_timeout = 0"], expect_error=True, note="CrateDB 5.3+ statement_timeout"),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders ORDER BY customer_id"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v INT CHECK (v > 0))", "INSERT INTO ck_lab VALUES (0)"], ["DROP TABLE ck_lab"], expect_error=True),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], ["DELETE FROM order_items WHERE id = 999999999"], expect_error=True),
            Probe("savepoint", ["SAVEPOINT s1"], []),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF SYSTEM TIME '-5s'"], fetch_last=True),
            Probe("json_aggregate", ["SELECT ARRAY_AGG(name) FROM categories WHERE id <= 3"], fetch_last=True),
            Probe("isolation_serializable", ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"], [], note="no transactions"),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE name ~ '^Ultra'"], fetch_last=True),
            Probe("geo_type", ["CREATE TABLE geo_lab (p GEO_POINT)", "INSERT INTO geo_lab VALUES ([13.4, 52.5])", "REFRESH TABLE geo_lab", "SELECT DISTANCE(p, [13.5, 52.5]) FROM geo_lab"], ["DROP TABLE geo_lab"], fetch_last=True),
        ]


# =====================================================================================
class QuestDB(Dialect):
    name = "questdb"
    family = "questdb"
    paramstyle = "format"
    supports_fk = False
    supports_pk = False
    supports_create_index = False
    supports_transactions = False
    cross_lateral = None
    with_recursive = "WITH"
    type_map = {"int": "INT", "bigint": "LONG", "varchar": "VARCHAR", "text": "VARCHAR",
                "decimal": "DOUBLE", "bool": "BOOLEAN", "timestamp": "TIMESTAMP", "json": "VARCHAR"}
    explain_plain = "EXPLAIN {sql}"
    explain_analyze = None
    fts_kind = "like-fallback"
    SYMBOLS = {"status", "event_type", "tier", "country_code", "shipping_country"}

    def col_sql(self, col, table):
        if col.name in self.SYMBOLS:
            return f"{col.name} SYMBOL CAPACITY 256 CACHE"
        return f"{col.name} {self.sql_type(col)}"

    def table_suffix(self, table):
        if table.ts_col:
            return f" TIMESTAMP({table.ts_col}) PARTITION BY MONTH WAL"
        return ""

    def create_table(self, table):
        parts = [self.col_sql(c, table) for c in table.cols]
        return f"CREATE TABLE {table.name} (\n  " + ",\n  ".join(parts) + "\n)" + self.table_suffix(table)

    def truncate(self, table): return f"TRUNCATE TABLE {table}"
    def analyze_table(self, table): return None
    def m_keyset_pred(self, c1, c2, v1, v2): return None
    def m_limit(self, n): return f"LIMIT {n}"
    def m_offset_limit(self, off, n): return f"LIMIT {off},{int(off) + int(n)}"
    def m_json_get(self, col, key): return None
    def m_json_get_num(self, col, key): return None
    def m_date_trunc_day(self, col): return f"timestamp_floor('d', {col})"
    def m_date_trunc_month(self, col): return f"timestamp_floor('M', {col})"
    def m_year(self, col): return f"year({col})"
    def m_month(self, col): return f"month({col})"
    def m_date_sub_days(self, col, n): return f"dateadd('d', -{n}, {col})"
    def m_epoch_diff_seconds(self, a, b): return f"datediff('s', {b}, {a})"
    def m_string_agg_ordered(self, col, sep): return f"string_agg({col}, {sep})"
    def m_concat(self, *args): return f"concat({', '.join(args)})"
    def m_cast_str(self, expr, n): return f"CAST({expr} AS VARCHAR)"
    def m_ts(self, lit): return f"'{lit.replace(' ', 'T')}'"
    def m_now(self): return "now()"
    def m_percentile_cont(self, col, q): return f"approx_percentile({col}, {q}, 5)"
    def m_sleep(self, seconds): return None
    def m_bool_eq_true(self, col): return f"{col} = true"

    def query_overrides(self):
        return {"q14_upsert": None, "q15_merge": None, "q10_lateral_topn": None, "q06_recursive_cte": None, "q12_json_filter": None,
                "q17b_keyset_rowvalue": None, "q29_txn_order": None,
                "q08_exists_semijoin": None, "q09_correlated_scalar": None, "q19b_not_exists": None}   # no correlated subqueries

    def probes(self):
        return [
            Probe("materialized_view", ["CREATE MATERIALIZED VIEW mv_lab AS (SELECT occurred_at, event_type, count() AS n FROM events SAMPLE BY 1d) PARTITION BY MONTH", "SELECT count() AS n FROM mv_lab"], ["DROP MATERIALIZED VIEW mv_lab"], fetch_last=True, note="QuestDB incremental MV"),
            Probe("partitioning", ["CREATE TABLE part_lab (id INT, d TIMESTAMP) TIMESTAMP(d) PARTITION BY DAY WAL", "INSERT INTO part_lab VALUES (1, '2025-06-01T00:00:00Z')"], ["DROP TABLE part_lab"]),
            Probe("generated_column", ["CREATE TABLE gen_lab (a INT, b INT GENERATED ALWAYS AS (a * 2))"], ["DROP TABLE gen_lab"]),
            Probe("identity", ["CREATE TABLE id_lab (id INT GENERATED BY DEFAULT AS IDENTITY, v INT)"], ["DROP TABLE id_lab"]),
            Probe("returning", ["CREATE TABLE ret_lab (id INT, v INT)", "INSERT INTO ret_lab VALUES (1, 7) RETURNING id"], ["DROP TABLE ret_lab"]),
            Probe("sequence", ["CREATE SEQUENCE seq_lab"], []),
            Probe("arrays", ["SELECT ARRAY[1.0,2.0,3.0]"], fetch_last=True, note="QuestDB 9 N-dim arrays"),
            Probe("vector_type", ["CREATE TABLE vec_lab (e DOUBLE[])", "INSERT INTO vec_lab VALUES (ARRAY[1.0,2.0,3.0])"], ["DROP TABLE vec_lab"]),
            Probe("statement_timeout", ["SET statement_timeout = 200"], [], note="no session-level timeout; query.timeout.sec is a server-wide setting"),
            Probe("filter_clause", ["SELECT COUNT(*) FILTER (WHERE status = 'delivered') FROM orders"], fetch_last=True),
            Probe("distinct_on", ["SELECT DISTINCT ON (customer_id) customer_id, id FROM orders ORDER BY customer_id"], fetch_last=True),
            Probe("qualify", ["SELECT id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY id) <= 5"], fetch_last=True),
            Probe("check_constraint", ["CREATE TABLE ck_lab (v INT CHECK (v > 0))"], ["DROP TABLE ck_lab"]),
            Probe("fk_enforced", ["INSERT INTO order_items (id, order_id, product_id, qty, unit_price) VALUES (999999999, 1, 999999999, 1, 1)"], [], expect_error=True),
            Probe("savepoint", ["SAVEPOINT s1"], []),
            Probe("temporal_query", ["SELECT COUNT(*) FROM orders AS OF SYSTEM TIME '-5s'"], fetch_last=True),
            Probe("json_aggregate", ["SELECT string_agg(name, ',') FROM categories WHERE id <= 3"], fetch_last=True),
            Probe("isolation_serializable", ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"], []),
            Probe("regex_match", ["SELECT COUNT(*) FROM products WHERE name ~ '^Ultra'"], fetch_last=True),
            Probe("sample_by", ["SELECT occurred_at, count() FROM events SAMPLE BY 1M"], fetch_last=True, note="time-series SAMPLE BY"),
            Probe("asof_join", ["SELECT o.id, e.event_type FROM orders o ASOF JOIN events e ON (customer_id) LIMIT 5"], fetch_last=True, note="ASOF JOIN"),
        ]


DIALECTS: dict[str, type[Dialect]] = {
    "ansi": Dialect, "postgres": Postgres, "cockroach": Cockroach, "yugabyte": Yugabyte, "citus": Citus,
    "mysql": MySQL, "mariadb": MariaDB, "tidb": TiDB, "tsql": TSQL, "oracle": Oracle, "db2": Db2,
    "sqlite": SQLite, "duckdb": DuckDB, "clickhouse": ClickHouse, "firebird": Firebird, "h2": H2,
    "monetdb": MonetDB, "crate": Crate, "questdb": QuestDB,
}


def get_dialect(name: str) -> Dialect:
    return DIALECTS[name]()
