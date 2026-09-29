"""ClickHouse via clickhouse-connect (HTTP). Parameters are inlined as literals."""
from __future__ import annotations

import datetime as dt
import decimal
import uuid
from pathlib import Path
from typing import Any

import clickhouse_connect

from ..config import Target
from ..datagen import read_csv_rows
from ..dialects import render
from ..schema import Table
from .base import Conn, Engine


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, decimal.Decimal):
        return format(v, "f")
    if isinstance(v, dt.datetime):
        return f"toDateTime64('{v.strftime('%Y-%m-%d %H:%M:%S')}', 6)"
    s = str(v).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


def inline(sql: str, params) -> str:
    out, i = [], 0
    for ch in sql:
        if ch == "?" and i < len(params):
            out.append(_lit(params[i]))
            i += 1
        else:
            out.append(ch)
    return "".join(out)


class CHConn(Conn):
    paramstyle = "qmark"

    def execute(self, sql, params=None, *, rendered=False, fetch=True, query_id: str | None = None):
        s = sql if rendered else render(sql, self.dialect)
        if params:
            s = inline(s, list(params))
        settings = {"query_id": query_id} if query_id else None
        head = s.lstrip()[:12].upper()
        if fetch and (head.startswith(("SELECT", "WITH", "SHOW", "EXPLAIN", "DESC", "EXISTS")) or head.startswith("(")):
            res = self.raw.query(s, settings=settings)
            return [tuple(r) for r in res.result_rows]
        self.raw.command(s, settings=settings)
        return None

    def executemany(self, sql, seq, *, rendered=False):
        """One multi-row INSERT (ClickHouse creates a part per INSERT, so never insert row by row)."""
        s = sql if rendered else render(sql, self.dialect)
        up = s.upper()
        if up.lstrip().startswith("INSERT") and " VALUES " in up:
            head, tail = s[:up.index(" VALUES ") + 8], s[up.index(" VALUES ") + 8:]
            tpl = tail.strip()
            for i in range(0, len(seq), 5000):
                chunk = seq[i:i + 5000]
                values = ", ".join(inline(tpl, list(p)) for p in chunk)
                self.execute(head + values, rendered=True, fetch=False)
            return
        for p in seq:
            self.execute(inline(s, list(p)), rendered=True, fetch=False)

    def set_autocommit(self, on):
        self.autocommit = on

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        try:
            self.raw.close()
        except Exception:  # noqa: BLE001
            pass

    def server_version(self):
        try:
            return f"ClickHouse {self.raw.server_version}"
        except Exception:  # noqa: BLE001
            return "ClickHouse"

    def role(self):
        return "node"

    def explain(self, sql, params, analyze):
        rendered = render(sql, self.dialect)
        if params:
            rendered = inline(rendered, list(params))
        if not analyze:
            rows = self.execute(f"EXPLAIN indexes = 1 {rendered}", rendered=True)
            return "\n".join(str(r[0]) for r in rows or [])
        qid = f"rdlab-{uuid.uuid4().hex[:12]}"
        self.execute(rendered, rendered=True, query_id=qid)
        self.raw.command("SYSTEM FLUSH LOGS")
        rows = self.execute("SELECT query_duration_ms, read_rows, read_bytes, memory_usage, result_rows "
                            f"FROM system.query_log WHERE query_id = '{qid}' AND type = 'QueryFinish' ORDER BY event_time DESC LIMIT 1", rendered=True)
        pipe = self.execute(f"EXPLAIN PIPELINE {rendered}", rendered=True)
        stats = rows[0] if rows else None
        return (f"query_log: duration_ms={stats[0]} read_rows={stats[1]} read_bytes={stats[2]} memory={stats[3]} result_rows={stats[4]}\n" if stats else "") + \
               "\n".join(str(r[0]) for r in pipe or [])


class ClickHouseEngine(Engine):
    driver = "clickhouse-connect"

    def apply_features(self, features):
        if features.get("replicated"):
            dl = self.dialect

            def suffix(table, _orig=dl.table_suffix):
                key = ", ".join(table.pk or table.order_hint)
                # {uuid} in the Keeper path (the Atomic-database default) so that RESTORE ... AS <other database> gets its own path
                return f" ENGINE = ReplicatedMergeTree('/clickhouse/tables/{{uuid}}/{{shard}}', '{{replica}}') ORDER BY ({key})"

            def create_table(table, _orig=dl.create_table):
                s = _orig(table)
                return s.replace(f"CREATE TABLE {table.name} (", f"CREATE TABLE {table.name} ON CLUSTER {features.get('cluster', 'lab_cluster')} (")

            dl.table_suffix = suffix
            dl.create_table = create_table
            cluster = features.get("cluster", "lab_cluster")
            dl.drop_table_if_exists = lambda t: f"DROP TABLE IF EXISTS {t} ON CLUSTER {cluster} SYNC"
            dl.drop_table = lambda t: f"DROP TABLE {t} ON CLUSTER {cluster} SYNC"

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        raw = clickhouse_connect.get_client(host=target.host, port=target.port, username=target.user, password=target.password,
                                            database=target.database or "default", connect_timeout=int(timeout), send_receive_timeout=600,
                                            settings={"max_execution_time": 600, "join_use_nulls": 1})
        c = CHConn(self, raw, target)
        c.autocommit = autocommit
        return c

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 100_000):
        n = 0
        buf: list[list] = []
        for row in read_csv_rows(csv_path, table.name):
            buf.append([float(v) if isinstance(v, decimal.Decimal) else v for v in row])
            if len(buf) >= batch:
                conn.raw.insert(table.name, buf, column_names=table.colnames)
                n += len(buf)
                buf = []
        if buf:
            conn.raw.insert(table.name, buf, column_names=table.colnames)
            n += len(buf)
        return n, "client.insert (native batches)"

    def max_connections(self, conn: Conn) -> int | None:
        try:
            r = conn.execute("SELECT value FROM system.server_settings WHERE name = 'max_connections'", rendered=True)
            return int(r[0][0]) if r else None
        except Exception:  # noqa: BLE001
            return None

    def replication_status(self, conn: Conn) -> dict[str, Any]:
        try:
            rows = conn.execute("SELECT database, table, is_leader, is_readonly, absolute_delay, queue_size, inserts_in_queue, total_replicas, active_replicas "
                                "FROM system.replicas", rendered=True)
            return {"replicas": rows}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
