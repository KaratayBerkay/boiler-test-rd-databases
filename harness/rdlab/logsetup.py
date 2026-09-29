"""Harness-side logging strategy.

Every `rdlab run` / `rdlab phase` writes three things for the run:
  results/logs/<stack>/<run_id>.log     human-readable text with timestamps, level and phase
  results/logs/<stack>/<run_id>.jsonl   one JSON object per line (ts, level, stack, phase, msg, exc) for log pipelines
  results/logs/<stack>/latest.log       symlink to the newest text log
plus the console output (unchanged look, via rich). Tracebacks go to the files, not the console.

Phases keep calling `log(msg)`; the phase name is attached through a contextvar so no signature changes.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import os
from pathlib import Path

from rich.console import Console

from .util import RESULTS

LOGGER = logging.getLogger("rdlab")
_phase: contextvars.ContextVar[str] = contextvars.ContextVar("rdlab_phase", default="-")
_stack: contextvars.ContextVar[str] = contextvars.ContextVar("rdlab_stack", default="-")
_console = Console()


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.phase = _phase.get()
        record.stack = _stack.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "stack": getattr(record, "stack", "-"),
            "phase": getattr(record, "phase", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(obj, ensure_ascii=False)


class _ConsoleHandler(logging.Handler):
    """Console output exactly as before (no timestamps, no markup interpretation)."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING and not record.getMessage().startswith("  "):
            _console.print(record.getMessage(), highlight=False, markup=False, style="yellow" if record.levelno < logging.ERROR else "red")
        else:
            _console.print(record.getMessage(), highlight=False, markup=False)


def setup_logging(stack: str, run_id: str, *, level: str | None = None) -> Path:
    """Configure the rdlab logger for one run; returns the text log path."""
    d = RESULTS / "logs" / stack
    d.mkdir(parents=True, exist_ok=True)
    txt = d / f"{run_id}.log"
    jsl = d / f"{run_id}.jsonl"
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False
    flt = _ContextFilter()
    ch = _ConsoleHandler()
    ch.setLevel(getattr(logging, (level or os.environ.get("RDLAB_LOG_LEVEL", "INFO")).upper(), logging.INFO))
    ch.addFilter(flt)
    fh = logging.FileHandler(txt, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(stack)s/%(phase)s] %(message)s"))
    fh.addFilter(flt)
    jh = logging.FileHandler(jsl, encoding="utf-8")
    jh.setLevel(logging.DEBUG)
    jh.setFormatter(_JsonFormatter())
    jh.addFilter(flt)
    for h in (ch, fh, jh):
        LOGGER.addHandler(h)
    _stack.set(stack)
    latest = d / "latest.log"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(txt.name)
    except OSError:
        pass
    LOGGER.debug("logging to %s and %s", txt, jsl)
    return txt


def set_phase(name: str) -> contextvars.Token:
    return _phase.set(name)


def reset_phase(token: contextvars.Token) -> None:
    _phase.reset(token)


def log(msg: str) -> None:
    """Drop-in for the old console-only `log()`: INFO to console + files."""
    if not LOGGER.handlers:            # e.g. scripts importing phases without setup_logging()
        _console.print(msg, highlight=False, markup=False)
        return
    LOGGER.info(msg)


def log_exception(msg: str) -> None:
    if LOGGER.handlers:
        LOGGER.error(msg, exc_info=True)
    else:
        _console.print(msg, highlight=False, markup=False)
