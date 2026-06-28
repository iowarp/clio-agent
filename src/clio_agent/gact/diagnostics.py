"""Process diagnostics for the GACT server (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module owns
the cohesive cluster that wires the ``SIGUSR1`` self-diagnostic: by default a
``faulthandler`` thread-traceback dump (wedge debugging), or -- when
``debug.memprof`` is on -- a ``tracemalloc`` heap snapshot (heap attribution).
It is the single source of truth for:

* :func:`_memprof_dump` -- the SIGUSR1 handler that dumps a tracemalloc snapshot
  (top allocations + growth-since-previous + a gc type histogram).
* :func:`_install_sigusr1_diagnostic` -- the installer that picks the handler
  based on the ``debug.memprof`` config knob.

``_install_sigusr1_diagnostic()`` is invoked once at GACT app import (from
``clio_agent.gact.app``), not here, so importing this module is side-effect-free.

The module imports only stdlib plus :mod:`clio_agent.conf` (lazily, to keep
config from ever blocking server import). It never imports
:mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import faulthandler as _faulthandler
import os
import signal as _signal
import sys
from typing import Any

# Mutable handler state: the previous tracemalloc snapshot (for growth diffs) and
# a monotonically increasing snapshot counter (so successive SIGUSR1 dumps land in
# distinct numbered output files). Owned here so the handler is the single writer.
_MEMPROF_STATE: dict[str, Any] = {"prev": None, "n": 0}


def _memprof_dump(signum: Any, frame: Any) -> None:
    """SIGUSR1 handler (when ``debug.memprof`` is on): dump a tracemalloc
    snapshot of the top allocations + a gc type histogram, for heap attribution.

    Writes to ``CLIO_DEBUG_MEMPROF_OUT.<n>.txt`` if set (numbered so successive
    SIGUSR1s can be diffed), else to stderr. Best-effort; never raises.
    """
    try:
        import collections
        import gc
        import tracemalloc

        snap = tracemalloc.take_snapshot()
        cur, peak = tracemalloc.get_traced_memory()
        try:
            with open(f"/proc/{os.getpid()}/status") as _f:
                rss = next(
                    (int(line.split()[1]) / 1024 for line in _f if line.startswith("VmRSS")),
                    -1.0,
                )
        except OSError:
            rss = -1.0
        lines = [
            f"pid={os.getpid()} RSS={rss:.1f}MB "
            f"traced_current={cur / 1e6:.1f}MB traced_peak={peak / 1e6:.1f}MB",
            "=== top 30 allocations by line ===",
        ]
        for stat in snap.statistics("lineno")[:30]:
            fr = stat.traceback[0]
            lines.append(
                f"{stat.size / 1e6:8.2f}MB count={stat.count:<8} {fr.filename}:{fr.lineno}"
            )
        prev = _MEMPROF_STATE["prev"]
        if prev is not None:
            lines.append("=== top 25 GROWTH since previous snapshot ===")
            for diff in snap.compare_to(prev, "lineno")[:25]:
                fr = diff.traceback[0]
                lines.append(
                    f"{diff.size_diff / 1e6:+8.2f}MB (count {diff.count_diff:+d}) "
                    f"{fr.filename}:{fr.lineno}"
                )
        lines.append("=== gc object type histogram (top 25) ===")
        hist = collections.Counter(type(o).__name__ for o in gc.get_objects())
        lines.extend(f"{count:>9}  {name}" for name, count in hist.most_common(25))
        _MEMPROF_STATE["prev"] = snap
        _MEMPROF_STATE["n"] += 1

        report = "\n".join(lines) + "\n"
        out = os.environ.get("CLIO_DEBUG_MEMPROF_OUT", "").strip()
        if out:
            with open(f"{out}.{_MEMPROF_STATE['n']}.txt", "w", encoding="utf-8") as fh:
                fh.write(report)
        else:
            sys.stderr.write("\n=== CLIO MEMPROF SNAPSHOT ===\n" + report)
            sys.stderr.flush()
    except Exception:  # noqa: BLE001 - diagnostics must never crash the server
        pass


def _install_sigusr1_diagnostic() -> None:
    """Install the SIGUSR1 diagnostic handler.

    Default: ``faulthandler`` thread-traceback dump (wedge debugging). When
    ``debug.memprof`` (env ``CLIO_DEBUG_MEMPROF``) is on, SIGUSR1 instead dumps a
    tracemalloc heap snapshot (heap attribution) — the in-core replacement for
    ad-hoc sitecustomize profiling, and it does not fight faulthandler.

    SIGUSR1 and ``faulthandler.register`` are POSIX-only — on Windows neither
    exists and merely referencing them raises ``AttributeError`` (not the
    ``ValueError``/``OSError`` guarded below), which would crash server import.
    This diagnostic is therefore a no-op on platforms without SIGUSR1.
    """
    if not hasattr(_signal, "SIGUSR1"):
        return
    memprof = False
    try:
        from clio_agent import conf

        memprof = conf.resolve(
            "debug.memprof", env="CLIO_DEBUG_MEMPROF", default=False, cast=conf.as_bool
        )
    except Exception:  # noqa: BLE001 - config must never block server import
        memprof = False
    if memprof:
        try:
            import tracemalloc

            frames = int(os.environ.get("CLIO_DEBUG_MEMPROF_FRAMES", "20") or "20")
            tracemalloc.start(frames)
        except Exception:  # noqa: BLE001
            pass
        try:
            _signal.signal(_signal.SIGUSR1, _memprof_dump)
        except (ValueError, OSError):
            pass
    else:
        try:
            register = getattr(_faulthandler, "register", None)
            if register is not None:
                register(_signal.SIGUSR1, all_threads=True)
        except (ValueError, OSError):
            pass
