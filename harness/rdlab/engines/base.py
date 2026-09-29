"""Uniform connection wrapper + engine base class (DB-API 2 flavoured)."""
from __future__ import annotations

import datetime as dt
import decimal
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..config import StackConfig, Target
from ..datagen import read_csv_rows
from ..dialects import Dialect, bind, get_dialect, render
from ..schema import Table


class EngineError(RuntimeError):
    pass


class Conn:
    """Wraps a DB-API connection. Subclasses adapt driver quirks."""
    paramstyle = "qmark"

    def __init__(self, engine: "Engine", raw: Any, target: Target):
        self.engine = engine
        self.raw = raw
        self.target = target
        self.dialect: Dialect = engine.dialect
        self.autocommit = True

    # --- statement execution -------------------------------------------------------
    def prepare_sql(self, sql: str, nparams: int = 0) -> str:
        return bind(render(sql, self.dialect), self.paramstyle, nparams)

    def cursor(self):
        return self.raw.cursor()

    def execute(self, sql: str, params: tuple | list | None = None, *, rendered: bool = False,
                fetch: bool = True) -> list[tuple] | None:
        """Execute one statement; return all rows when the statement produced a result set."""
        s = sql if rendered else self.prepare_sql(sql, len(params) if params else 0)
        cur = self.cursor()
        try:
            if params:
                cur.execute(s, self._adapt_params(params))
            else:
                cur.execute(s)
            rows = None
            if fetch and self._has_rows(cur):
                rows = [tuple(r) for r in cur.fetchall()]
            if self.autocommit and self.engine.manual_autocommit:
                self.raw.commit()
            return rows
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass

    def execute_script(self, statements: Iterable[str]) -> None:
        for s in statements:
            self.execute(s, fetch=False)

    def executemany(self, sql: str, seq: list[tuple], *, rendered: bool = False) -> None:
        s = sql if rendered else self.prepare_sql(sql, len(seq[0]) if seq else 0)
        cur = self.cursor()
        try:
            cur.executemany(s, [self._adapt_params(p) for p in seq])
            if self.autocommit and self.engine.manual_autocommit:
                self.raw.commit()
        finally:
            cur.close()

    def _has_rows(self, cur) -> bool:
        return getattr(cur, "description", None) is not None

    def _adapt_params(self, params):
        return tuple(params)

    # --- transactions ------------------------------------------------------------------
    def set_autocommit(self, on: bool) -> None:
        self.autocommit = on
        if not self.engine.manual_autocommit:
            try:
                self.raw.autocommit = on
            except Exception:  # noqa: BLE001
                pass

    def begin(self) -> None:
        self.set_autocommit(False)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        try:
            self.raw.rollback()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self.raw.close()
        except Exception:  # noqa: BLE001
            pass

    # --- introspection ---------------------------------------------------------------
    def server_version(self) -> str:
        return "unknown"

    def role(self) -> str:
        return self.target.role

    def explain(self, sql: str, params: tuple | None, analyze: bool) -> str | None:
        rendered = render(sql, self.dialect)
        tpl = self.dialect.explain(rendered, analyze)
        if not tpl:
            return None
        if params:
            rows = self.execute(bind(tpl, self.paramstyle, len(params)), params, rendered=True)
        else:
            rows = self.execute(tpl, rendered=True)
        return "\n".join(" | ".join(str(c) for c in r) for r in (rows or []))


def looks_unsupported(msg: str | None) -> bool:
    """Post-hoc version of Engine.is_unsupported_error for stored error strings (used by the report builders)."""
    if not msg:
        return False
    m = msg.lower()
    return any(h in m for h in Engine.UNSUPPORTED_HINTS) or any(f"({c}," in m or f"ora-{c[4:]}" == c.lower() for c in Engine.UNSUPPORTED_CODES if c.isdigit() and len(c) >= 4)


class Engine:
    driver = "base"
    manual_autocommit = False        # True when the driver has no autocommit attribute (commit after each stmt)
    supports_explain_params = True

    def __init__(self, cfg: StackConfig):
        self.cfg = cfg
        self.dialect = get_dialect(cfg.dialect)
        self.apply_features(cfg.features)

    def apply_features(self, features: dict) -> None:
        pass

    # --- connections ---------------------------------------------------------------
    def connect(self, target: Target, *, autocommit: bool = True, timeout: float = 10.0) -> Conn:
        raise NotImplementedError

    def connect_primary(self, **kw) -> Conn:
        return self.connect(self.cfg.primary, **kw)

    # --- schema / data -------------------------------------------------------------
    def create_schema(self, conn: Conn, tables: list[Table]) -> list[str]:
        stmts = self.dialect.schema_ddl(tables)
        for s in stmts:
            conn.execute(s, rendered=True, fetch=False)
        return stmts

    def drop_schema(self, conn: Conn, tables: list[Table]) -> None:
        for t in reversed(tables):
            try:
                conn.execute(self.dialect.drop_table_if_exists(t.name), rendered=True, fetch=False)
            except Exception:  # noqa: BLE001
                try:
                    conn.execute(self.dialect.drop_table(t.name), rendered=True, fetch=False)
                except Exception:  # noqa: BLE001
                    pass

    def bulk_load(self, conn: Conn, table: Table, csv_path: Path, batch: int = 5000) -> tuple[int, str]:
        """Default: batched executemany INSERT. Returns (rows, method)."""
        cols = table.colnames
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {table.name} ({', '.join(cols)}) VALUES ({placeholders})"
        n = 0
        conn.set_autocommit(False)
        buf: list[tuple] = []
        try:
            for row in read_csv_rows(csv_path, table.name):
                buf.append(tuple(self.adapt_value(v, c.ptype) for v, c in zip(row, table.cols)))
                if len(buf) >= batch:
                    conn.executemany(sql, buf)
                    n += len(buf)
                    buf = []
            if buf:
                conn.executemany(sql, buf)
                n += len(buf)
            conn.commit()
        finally:
            conn.set_autocommit(True)
        return n, "executemany"

    def adapt_value(self, v: Any, ptype: str) -> Any:
        return v

    def after_load(self, conn: Conn, tables: list[Table]) -> list[str]:
        """Statistics refresh etc. Returns statements executed."""
        done = []
        for t in tables:
            s = self.dialect.analyze_table(t.name)
            if s:
                try:
                    conn.execute(s, rendered=True, fetch=False)
                    done.append(s)
                except Exception as e:  # noqa: BLE001
                    done.append(f"FAILED {s}: {e}")
        return done

    def fts_setup(self, conn: Conn) -> list[str]:
        return []

    # --- introspection ---------------------------------------------------------------
    def max_connections(self, conn: Conn) -> int | None:
        return None

    def count(self, conn: Conn, table: str) -> int:
        rows = conn.execute(f"SELECT COUNT(*) FROM {table}", rendered=True)
        return int(rows[0][0]) if rows else -1

    # --- replication hooks ---------------------------------------------------------
    def replication_status(self, conn: Conn) -> dict[str, Any]:
        return {}

    def promote(self, target: Target) -> str:
        raise EngineError(f"{self.driver}: promote not implemented")

    UNSUPPORTED_CODES = {"0A000", "42601", "42883", "42P01", "42704", "42809", "42P10",      # PostgreSQL family
                         "1064", "1305", "1235", "1064", "3029", "6037", "4028",              # MySQL / MariaDB / TiDB
                         "102", "156", "195", "319", "1035", "35100",                        # SQL Server
                         "ORA-00900", "ORA-00907", "ORA-00933", "ORA-00904", "ORA-00923", "ORA-00902", "ORA-00920", "ORA-00936", "ORA-02000", "ORA-02179", "ORA-00911",
                         "-104", "-440", "-204", "-199", "-206", "-901",                    # Db2 / Firebird
                         "62", "46", "36", "48", "344", "1", "47", "43", "27"}                    # ClickHouse
    UNSUPPORTED_HINTS = ("syntax error", "not supported", "unsupported", "unimplemented", "no such function", "unknown function",
                         "does not exist", "not recognized", "not implemented", "unknown signature", "is not a recognized", "unknown identifier",
                         "token unknown", "no syntax", "cannot be used", "unknown command", "not available", "not a valid",
                         "' expected", "unexpected token", "unknown function name", "is not supported", "unrecognized", "incorrect syntax",
                         "parse error", "parser error", "sql compilation error", "unknown statement", "parsingexception",
                         "no viable alternative", "mismatched input", "extraneous input", "unknown data type", "unsupported")

    def is_unsupported_error(self, exc: BaseException) -> bool:
        """Heuristic: does this error mean 'this SQL construct does not exist here' rather than a runtime failure?"""
        code = self.error_code(exc)
        if code and str(code) in self.UNSUPPORTED_CODES:
            return True
        msg = str(exc).lower()
        return any(h in msg for h in self.UNSUPPORTED_HINTS)

    def error_code(self, exc: BaseException) -> str | None:
        for attr in ("sqlstate", "pgcode", "code"):
            v = getattr(exc, attr, None)
            if v:
                return str(v)
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int):
            return str(args[0])
        return None


def fmt_value_for_text(v: Any, *, bool_style: str = "true") -> str:
    """Format python values for text-based bulk loaders (COPY / LOAD DATA)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        if bool_style == "int":
            return "1" if v else "0"
        return "true" if v else "false"
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, decimal.Decimal):
        return format(v, "f")
    return str(v)
