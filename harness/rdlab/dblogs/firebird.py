"""Firebird logging probe: the trace/audit API. A system audit trace (AuditTraceConfigFile -> conf/fbtrace.conf) logs
statements slower than 100 ms plus connections to /var/log/firebird/audit.log; an on-demand user trace session
(fbtracemgr -start with time_threshold=0) is the "log everything" switch. firebird.log holds server messages."""
from __future__ import annotations

import re
from typing import Any

from .. import dockerctl
from ..config import StackConfig
from ..engines import Engine
from .common import LogProbe

BIN = "/opt/firebird/bin"
FULL_CONF = """database = /var/lib/firebird/data/lab.fdb
{
    enabled = true
    log_statement_finish = true
    time_threshold = 0
    max_sql_length = 500
}
"""


class FbLogProbe(LogProbe):
    sink = "system audit trace (fbtrace.conf, time_threshold=100) -> /var/log/firebird/audit.log"
    audit_mechanism = "audit trace: log_connections + log_statement_prepare (every statement, DDL included)"

    def __init__(self, engine: Engine, cfg: StackConfig, *, log=print):
        super().__init__(engine, cfg, log=log)
        self.audit_log = self.lconf.get("audit_log", "/var/log/firebird/audit.log")
        self.sysdba_pw = self.lconf.get("sysdba_password", "rootpass")
        self._trace_id: str | None = None

    def _svc(self) -> str:
        return f"{BIN}/fbtracemgr -se localhost:service_mgr -user SYSDBA -password {self.sysdba_pw}"

    def settings(self) -> dict[str, Any]:
        rc, out, _ = dockerctl.exec_in(self.container, ["sh", "-c", "grep -E '^(AuditTraceConfigFile|WireCrypt|ServerMode|DefaultDbCachePages)' /opt/firebird/firebird.conf; cat /opt/firebird/fbtrace.conf 2>/dev/null | head -20"])
        return {"firebird_conf": out.strip()[:600], "audit_log": self.audit_log}

    def fast_query(self, mk: str) -> str:
        return f"SELECT 1 AS {mk} FROM rdb$database"

    def slow_query(self, mk: str) -> str:
        return f"SELECT count(*) AS {mk} FROM events e1 JOIN events e2 ON e1.customer_id = e2.customer_id WHERE e1.id < 40000 AND e2.id < 40000"

    def find_slow(self, mk: str) -> dict[str, Any]:
        """Only EXECUTE_STATEMENT_FINISH events are subject to time_threshold (prepare events log every statement)."""
        txt = self.read(self.audit_log, tail_bytes=3_000_000)
        for block in reversed(re.split(r"\n(?=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", txt)):     # one trace event per block
            if mk in block and block.split("\n", 1)[0].endswith("EXECUTE_STATEMENT_FINISH"):
                m = re.search(r"(\d+) ms", block)
                return {"found": True, "sample": block.strip()[:600], "duration_ms": int(m.group(1)) if m else None}
        return {"found": False}

    def full_logging(self, on: bool) -> str | None:
        if on:
            dockerctl.exec_in(self.container, ["sh", "-c", f"printf '%s' '{FULL_CONF}' > /tmp/full-trace.conf; nohup {self._svc()} -start -name rdlab_full -config /tmp/full-trace.conf > /var/log/firebird/full-trace.log 2>&1 &"])
            return "fbtracemgr -start user trace session with time_threshold=0 (every statement to full-trace.log)"
        rc, out, _ = dockerctl.exec_in(self.container, ["sh", "-c", f"{self._svc()} -list"])
        for m in re.finditer(r"Session ID:\s*(\d+)\s*\n\s*name:\s*rdlab_full", out):
            dockerctl.exec_in(self.container, ["sh", "-c", f"{self._svc()} -stop -id {m.group(1)}"])
        return None

    def audit_setup(self) -> str | None:
        return "always on (AuditTraceConfigFile)"

    def audit_find(self, mk: str) -> dict[str, Any]:
        txt = self.read(self.audit_log, tail_bytes=3_000_000)
        for block in reversed(re.split(r"\n(?=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", txt)):
            if f"audit_{mk}" in block:
                return {"found": True, "sample": block.strip()[:500]}
        return {"found": False}

    def log_dirs(self) -> list[tuple[str, str]]:
        return [(self.container, "/var/log/firebird")]

    def log_files(self) -> list[tuple[str, str]]:
        return [(self.container, self.audit_log), (self.container, "/var/log/firebird/full-trace.log"), (self.container, "/opt/firebird/firebird.log")]


def probe(engine: Engine, cfg: StackConfig, *, log=print) -> LogProbe:
    return FbLogProbe(engine, cfg, log=log)
