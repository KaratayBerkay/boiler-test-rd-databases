"""Latency measurement with monotonic clocks, warmup and percentiles."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .util import checksum_rows, short_err, summarize


@dataclass
class Timing:
    name: str
    ok: bool = True
    error: str | None = None
    n: int = 0
    stats: dict[str, float | int] = field(default_factory=dict)   # ms
    first_ms: float | None = None          # first (cold-ish) execution, not in stats
    rows: int | None = None
    checksum: str | None = None
    samples_ms: list[float] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    exc: BaseException | None = field(default=None, repr=False, compare=False)

    @property
    def p50(self) -> float | None:
        return self.stats.get("p50") if self.stats else None


def measure(name: str, fn: Callable[[], Any], *, warmup: int = 2, iters: int = 10,
            max_seconds: float = 30.0, min_iters: int = 3, keep_samples: bool = True,
            row_fn: Callable[[Any], list] | None = None) -> Timing:
    """Run fn() (which returns a list of rows) repeatedly and time it.

    - `warmup` runs are executed and discarded (the very first is recorded as first_ms).
    - Then up to `iters` timed runs, stopping early once `max_seconds` of measured time
      have elapsed (but never fewer than `min_iters`).
    - Rows of the first timed run are checksummed so repeatability can be verified.
    """
    t = Timing(name=name)
    try:
        t0 = time.perf_counter_ns()
        rows = fn()
        t.first_ms = (time.perf_counter_ns() - t0) / 1e6
        if row_fn:
            rows = row_fn(rows)
        for _ in range(max(0, warmup - 1)):
            fn()
        samples: list[float] = []
        budget_start = time.perf_counter()
        for i in range(iters):
            t0 = time.perf_counter_ns()
            r = fn()
            samples.append((time.perf_counter_ns() - t0) / 1e6)
            if i == 0:
                rr = row_fn(r) if row_fn else r
                if rr is not None:
                    try:
                        cs, n = checksum_rows(rr)
                        t.checksum, t.rows = cs, n
                    except TypeError:
                        t.rows = None
            if (time.perf_counter() - budget_start) > max_seconds and len(samples) >= min_iters:
                break
        t.n = len(samples)
        t.stats = summarize(samples)
        if keep_samples:
            t.samples_ms = [round(s, 4) for s in samples]
    except Exception as e:  # noqa: BLE001 - we want to record every failure kind
        t.ok = False
        t.error = short_err(e)
        t.exc = e
    return t


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.perf_counter_ns()

    def ms(self) -> float:
        return (time.perf_counter_ns() - self.t0) / 1e6
