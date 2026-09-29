"""MySQL-wire engines via PyMySQL: MySQL, MariaDB, TiDB, Percona, Dolt, OceanBase, StarRocks, ..."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pymysql

from ..config import Target
from ..schema import Table
from .base import Conn, Engine, EngineError


class MyConn(Conn):
    paramstyle = "format"

    def _adapt_params(self, params):
        return tuple(int(p) if isinstance(p, bool) else p for p in params)

    def set_autocommit(self, on):
        self.autocommit = on
        self.raw.autocommit(on)          # PyMySQL: autocommit is a method, not a property

    def server_version(self):
        try:
            return self.execute("SELECT VERSION()", rendered=True)[0][0]
        except Exception as e:  # noqa: BLE001
            return f"unknown ({e})"

    def role(self):
        try:
            ro = self.execute("SELECT @@read_only", rendered=True)[0][0]
            return "replica" if int(ro) else "primary"
        except Exception:  # noqa: BLE001
            return self.target.role


class PyMySQLEngine(Engine):
    driver = "pymysql"

    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        raw = pymysql.connect(host=target.host, port=target.port, user=target.user, password=target.password,
                              database=target.database or None, connect_timeout=int(timeout), read_timeout=600, write_timeout=600,
                              autocommit=autocommit, local_infile=True, charset="utf8mb4",
                              ssl_disabled=not bool(target.extra.get("ssl", False)))
        c = MyConn(self, raw, target)
        c.autocommit = autocommit
        return c

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 5000):
        if self.cfg.features.get("bulk_load") == "executemany":
            return super().bulk_load(conn, table, csv_path, batch)
        # LOAD DATA LOCAL INFILE with user variables so '' -> NULL and 'true'/'false' -> 1/0
        vars_, sets = [], []
        for c in table.cols:
            base = c.ptype.split("(")[0]
            if base == "bool":
                vars_.append(f"@v_{c.name}")
                sets.append(f"{c.name} = (@v_{c.name} = 'true')")
            elif c.nullable:
                vars_.append(f"@v_{c.name}")
                sets.append(f"{c.name} = NULLIF(@v_{c.name}, '')")
            else:
                vars_.append(c.name)
        sql = (f"LOAD DATA LOCAL INFILE '{csv_path}' INTO TABLE {table.name} "
               f"FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' LINES TERMINATED BY '\\n' IGNORE 1 LINES ({', '.join(vars_)})")
        if sets:
            sql += " SET " + ", ".join(sets)
        try:
            conn.execute(sql, rendered=True, fetch=False)
            return self.count(conn, table.name), "LOAD DATA LOCAL INFILE"
        except Exception as e:  # noqa: BLE001
            n, m = super().bulk_load(conn, table, csv_path, batch)
            return n, f"executemany (LOAD DATA failed: {str(e)[:120]})"

    def adapt_value(self, v, ptype):
        if isinstance(v, bool):
            return int(v)
        return v

    def fts_setup(self, conn: Conn) -> list[str]:
        s = "CREATE FULLTEXT INDEX ft_products_description ON products (description)"
        try:
            conn.execute(s, rendered=True, fetch=False)
            return [s]
        except Exception as e:  # noqa: BLE001
            return [f"FAILED {s}: {e}"]

    def max_connections(self, conn: Conn) -> int | None:
        try:
            return int(conn.execute("SELECT @@max_connections", rendered=True)[0][0])
        except Exception:  # noqa: BLE001
            return None

    def replication_status(self, conn: Conn) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            out["read_only"] = conn.execute("SELECT @@read_only", rendered=True)[0][0]
            cur = conn.raw.cursor(pymysql.cursors.DictCursor)
            try:
                cur.execute("SHOW REPLICA STATUS")
            except Exception:  # noqa: BLE001
                cur.execute("SHOW SLAVE STATUS")
            row = cur.fetchone()
            cur.close()
            if row:
                keys = ["Replica_IO_Running", "Replica_SQL_Running", "Seconds_Behind_Source", "Source_Host", "Executed_Gtid_Set",
                        "Last_IO_Error", "Last_SQL_Error", "Slave_IO_Running", "Slave_SQL_Running", "Seconds_Behind_Master", "Master_Host",
                        "Gtid_IO_Pos", "Using_Gtid", "Last_Error"]
                out["replica_status"] = {k: row[k] for k in keys if k in row}
            else:
                out["replica_status"] = None
            try:
                out["gtid_executed"] = conn.execute("SELECT @@global.gtid_executed", rendered=True)[0][0]
            except Exception:  # noqa: BLE001
                try:
                    out["gtid_executed"] = conn.execute("SELECT @@global.gtid_current_pos", rendered=True)[0][0]
                except Exception:  # noqa: BLE001
                    pass
            try:
                cur = conn.raw.cursor(pymysql.cursors.DictCursor)
                cur.execute("SHOW REPLICAS")
                out["replicas"] = cur.fetchall()
                cur.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
        return out

    def promote(self, target: Target) -> str:
        admin = Target(name="admin", role="admin", host=target.host, port=target.port, user=target.extra.get("admin_user", "root"),
                       password=target.extra.get("admin_password", target.password), database="")
        c = self.connect(admin)
        stmts = []
        try:
            for s in ("STOP REPLICA", "RESET REPLICA ALL", "SET GLOBAL super_read_only = OFF", "SET GLOBAL read_only = OFF"):
                try:
                    c.execute(s, rendered=True, fetch=False)
                    stmts.append(s)
                except Exception as e:  # noqa: BLE001
                    alt = s.replace("REPLICA", "SLAVE")
                    if alt != s:
                        try:
                            c.execute(alt, rendered=True, fetch=False)
                            stmts.append(alt)
                            continue
                        except Exception:  # noqa: BLE001
                            pass
                    stmts.append(f"FAILED {s}: {e}")
        finally:
            c.close()
        return "; ".join(stmts)

    def error_code(self, exc: BaseException) -> str | None:
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int):
            return str(args[0])
        return super().error_code(exc)
