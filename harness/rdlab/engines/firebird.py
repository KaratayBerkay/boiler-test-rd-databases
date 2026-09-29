"""Firebird via firebird-driver (needs libfbclient; path taken from FIREBIRD_CLIENT_LIB or lab.yaml features.fbclient)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..config import Target
from ..dialects import bind, render
from ..schema import Table
from .base import Conn, Engine


class FbConn(Conn):
    paramstyle = "qmark"

    ISOLATION = {"SNAPSHOT TABLE STABILITY": "SERIALIZABLE", "SERIALIZABLE": "SERIALIZABLE", "SNAPSHOT": "SNAPSHOT",
                 "REPEATABLE READ": "SNAPSHOT", "READ COMMITTED": "READ_COMMITTED"}

    def execute(self, sql, params=None, *, rendered=False, fetch=True):
        s = sql if rendered else self.prepare_sql(sql, len(params) if params else 0)
        head = s.strip().upper()
        if head.startswith("SET TRANSACTION ISOLATION LEVEL"):
            # Firebird sets isolation through the transaction parameter block, not as a statement
            from firebird.driver import tpb, Isolation
            from ..dialects import Unsupported
            lvl = head.replace("SET TRANSACTION ISOLATION LEVEL", "").strip()
            name = self.ISOLATION.get(lvl)
            if not name:
                raise Unsupported(f"firebird: isolation level {lvl} not available")
            self.raw.commit()
            self.raw.default_tpb = tpb(getattr(Isolation, name))
            return None
        cur = self.raw.cursor()
        try:
            cur.execute(s, tuple(params) if params else None)
            rows = None
            if fetch and cur.description:
                rows = [tuple(r) for r in cur.fetchall()]
            # DDL must be committed before the new object can be used, even inside an explicit transaction
            if self.autocommit or head.startswith(("CREATE", "DROP", "ALTER", "RECREATE")):
                self.raw.commit()
            return rows
        finally:
            cur.close()

    def executemany(self, sql, seq, *, rendered=False):
        s = sql if rendered else self.prepare_sql(sql, len(seq[0]) if seq else 0)
        cur = self.raw.cursor()
        try:
            cur.executemany(s, [tuple(p) for p in seq])
            if self.autocommit:
                self.raw.commit()
        finally:
            cur.close()

    def set_autocommit(self, on):
        self.autocommit = on

    def server_version(self):
        try:
            return f"Firebird {self.raw.info.server_version}"
        except Exception as e:  # noqa: BLE001
            return f"unknown ({e})"

    def role(self):
        return self.target.role

    def explain(self, sql, params, analyze):
        rendered = render(sql, self.dialect)
        s = bind(rendered, self.paramstyle, len(params) if params else 0)
        cur = self.raw.cursor()
        try:
            ps = cur.prepare(s)
            plan = ps.detailed_plan or ps.plan
            if analyze:
                cur.execute(ps, tuple(params) if params else None)
                if cur.description:
                    cur.fetchall()
                try:
                    from firebird.driver import DbInfoCode  # noqa: F401
                    plan += f"\nfetches={self.raw.info.get_info(DbInfoCode.FETCHES)} reads={self.raw.info.get_info(DbInfoCode.READS)}"
                except Exception:  # noqa: BLE001
                    pass
            if self.autocommit:
                self.raw.commit()
            return plan
        finally:
            try:
                ps.free()          # release the metadata lock held by the prepared statement
            except Exception:  # noqa: BLE001
                pass
            cur.close()


class FirebirdEngine(Engine):
    driver = "firebird-driver"

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        from firebird.driver import connect, driver_config
        lib = self.cfg.features.get("fbclient") or os.environ.get("FIREBIRD_CLIENT_LIB")
        if lib and not driver_config.fb_client_library.value:
            driver_config.fb_client_library.value = lib
        raw = connect(f"{target.host}/{target.port}:{target.database}", user=target.user, password=target.password, charset="UTF8")
        c = FbConn(self, raw, target)
        c.autocommit = autocommit
        return c

    def adapt_value(self, v, ptype):
        return v

    def max_connections(self, conn: Conn) -> int | None:
        return None
