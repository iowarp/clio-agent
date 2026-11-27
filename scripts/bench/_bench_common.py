"""Shared helpers for the clio-core benchmarks (iowarp-core version comparisons).

Every benchmark runs CLIO's own ARC store path (``storage.make_arc_store``) against
a private ``CLIO_AGENT_HOME``, so the daemon, its generated ``cte.yaml`` and its
state are the ones a real CLIO home gets, and nothing touches the host's daemon.
The interpreter decides the iowarp-core version: run the same script with a venv
that has 2.4.1 and one that has 2.5.0.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def use_home(home: Path) -> None:
    """Point this process (and children it starts) at a private CLIO home."""
    home = home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["CLIO_AGENT_HOME"] = str(home)
    os.environ.pop("CLIO_USER_DIR", None)
    os.environ.setdefault("CTP_LOG_LEVEL", "error")


def core_version() -> str:
    import iowarp_core  # noqa: PLC0415

    return str(getattr(iowarp_core, "__version__", "unknown"))


def open_store(namespace: str = "bench") -> Any:
    from clio_agent.arc import storage  # noqa: PLC0415

    return storage.make_arc_store(backend="cte", namespace=namespace)


def stop_daemon(store: Any) -> Any:
    from clio_agent.arc.runtime_stop import stop_runtime_daemon  # noqa: PLC0415

    return stop_runtime_daemon(store._config_path, "error")


def daemon_pid() -> int | None:
    """The pid of this home's daemon, from CLIO's pidfile."""
    from clio_agent.arc.clio_core_config import runtime_state_dir  # noqa: PLC0415

    try:
        text = (runtime_state_dir() / "clio-runtime.pid").read_text(encoding="utf-8")
        return int(text.split()[0])
    except (OSError, ValueError, IndexError):
        return None


def percentiles(samples: list[float]) -> dict[str, float]:
    """p50/p95/p99/max/mean in milliseconds (samples are seconds)."""
    if not samples:
        return {"n": 0}
    ordered = sorted(samples)

    def pick(q: float) -> float:
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))] * 1000

    return {
        "n": len(ordered),
        "p50_ms": round(pick(0.50), 3),
        "p95_ms": round(pick(0.95), 3),
        "p99_ms": round(pick(0.99), 3),
        "max_ms": round(ordered[-1] * 1000, 3),
        "mean_ms": round(statistics.fmean(ordered) * 1000, 3),
    }


def _rss_breakdown(pid: int | None) -> dict[str, float]:
    """``RssAnon``/``RssFile``/``RssShmem`` of ``pid`` in MiB (Linux ``/proc``; else ``{}``)."""
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in ("RssAnon", "RssFile", "RssShmem"):
            out[key] = round(int(value.split()[0]) / 1024, 1)
    return out


class RssSampler:
    """Samples a process's RSS (MiB) every ``interval`` seconds in a thread."""

    def __init__(self, pid: int | None, interval: float = 1.0) -> None:
        self.pid = pid
        self.interval = interval
        self.samples: list[tuple[float, float]] = []
        #: Linux only: RssAnon / RssFile / RssShmem (MiB) per sample, else empty dicts.
        self.breakdown: list[tuple[float, dict[str, float]]] = []
        self._stop = threading.Event()
        self._start = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        import psutil  # noqa: PLC0415

        if self.pid is None:
            return
        try:
            proc = psutil.Process(self.pid)
        except psutil.Error:
            return
        while not self._stop.is_set():
            try:
                rss = proc.memory_info().rss / 2**20
            except psutil.Error:
                return
            at = round(time.monotonic() - self._start, 1)
            self.samples.append((at, round(rss, 1)))
            self.breakdown.append((at, _rss_breakdown(self.pid)))
            self._stop.wait(self.interval)

    def __enter__(self) -> RssSampler:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def summary(self) -> dict[str, Any]:
        values = [rss for _t, rss in self.samples]
        if not values:
            return {"samples": 0}
        return {
            "samples": len(values),
            "start_mib": values[0],
            "peak_mib": max(values),
            "end_mib": values[-1],
        }


def parse_size(text: str) -> int:
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    text = text.strip().lower().rstrip("b")
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)
