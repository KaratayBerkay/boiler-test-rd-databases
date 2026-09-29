"""Portable benchmark schema + per-dialect DDL generation.

The schema is an e-commerce style model chosen to exercise joins, aggregation, window
functions, recursive CTEs (category tree), JSON attributes, full-text search on
descriptions, UPSERT/MERGE (inventory) and high-volume time-ordered data (events).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Col:
    name: str
    ptype: str                 # portable type: int | bigint | varchar(n) | text | decimal(p,s) | bool | timestamp | json
    nullable: bool = False


@dataclass(frozen=True)
class Table:
    name: str
    cols: tuple[Col, ...]
    pk: tuple[str, ...]
    fks: tuple[tuple[str, str, str], ...] = ()          # (col, ref_table, ref_col)
    uniques: tuple[tuple[str, ...], ...] = ()
    indexes: tuple[tuple[str, ...], ...] = ()           # baseline secondary indexes
    order_hint: tuple[str, ...] = ()                     # for engines with ORDER BY / designated timestamp
    ts_col: str | None = None                            # designated timestamp (QuestDB / partitioning)

    @property
    def colnames(self) -> list[str]:
        return [c.name for c in self.cols]


TABLES: list[Table] = [
    Table("customers", (
        Col("id", "int"), Col("name", "varchar(100)"), Col("email", "varchar(150)"),
        Col("country_code", "varchar(2)"), Col("tier", "varchar(10)"), Col("created_at", "timestamp"),
        Col("attrs", "json", nullable=True)),
        pk=("id",), uniques=(("email",),), order_hint=("id",)),
    Table("categories", (
        Col("id", "int"), Col("parent_id", "int", nullable=True), Col("name", "varchar(100)"), Col("depth", "int")),
        pk=("id",), order_hint=("id",)),
    Table("products", (
        Col("id", "int"), Col("category_id", "int"), Col("sku", "varchar(20)"), Col("name", "varchar(150)"),
        Col("description", "text"), Col("price", "decimal(12,2)"), Col("attrs", "json", nullable=True), Col("active", "bool")),
        pk=("id",), fks=(("category_id", "categories", "id"),), indexes=(("category_id",),), order_hint=("id",)),
    Table("orders", (
        Col("id", "int"), Col("customer_id", "int"), Col("status", "varchar(12)"), Col("ordered_at", "timestamp"),
        Col("total_amount", "decimal(14,2)"), Col("shipping_country", "varchar(2)")),
        pk=("id",), fks=(("customer_id", "customers", "id"),), indexes=(("customer_id",),), order_hint=("id",), ts_col="ordered_at"),
    Table("order_items", (
        Col("id", "int"), Col("order_id", "int"), Col("product_id", "int"), Col("qty", "int"), Col("unit_price", "decimal(12,2)")),
        pk=("id",), fks=(("order_id", "orders", "id"), ("product_id", "products", "id")),
        indexes=(("order_id",), ("product_id",)), order_hint=("id",)),
    Table("events", (
        Col("id", "bigint"), Col("customer_id", "int"), Col("event_type", "varchar(20)"), Col("occurred_at", "timestamp"),
        Col("payload", "json", nullable=True), Col("value_num", "decimal(12,4)")),
        pk=("id",), indexes=(("customer_id",),), order_hint=("id",), ts_col="occurred_at"),
    # NOTE: deliberately no index on events(event_type, occurred_at) or orders(ordered_at):
    # the optimisation phase adds them and measures the difference.
    Table("inventory", (
        Col("product_id", "int"), Col("warehouse_id", "int"), Col("qty", "int"), Col("updated_at", "timestamp")),
        pk=("product_id", "warehouse_id"), order_hint=("product_id", "warehouse_id")),
]

TABLE_BY_NAME = {t.name: t for t in TABLES}
LOAD_ORDER = ["customers", "categories", "products", "orders", "order_items", "events", "inventory"]
DROP_ORDER = list(reversed(LOAD_ORDER))


def parse_ptype(ptype: str) -> tuple[str, list[str]]:
    if "(" in ptype:
        base, rest = ptype.split("(", 1)
        return base, [x.strip() for x in rest.rstrip(")").split(",")]
    return ptype, []
