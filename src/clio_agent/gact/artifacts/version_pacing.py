"""One version per interval for a file a background script keeps registering.

A script that watches something writes its status file and registers it with
``create_artifact(path=...)``, so a view bound to ``artifact://<workspace>/<name>@latest``
follows it (taskable run_code plan, Part 4; CT25 (1) and (2)). Every version costs
several store writes and rewrites the artifact's record, so a script registering every
second would grow a record per second. From a background script, a registration of a
name that got a version less than ``artifacts.version_interval_s`` ago is deferred: the
call is answered at once, and one version is recorded when the interval ends, with the
file's bytes at that moment (the latest wins; nothing is dropped). A model's own call,
and a synchronous script, register at once as before. State that changes every second
belongs in a stream, not in versions of a file.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: The deferred registration of a name failed when its interval ended (logged and traced).
VERSION_DEFERRED_FAILED = "artifact_version_deferred_failed"


def version_interval_s() -> float:
    """Seconds between two versions of one file from background scripts."""

    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "artifacts.version_interval_s",
        env="CLIO_ARTIFACTS_VERSION_INTERVAL_S",
        default=5.0,
        cast=float,
    )
    return max(0.0, value)


@dataclass
class _Name:
    """One name's pacing: when it last got a version, and the registration waiting."""

    last: float = 0.0
    pending: Callable[[], Any] | None = None
    timer: threading.Timer | None = None
    #: When the waiting registration is recorded (wall clock, for the result).
    due: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


_NAMES: dict[tuple[str, str, str], _Name] = {}
_NAMES_LOCK = threading.Lock()


def _state(key: tuple[str, str, str]) -> _Name:
    with _NAMES_LOCK:
        return _NAMES.setdefault(key, _Name())


def _run_pending(key: tuple[str, str, str]) -> None:
    state = _state(key)
    with state.lock:
        register, state.pending, state.timer = state.pending, None, None
        state.last = time.monotonic()
    if register is None:
        return
    try:
        register()
    except (OSError, ValueError, RuntimeError) as exc:  # the version's write failed: typed
        logger.warning(
            "deferred artifact version failed reason=%s session=%s name=%s error=%r",
            VERSION_DEFERRED_FAILED,
            key[0],
            key[2],
            exc,
        )


def pace(
    session_id: str, workspace_id: str, name: str, register: Callable[[], Any]
) -> dict[str, Any] | None:
    """Register now (``None``: the caller registers), or defer to the interval's end.

    ``register`` records the version from the file as it is when called; it runs in a
    copy of the caller's context, so it registers as the same session and script.
    Only a call from a background script is paced.
    """

    from clio_agent.gact.code_task_context import active_background_task  # noqa: PLC0415

    interval = version_interval_s()
    if active_background_task() is None or interval <= 0:
        return None
    key = (session_id, workspace_id, name)
    state = _state(key)
    now = time.monotonic()
    with state.lock:
        wait = state.last + interval - now
        if wait <= 0 and state.timer is None:
            state.last = now
            return None
        context = contextvars.copy_context()
        state.pending = lambda: context.run(register)
        if state.timer is None:
            state.timer = threading.Timer(max(wait, 0.0), _run_pending, args=(key,))
            state.timer.daemon = True
            state.timer.start()
            state.due = time.time() + max(wait, 0.0)
        due = state.due
    return {
        "name": name,
        "accepted": True,
        "deferred": True,
        "version_at": datetime.fromtimestamp(due, timezone.utc).isoformat(),
        "note": (
            f"A version of {name} was recorded less than {interval:g} s ago; the next one is "
            "recorded when the interval ends, with the file as it is then. A view bound to "
            "this file at @latest shows it then."
        ),
    }


def flush_pending() -> int:
    """Record every deferred version now (a clean shutdown); how many were waiting."""

    with _NAMES_LOCK:
        keys = [key for key, state in _NAMES.items() if state.timer is not None]
    for key in keys:
        state = _state(key)
        with state.lock:
            if state.timer is not None:
                state.timer.cancel()
        _run_pending(key)
    return len(keys)


__all__ = ["VERSION_DEFERRED_FAILED", "flush_pending", "pace", "version_interval_s"]
