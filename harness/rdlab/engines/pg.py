"""PostgreSQL-wire engines via psycopg 3: PostgreSQL, CockroachDB, YugabyteDB, TimescaleDB, Citus, ..."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

from ..config import Target
from ..schema import Table
from .. import dockerctl
from .base import Conn, Engine, EngineError


class PgConn(Conn):
    paramstyle = "format"

    def __init__(self, engine, raw, target, prepare: bool = False):
        super().__init__(engine, raw, target)
        self.prepare = prepare

    def execute(self, sql, params=None, *, rendered=False, fetch=True):
        s = sql if rendered else self.prepare_sql(sql, len(params) if params else 0)
        with self.raw.cursor() as cur:
            cur.execute(s, tuple(params) if params else None, prepare=self.prepare or None)
            if fetch and cur.description is not None:
                return [tuple(r) for r in cur.fetchall()]
            return None

    def executemany(self, sql, seq, *, rendered=False):
        s = sql if rendered else self.prepare_sql(sql, len(seq[0]) if seq else 0)
        with self.raw.cursor() as cur:
            cur.executemany(s, [tuple(p) for p in seq])

    def server_version(self):
        try:
            return self.execute("SELECT version()", rendered=True)[0][0]
        except Exception as e:  # noqa: BLE001
            return f"unknown ({e})"

    def role(self):
        try:
            r = self.execute("SELECT pg_is_in_recovery()", rendered=True)[0][0]
            return "replica" if r else "primary"
        except Exception:  # noqa: BLE001
            return self.target.role


class PsycopgEngine(Engine):
    driver = "psycopg"

    def conninfo(self, target: Target, timeout: float) -> str:
        """Build a libpq conninfo string; empty values are omitted (libpq treats `password= dbname=x`
        as password="dbname=x")."""
        ssl = target.extra.get("sslmode", "disable")
        parts = {"host": target.host, "port": target.port, "user": target.user, "password": target.password, "dbname": target.database,
                 "connect_timeout": int(timeout), "sslmode": ssl, "application_name": "rdlab"}
        opts = target.extra.get("options", "")
        if opts:
            parts["options"] = f"'{opts}'"
        return " ".join(f"{k}={v}" for k, v in parts.items() if v not in (None, ""))

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        raw = psycopg.connect(self.conninfo(target, timeout), autocommit=autocommit)
        prep = bool(self.cfg.features.get("prepare_statements", False))
        c = PgConn(self, raw, target, prepare=prep)
        c.autocommit = autocommit
        return c

    def create_schema(self, conn, tables):
        stmts = super().create_schema(conn, tables)
        if self.cfg.features.get("citus"):
            for st in self.dialect.distribute_statements():
                conn.execute(st, rendered=True, fetch=False)
                stmts.append(st)
        return stmts

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 5000):
        if self.cfg.features.get("bulk_load") == "executemany":
            return super().bulk_load(conn, table, csv_path, batch)
        if self.cfg.features.get("bulk_load") == "inline_multirow":
            # for PostgreSQL-protocol emulations (H2) whose extended-protocol/batch support is partial: plain SQL text
            from ..datagen import read_csv_rows
            import datetime as _dt
            import decimal as _dec

            def lit(v):
                if v is None:
                    return "NULL"
                if isinstance(v, bool):
                    return "TRUE" if v else "FALSE"
                if isinstance(v, (int, float)):
                    return repr(v)
                if isinstance(v, _dec.Decimal):
                    return format(v, "f")
                if isinstance(v, _dt.datetime):
                    return "TIMESTAMP '" + v.strftime("%Y-%m-%d %H:%M:%S") + "'"
                return "'" + str(v).replace("'", "''") + "'"

            cols = ", ".join(table.colnames)
            n = 0
            buf: list[str] = []
            for row in read_csv_rows(csv_path, table.name):
                buf.append("(" + ", ".join(lit(v) for v in row) + ")")
                if len(buf) >= 500:
                    conn.execute(f"INSERT INTO {table.name} ({cols}) VALUES " + ", ".join(buf), rendered=True, fetch=False)
                    n += len(buf)
                    buf = []
            if buf:
                conn.execute(f"INSERT INTO {table.name} ({cols}) VALUES " + ", ".join(buf), rendered=True, fetch=False)
                n += len(buf)
            return n, "multi-row INSERT with inlined literals (500/stmt)"
        # The generated CSV is already COPY-compatible (header row, '' = NULL, true/false booleans,
        # ISO timestamps), so stream the raw file bytes straight into COPY.
        cols = ", ".join(table.colnames)
        with conn.raw.cursor() as cur:
            with cur.copy(f"COPY {table.name} ({cols}) FROM STDIN (FORMAT csv, HEADER true, NULL '')") as cp:
                with open(csv_path, "rb") as f:
                    while chunk := f.read(8 << 20):
                        cp.write(chunk)
        if not conn.autocommit:
            conn.commit()
        return self.count(conn, table.name), "COPY FROM STDIN (raw csv stream)"

    def fts_setup(self, conn: Conn) -> list[str]:
        if self.cfg.dialect == "cockroach":
            stmts = ["CREATE INDEX ix_products_fts ON products USING GIN (to_tsvector('english', description))"]
        else:
            stmts = ["CREATE INDEX ix_products_fts ON products USING GIN (to_tsvector('english', description))"]
        done = []
        for s in stmts:
            try:
                conn.execute(s, rendered=True, fetch=False)
                done.append(s)
            except Exception as e:  # noqa: BLE001
                done.append(f"FAILED {s}: {e}")
        return done

    def max_connections(self, conn: Conn) -> int | None:
        try:
            v = int(conn.execute("SHOW max_connections", rendered=True)[0][0])
            return v if v > 0 else None       # CockroachDB reports -1 (unlimited)
        except Exception:  # noqa: BLE001
            return None

    def replication_status(self, conn: Conn) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.cfg.dialect == "cockroach":
            try:
                out["node_id"] = conn.execute("SELECT node_id FROM [SHOW node_id]", rendered=True)[0][0]
                rows = conn.execute("SELECT range_id, lease_holder, replicas FROM [SHOW RANGES FROM DATABASE lab WITH DETAILS] LIMIT 5", rendered=True)
                out["ranges_sample"] = [tuple(str(x) for x in r) for r in rows]
            except Exception as e:  # noqa: BLE001
                out["error"] = str(e)[:200]
            return out
        try:
            out["in_recovery"] = conn.execute("SELECT pg_is_in_recovery()", rendered=True)[0][0]
            if out["in_recovery"]:
                rows = conn.execute("SELECT status, sender_host, flushed_lsn, latest_end_lsn, "
                                    "EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp())) AS replay_delay_s "
                                    "FROM pg_stat_wal_receiver", rendered=True)
                out["wal_receiver"] = rows[0] if rows else None
                out["last_replay_lsn"] = conn.execute("SELECT pg_last_wal_replay_lsn()", rendered=True)[0][0]
            else:
                rows = conn.execute("SELECT application_name, client_addr::text, state, sync_state, "
                                    "pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes, "
                                    "EXTRACT(EPOCH FROM write_lag), EXTRACT(EPOCH FROM flush_lag), EXTRACT(EPOCH FROM replay_lag) "
                                    "FROM pg_stat_replication", rendered=True)
                out["pg_stat_replication"] = rows
                out["current_lsn"] = conn.execute("SELECT pg_current_wal_lsn()", rendered=True)[0][0]
                out["synchronous_standby_names"] = conn.execute("SHOW synchronous_standby_names", rendered=True)[0][0]
                out["synchronous_commit"] = conn.execute("SHOW synchronous_commit", rendered=True)[0][0]
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
        return out

    def promote(self, target: Target) -> str:
        p = self.cfg.replication.get("promote", {})
        if self.cfg.features.get("citus") and target.container:
            # 1) promote the streaming replica, 2) repoint the coordinator's metadata from the dead worker to it.
            # The dead worker is unreachable, so metadata sync to it must be skipped while the node table is edited.
            cmd = p.get("cmd") or ["pg_ctl", "promote", "-D", "/var/lib/postgresql/18/docker"]
            rc, out, err = dockerctl.exec_in(target.container, cmd, user=p.get("user", "postgres"))
            if rc != 0:
                raise EngineError(f"promote failed rc={rc}: {err or out}")
            upstream = target.extra.get("upstream")
            dockerctl.wait_for(lambda: not self.connect(target).execute("SELECT pg_is_in_recovery()", rendered=True)[0][0], timeout=60, desc="replica promoted")
            steps = [
                "SET citus.enable_metadata_sync = off",
                f"SELECT citus_update_node(nodeid, '{target.name}-stale', 5432) FROM pg_dist_node WHERE nodename = '{target.name}' AND noderole = 'secondary'",
                f"SELECT citus_update_node(nodeid, '{target.name}', 5432) FROM pg_dist_node WHERE nodename = '{upstream}' AND noderole = 'primary'",
                f"UPDATE pg_dist_node SET isactive = false WHERE nodename = '{target.name}-stale'",
                "SET citus.enable_metadata_sync = on",
                f"SELECT citus_remove_node('{target.name}-stale', 5432)",
                "SELECT start_metadata_sync_to_all_nodes()",
            ]
            log = []
            coord = self.connect(self.cfg.primary)
            try:
                for st in steps:
                    try:
                        coord.execute(st, rendered=True, fetch=False)
                        log.append(f"ok: {st[:70]}")
                    except Exception as e:  # noqa: BLE001
                        log.append(f"FAILED: {st[:60]} -> {str(e)[:120]}")
            finally:
                coord.close()
            return " ".join(cmd) + " | " + "; ".join(log)
        if p.get("sql"):
            c = self.connect(target)
            try:
                c.execute(p["sql"], rendered=True)
            finally:
                c.close()
            return p["sql"]
        if target.container:
            cmd = p.get("cmd") or ["pg_ctl", "promote", "-D", "/var/lib/postgresql/data"]
            rc, out, err = dockerctl.exec_in(target.container, cmd, user=p.get("user", "postgres"))
            if rc != 0:
                raise EngineError(f"promote failed rc={rc}: {err or out}")
            return " ".join(cmd)
        raise EngineError("no promote method configured")

    def error_code(self, exc: BaseException) -> str | None:
        code = getattr(exc, "sqlstate", None)
        if code:
            return str(code)
        msg = str(exc)
        if "too many clients" in msg or "remaining connection slots" in msg or "too many connections" in msg:
            return "53300"
        if "connection refused" in msg.lower() or "could not connect" in msg.lower():
            return "08001"
        if "timeout expired" in msg.lower():
            return "08001-timeout"
        return super().error_code(exc)
