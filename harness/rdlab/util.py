"""Shared helpers: paths, JSON encoding, checksums, percentiles."""
from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import math
import os
import statistics
import uuid
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]          # rd-databases/
STACKS = ROOT / "stacks"
RESULTS = Path(os.environ["RDLAB_RESULTS_DIR"]).resolve() if os.environ.get("RDLAB_RESULTS_DIR") else ROOT / "results"   # RDLAB_RESULTS_DIR: e.g. results-k3s for cluster runs
DATA = ROOT / "data"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def run_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def to_jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in o]
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (dt.datetime, dt.date, dt.time)):
        return o.isoformat()
    if isinstance(o, dt.timedelta):
        return o.total_seconds()
    if isinstance(o, bytes):
        return o.hex()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, BaseException):
        return f"{type(o).__name__}: {o}"[:300]
    if hasattr(o, "__dataclass_fields__"):
        return {k: to_jsonable(getattr(o, k)) for k in o.__dataclass_fields__ if k != "exc"}
    return o


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(to_jsonable(obj), indent=2, sort_keys=False, default=str))
    os.replace(tmp, path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _norm_cell(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int,)):
        return str(v)
    if isinstance(v, (float, decimal.Decimal)):
        f = float(v)
        if f == 0:
            return "0.000"
        return f"{f:.3f}"
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None).isoformat(timespec="seconds")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return str(v)


def checksum_rows(rows: Iterable[Iterable[Any]], limit: int = 200_000) -> tuple[str, int]:
    """Order-sensitive checksum of a result set (normalised so drivers that return
    Decimal vs float vs str for the same value hash identically)."""
    h = hashlib.blake2b(digest_size=12)
    n = 0
    for row in rows:
        n += 1
        if n <= limit:
            h.update("|".join(_norm_cell(c) for c in row).encode("utf-8", "replace"))
            h.update(b"\n")
    return h.hexdigest(), n


def percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * q
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def summarize(samples_ms: list[float]) -> dict[str, float | int]:
    s = sorted(samples_ms)
    if not s:
        return {"n": 0}
    return {
        "n": len(s),
        "min": s[0],
        "p50": percentile(s, 0.50),
        "p90": percentile(s, 0.90),
        "p95": percentile(s, 0.95),
        "p99": percentile(s, 0.99),
        "max": s[-1],
        "mean": statistics.fmean(s),
        "stdev": statistics.pstdev(s) if len(s) > 1 else 0.0,
    }


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def short_err(e: BaseException, limit: int = 300) -> str:
    s = f"{type(e).__name__}: {e}"
    s = " ".join(s.split())
    return s[:limit]
