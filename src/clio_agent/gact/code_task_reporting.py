"""A background script's own task: ``task.progress`` and ``task.notify`` (taskable CT9).

A background ``run_code`` kernel gets a ``task`` binding, a few lines of standard-library
Python that call reserved host names (``__task_progress__``, ``__task_notify__``) through
``tools.call``; the script dispatcher answers them here. A synchronous script has no
``task``, so it gets a ``NameError``.

- ``task.progress(message, done=None, total=None)`` writes the task row's progress, with
  the script's status (elapsed time, calls, memory). It wakes nobody.
- ``task.notify(message, data=None)`` wakes the conversation that owns the task with an
  ``attention`` message attributed to the task (the same mail a settled task's notice
  uses). Wakes are bounded per owning conversation, not per task: at most one per
  ``code.notify_min_interval_s``. Notifies inside the window are queued and delivered
  together at its end, one mail per task, recorded together so they reach the
  conversation in one wake; the task's result counts the ones that waited.

Nothing queued is dropped. When a task settles, its queued notifies are sent before its
result (:func:`flush_task`); a clean shutdown sends every window's (:func:`flush_all`).
A send that fails keeps its notifies queued for the next attempt, traced as
``code_notify_send_failed``. A window nobody has used for an interval is forgotten.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from clio_agent.runtime import trace

__all__ = [
    "CODE_NOTIFY_SEND_FAILED",
    "TASK_BINDING_SOURCE",
    "binding",
    "flush_all",
    "flush_task",
    "handlers",
    "notify_interval_s",
]

#: Typed failure: a notify's mail could not be recorded; it stays queued for a retry.
CODE_NOTIFY_SEND_FAILED = "code_notify_send_failed"
#: What a failed mail write raises (Core I/O, the content store).
_SEND_ERRORS: tuple[type[BaseException], ...] = (OSError, RuntimeError, ValueError)

#: The ``task`` binding's source, bound only in a background kernel.
TASK_BINDING_SOURCE = '''"""``task``: the background task this script runs as."""


class _Task:
    """This script's task: show progress on its row, or wake its conversation. Each call
    is sent when made; await it to wait for it (a forgotten await still sends)."""

    def progress(self, message, done=None, total=None):
        """Show progress on the task row (and the script's status); wakes nobody."""
        return tools.call("__task_progress__", message=str(message), done=done, total=total)

    def notify(self, message, data=None):
        """Wake the conversation that owns this task with ``message`` (and JSON ``data``).
        At most one wake per window per conversation; notifies inside it arrive together."""
        return tools.call("__task_notify__", message=str(message), data=data)


export = _Task()
'''


def binding() -> dict[str, dict[str, str]]:
    """The ``task`` binding as a kernel bind spec."""
    digest = hashlib.sha256(TASK_BINDING_SOURCE.encode()).hexdigest()
    return {"task": {"source": TASK_BINDING_SOURCE, "digest": digest}}


def notify_interval_s() -> float:
    """Minimum seconds between two wakes of one conversation (``code.notify_min_interval_s``)."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "code.notify_min_interval_s",
        env="CLIO_CODE_NOTIFY_MIN_INTERVAL_S",
        default=30.0,
        cast=float,
    )
    return max(0.0, value)


@dataclass
class _Window:
    last: float = float("-inf")
    pending: list[dict[str, Any]] = field(default_factory=list)
    timer: threading.Timer | None = None
    #: The server whose task history records this owner's mail.
    app: Any = None


_WINDOWS: dict[str, _Window] = {}
_WINDOWS_LOCK = threading.Lock()


def _send(app: Any, owner: str, entries: list[dict[str, Any]]) -> None:
    """One ``attention`` mail per task to ``owner``, each attributed to its task by
    ``handle`` as a task's result is; notifies of one window are recorded together, so
    they reach the conversation in one wake."""
    from clio_agent.arc.task_types import MailItem  # noqa: PLC0415
    from clio_agent.gact.native_work import now  # noqa: PLC0415

    tasks: dict[str, dict[str, Any]] = {}
    for entry in entries:
        group = tasks.setdefault(
            entry["handle"], {"handle": entry["handle"], "label": entry["label"], "notifies": []}
        )
        group["notifies"].append(
            {"message": entry["message"], "data": entry["data"], "at": entry["at"]}
        )
    history = app.state.task_history
    with history.serialized():
        for handle, task in tasks.items():
            content = history.write_content(owner, {"type": "clio.code.notify", "tasks": [task]})
            history.record_mail(
                MailItem(
                    id="mail_" + uuid4().hex,
                    recipient_session=owner,
                    kind="attention",
                    content=content,
                    created_at=now(),
                    handle=handle,
                )
            )


def _deliver(app: Any, owner: str, entries: list[dict[str, Any]]) -> bool:
    """Send ``entries``; on failure put them back at the front of the owner's window
    (traced), with a timer to try again when the window ends. Whether they were sent."""
    try:
        _send(app, owner, entries)
    except _SEND_ERRORS as exc:
        trace.event(
            "CODE",
            "task notify send failed reason=%s owner=%s notifies=%d error=%s",
            CODE_NOTIFY_SEND_FAILED,
            owner,
            len(entries),
            exc,
        )
        with _WINDOWS_LOCK:
            window = _WINDOWS.setdefault(owner, _Window(app=app))
            window.pending[:0] = entries
            _arm(window, owner, notify_interval_s(), time.monotonic())
        return False
    return True


def _arm(window: _Window, owner: str, interval: float, clock: float) -> None:
    """Start the window's timer if none runs (``_WINDOWS_LOCK`` held)."""
    if window.timer is None:
        window.timer = threading.Timer(
            max(0.0, window.last + interval - clock), _flush, args=(window.app, owner)
        )
        window.timer.daemon = True
        window.timer.start()


def _flush(app: Any, owner: str) -> None:
    with _WINDOWS_LOCK:
        window = _WINDOWS.get(owner)
        if window is None:
            return
        window.timer = None
        if not window.pending:
            return
        entries, window.pending = window.pending, []
        window.last = time.monotonic()
    _deliver(app, owner, entries)


def _prune(clock: float, interval: float) -> None:
    """Forget the windows nobody used for an interval (``_WINDOWS_LOCK`` held)."""
    for owner in [
        o
        for o, w in _WINDOWS.items()
        if not w.pending and w.timer is None and clock - w.last >= interval
    ]:
        del _WINDOWS[owner]


def flush_task(app: Any, owner: str, handle: str) -> int:
    """The task ``handle`` is settling: send its queued notifies now, before its result
    (the owner's other tasks keep theirs queued). How many were sent."""
    with _WINDOWS_LOCK:
        window = _WINDOWS.get(owner)
        entries = [e for e in window.pending if e["handle"] == handle] if window else []
        if window is not None and entries:
            window.pending = [e for e in window.pending if e["handle"] != handle]
            window.last = time.monotonic()
            if not window.pending and window.timer is not None:
                window.timer.cancel()
                window.timer = None
        _prune(time.monotonic(), notify_interval_s())
    if not entries:
        return 0
    return len(entries) if _deliver(app, owner, entries) else 0


def flush_all(app: Any) -> int:
    """A clean shutdown: send every queued notify of ``app``'s windows now. How many
    could not be sent (each traced)."""
    with _WINDOWS_LOCK:
        owners = [o for o, w in _WINDOWS.items() if w.app is app and w.pending]
        batches: list[tuple[str, list[dict[str, Any]]]] = []
        for owner in owners:
            window = _WINDOWS.pop(owner)
            if window.timer is not None:
                window.timer.cancel()
            batches.append((owner, window.pending))
    unsent = 0
    for owner, entries in batches:
        if not _deliver(app, owner, entries):
            with _WINDOWS_LOCK:  # no retry after shutdown: the failure was traced
                left = _WINDOWS.pop(owner, None)
                if left is not None and left.timer is not None:
                    left.timer.cancel()
            unsent += len(entries)
    return unsent


def _notify(app: Any, owner: str, entry: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    interval = notify_interval_s()
    with _WINDOWS_LOCK:
        window = _WINDOWS.setdefault(owner, _Window(app=app))
        window.app = app
        clock = time.monotonic()
        if not window.pending and clock - window.last >= interval:
            window.last = clock
            immediate = True
        else:
            window.pending.append(entry)
            live["coalesced"] = int(live.get("coalesced") or 0) + 1
            _arm(window, owner, interval, clock)
            immediate = False
    if immediate and _deliver(app, owner, [entry]):
        return {"delivered": True}
    return {"delivered": False, "queued": True}


def handlers(
    app: Any, owner: str, handle: str, label: str, live: dict[str, Any]
) -> dict[str, Callable[[Mapping[str, Any]], Any]]:
    """The reserved calls a background script's ``task`` binding makes."""
    from clio_agent.gact.native_code import status  # noqa: PLC0415

    def progress(args: Mapping[str, Any]) -> dict[str, Any]:
        live["last_progress"] = {
            "message": str(args.get("message") or ""),
            "done": args.get("done"),
            "total": args.get("total"),
        }
        row = {**status(live, float(live["started"])), "type": "clio.code.progress"}
        report = live["progress"].report_progress(
            float(row["elapsed_s"]),
            None if args.get("total") is None else float(args["total"]),
            json.dumps(row),
        )
        asyncio.run_coroutine_threadsafe(report, live["loop"]).result()
        return {"recorded": True}

    def notify(args: Mapping[str, Any]) -> dict[str, Any]:
        from clio_agent.gact.native_work import now  # noqa: PLC0415

        data = args.get("data")
        json.dumps(data)  # JSON only; a value that is not raises in the script as ToolError
        entry = {
            "handle": handle,
            "label": label,
            "message": str(args.get("message") or ""),
            "data": data,
            "at": now(),
        }
        return _notify(app, owner, entry, live)

    return {"__task_progress__": progress, "__task_notify__": notify}
