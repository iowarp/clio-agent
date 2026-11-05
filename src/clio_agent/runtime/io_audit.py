"""Timed clio-core and I/O rows in the stream audit, to find where a run waits.

Three choke points carry every store interaction:

* ``store.rpc`` -- one native store call (``guarded_store_rpc``): the operation, the
  record kind when known, its duration and the calling thread;
* ``core_io.job`` -- one job on the history writer (``CoreIO``, one thread for every
  session): how long it waited in the queue and how long it ran;
* ``highway.flush`` -- one wait at the highway barrier (before a model request, at the
  end of a turn) for the session's queued records.

The rows go to the stream audit only when it is on (``debug.stream_audit_log``); the
evaluation runner slices them per run. Writing a row costs one append, so this stays
off in normal use.
"""

from __future__ import annotations

import threading
import time
from typing import Any

__all__ = ["barrier", "enabled", "job", "rpc"]

#: Seconds an enabled/disabled answer is reused (the setting is read from config).
_RECHECK_S = 1.0
_state: dict[str, Any] = {"at": -_RECHECK_S, "on": False}


def enabled() -> bool:
    """Whether the stream audit is on (re-read at most once a second)."""
    now = time.monotonic()
    if now - _state["at"] >= _RECHECK_S:
        from clio_agent.runtime.stream_audit import stream_audit_enabled  # noqa: PLC0415

        _state["on"], _state["at"] = stream_audit_enabled(), now
    return bool(_state["on"])


def _emit(stage: str, **fields: Any) -> None:
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    stream_audit(stage, thread=threading.current_thread().name, **fields)


#: Modules a store call passes through on its way to the store; ``caller`` skips them
#: so it names the CLIO code that asked for the write.
_PLUMBING = ("clio_agent/arc/", "clio_agent/runtime/", "clio_agent/gact/task_history")


def _caller() -> str:
    """The first CLIO frames outside the store plumbing, innermost first (audit only)."""
    import sys  # noqa: PLC0415

    frame: Any = sys._getframe(2)
    names: list[str] = []
    while frame is not None and len(names) < 3:
        path = frame.f_code.co_filename.replace("\\", "/")
        if "clio_agent/" in path and not any(p in path for p in _PLUMBING):
            module = path.rsplit("clio_agent/", 1)[1].removesuffix(".py").replace("/", ".")
            names.append(f"{module}.{frame.f_code.co_name}")
        frame = frame.f_back
    return " < ".join(names)


def rpc(op: str, kind: str, seconds: float) -> None:
    """One store call took ``seconds``; ``caller`` names the CLIO code that made it."""
    if enabled():
        _emit("store.rpc", op=op, kind=kind, ms=round(seconds * 1e3, 3), caller=_caller())


def job(label: str, wait_s: float, run_s: float) -> None:
    """One history-writer job waited ``wait_s`` in the queue and ran ``run_s``."""
    if enabled():
        _emit(
            "core_io.job",
            label=label,
            wait_ms=round(wait_s * 1e3, 3),
            run_ms=round(run_s * 1e3, 3),
        )


def barrier(session_id: str, seconds: float) -> None:
    """The session waited ``seconds`` at the highway barrier."""
    if enabled():
        _emit("highway.flush", session_id=session_id, ms=round(seconds * 1e3, 3))
