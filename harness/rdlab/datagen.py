"""Deterministic synthetic data generator (seeded) producing typed rows and cached CSVs."""
from __future__ import annotations

import csv
import datetime as dt
import decimal
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .schema import LOAD_ORDER, TABLE_BY_NAME
from .util import DATA

SEED = 20260912
T_START = dt.datetime(2024, 1, 1)
T_END = dt.datetime(2025, 12, 31, 23, 59, 59)
SPAN_S = int((T_END - T_START).total_seconds())

COUNTRIES = [("US", 30), ("DE", 12), ("GB", 10), ("TR", 10), ("FR", 8), ("NL", 5), ("ES", 5), ("IT", 5),
             ("BR", 4), ("IN", 4), ("JP", 3), ("CA", 2), ("AU", 2)]
TIERS = [("free", 70), ("plus", 20), ("pro", 8), ("vip", 2)]
STATUSES = [("delivered", 70), ("shipped", 10), ("pending", 8), ("cancelled", 7), ("returned", 5)]
EVENT_TYPES = [("page_view", 60), ("add_to_cart", 15), ("login", 10), ("checkout", 8), ("review", 4),
               ("support", 2), ("churn", 1)]
COLORS = ["red", "blue", "green", "black", "white", "silver", "gold"]
MATERIALS = ["steel", "plastic", "wood", "glass", "carbon", "cotton"]
ADJ = ["wireless", "portable", "ergonomic", "compact", "premium", "rugged", "smart", "eco", "ultra", "classic"]
NOUN = ["charger", "headphones", "keyboard", "lamp", "backpack", "bottle", "camera", "speaker", "monitor", "router",
        "tripod", "blender", "kettle", "jacket", "sneakers", "notebook", "drone", "watch", "mouse", "cable"]
FIRST = ["Ada", "Berk", "Cem", "Deniz", "Ece", "Fatih", "Gul", "Hakan", "Ipek", "Jale", "Kaan", "Leyla", "Mert", "Nil",
         "Omer", "Pinar", "Rana", "Selin", "Tolga", "Umut", "Vera", "Yusuf", "Zeynep", "Liam", "Olivia", "Noah", "Emma",
         "Mia", "Lucas", "Sofia"]
LAST = ["Karatay", "Yilmaz", "Kaya", "Demir", "Sahin", "Celik", "Yildiz", "Aydin", "Ozturk", "Arslan", "Smith", "Muller",
        "Garcia", "Rossi", "Silva", "Tanaka", "Dubois", "Novak", "Brown", "Jansen"]
WORDS = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore magna "
         "aliqua quality battery fast light durable design value warranty bluetooth usb charging stereo noise cancelling "
         "waterproof travel office gaming kitchen outdoor fitness wireless charger compact premium rugged smart eco ultra "
         "classic aluminium steel leather fabric mesh foam rubber glass ceramic bamboo recycled solar magnetic foldable "
         "adjustable ergonomic lightweight heavy sturdy slim wide narrow tall short round square modern vintage minimal "
         "bright dim warm cool silent loud powerful efficient reliable affordable luxury handmade imported local seasonal "
         "limited edition bundle refurbished certified tested approved rated popular trending new improved upgraded "
         "professional beginner advanced family kids adult pet garden bathroom bedroom living dining balcony garage "
         "workshop studio classroom hospital hotel restaurant camping hiking cycling running swimming yoga winter summer "
         "spring autumn morning evening daily weekly monthly annual gift holiday birthday anniversary wedding").split()


def _weighted(rng: random.Random, pairs: list[tuple[str, int]]) -> str:
    vals, weights = zip(*pairs)
    return rng.choices(vals, weights=weights, k=1)[0]


def _ts(rng: random.Random, start: dt.datetime = T_START, span: int = SPAN_S) -> dt.datetime:
    return (start + dt.timedelta(seconds=rng.randrange(span))).replace(microsecond=0)


@dataclass(frozen=True)
class Sizes:
    customers: int
    categories: int
    products: int
    orders: int
    events: int
    warehouses: int = 4

    @classmethod
    def for_scale(cls, scale: float) -> "Sizes":
        return cls(customers=max(1000, int(20_000 * scale)), categories=200, products=max(500, int(5_000 * scale)),
                   orders=max(5000, int(200_000 * scale)), events=max(20_000, int(1_000_000 * scale)))


def gen_customers(rng: random.Random, n: int) -> Iterator[tuple]:
    for i in range(1, n + 1):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        attrs = {"newsletter": rng.random() < 0.4, "age": rng.randint(18, 80), "lang": rng.choice(["en", "tr", "de", "fr"])}
        yield (i, name, f"user{i}@example.com", _weighted(rng, COUNTRIES), _weighted(rng, TIERS), _ts(rng),
               json.dumps(attrs) if rng.random() < 0.9 else None)


def gen_categories(rng: random.Random, n: int) -> Iterator[tuple]:
    # 10 roots, then a tree up to depth 3 under them
    roots = 10
    rows = []
    for i in range(1, roots + 1):
        rows.append((i, None, f"Category {i}", 0))
    for i in range(roots + 1, n + 1):
        parent = rng.randint(1, i - 1)
        depth = rows[parent - 1][3] + 1
        if depth > 3:
            parent = rng.randint(1, roots)
            depth = 1
        rows.append((i, parent, f"Category {i}", depth))
    yield from rows


def gen_products(rng: random.Random, n: int, n_categories: int) -> Iterator[tuple]:
    for i in range(1, n + 1):
        name = f"{rng.choice(ADJ).title()} {rng.choice(NOUN).title()} {rng.choice(['X', 'Pro', 'Mini', 'Max', 'Lite'])}{rng.randint(1, 99)}"
        desc = " ".join(rng.choice(WORDS) for _ in range(rng.randint(20, 60)))
        attrs = {"color": rng.choice(COLORS), "weight": round(rng.uniform(0.1, 30.0), 2), "material": rng.choice(MATERIALS),
                 "tags": rng.sample(ADJ, 2)}
        price = decimal.Decimal(rng.randint(199, 99999)) / 100
        yield (i, rng.randint(1, n_categories), f"SKU-{i:07d}", name, desc, price, json.dumps(attrs), rng.random() < 0.85)


def gen_orders_and_items(rng: random.Random, n_orders: int, n_customers: int, n_products: int,
                         product_prices: list[decimal.Decimal]) -> tuple[list[tuple], list[tuple]]:
    orders: list[tuple] = []
    items: list[tuple] = []
    item_id = 0
    for oid in range(1, n_orders + 1):
        cid = rng.randint(1, n_customers)
        n_items = rng.choices([1, 2, 3, 4, 5, 6], weights=[30, 30, 20, 10, 6, 4], k=1)[0]
        total = decimal.Decimal(0)
        for _ in range(n_items):
            item_id += 1
            pid = rng.randint(1, n_products)
            qty = rng.choices([1, 2, 3, 4, 5], weights=[60, 20, 10, 6, 4], k=1)[0]
            unit = product_prices[pid - 1]
            items.append((item_id, oid, pid, qty, unit))
            total += unit * qty
        orders.append((oid, cid, _weighted(rng, STATUSES), _ts(rng), total, _weighted(rng, COUNTRIES)))
    return orders, items


def gen_events(rng: random.Random, n: int, n_customers: int) -> Iterator[tuple]:
    for i in range(1, n + 1):
        cid = rng.randint(1, n_customers)
        et = _weighted(rng, EVENT_TYPES)
        payload = {"page": f"/p/{rng.randint(1, 5000)}", "ref": rng.choice(["direct", "search", "email", "social"]),
                   "device": rng.choice(["mobile", "desktop", "tablet"])}
        yield (i, cid, et, _ts(rng), json.dumps(payload) if rng.random() < 0.8 else None,
               decimal.Decimal(rng.randint(0, 100000)) / 10000)


def gen_inventory(rng: random.Random, n_products: int, warehouses: int) -> Iterator[tuple]:
    for pid in range(1, n_products + 1):
        for w in range(1, warehouses + 1):
            yield (pid, w, rng.randint(0, 500), _ts(rng))


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, decimal.Decimal):
        return format(v, "f")
    return str(v)


def csv_dir(scale: float) -> Path:
    return DATA / f"scale-{scale:g}"


def ensure_csvs(scale: float, force: bool = False) -> dict[str, Path]:
    """Generate (once) deterministic CSV files for the given scale; return table -> path."""
    d = csv_dir(scale)
    marker = d / "_complete"
    paths = {t: d / f"{t}.csv" for t in LOAD_ORDER}
    if marker.exists() and not force:
        return paths
    d.mkdir(parents=True, exist_ok=True)
    sizes = Sizes.for_scale(scale)
    rng = random.Random(SEED)

    def write(table: str, rows) -> int:
        n = 0
        with open(paths[table], "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(TABLE_BY_NAME[table].colnames)
            for r in rows:
                w.writerow([_fmt(v) for v in r])
                n += 1
        return n

    counts = {}
    counts["customers"] = write("customers", gen_customers(rng, sizes.customers))
    counts["categories"] = write("categories", gen_categories(rng, sizes.categories))
    products = list(gen_products(rng, sizes.products, sizes.categories))
    counts["products"] = write("products", products)
    prices = [p[5] for p in products]
    orders, items = gen_orders_and_items(rng, sizes.orders, sizes.customers, sizes.products, prices)
    counts["orders"] = write("orders", orders)
    counts["order_items"] = write("order_items", items)
    del orders, items
    counts["events"] = write("events", gen_events(rng, sizes.events, sizes.customers))
    counts["inventory"] = write("inventory", gen_inventory(rng, sizes.products, sizes.warehouses))
    (d / "counts.json").write_text(json.dumps(counts, indent=2))
    marker.write_text("ok")
    return paths


def read_csv_rows(path: Path, table: str) -> Iterator[list]:
    """Yield typed rows from a cached CSV (types restored from the portable schema)."""
    t = TABLE_BY_NAME[table]
    kinds = []
    for c in t.cols:
        base = c.ptype.split("(")[0]
        kinds.append(base)
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            out = []
            for kind, v in zip(kinds, row):
                if v == "":
                    out.append(None)
                elif kind in ("int", "bigint"):
                    out.append(int(v))
                elif kind == "decimal":
                    out.append(decimal.Decimal(v))
                elif kind == "bool":
                    out.append(v == "true")
                elif kind == "timestamp":
                    out.append(dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S"))
                else:
                    out.append(v)
            yield out


def counts_for(scale: float) -> dict[str, int]:
    p = csv_dir(scale) / "counts.json"
    return json.loads(p.read_text()) if p.exists() else {}
