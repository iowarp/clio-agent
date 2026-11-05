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
  together at its end as one message grouped by task, never dropped; the task's result
  counts the ones that waited.
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

__all__ = ["TASK_BINDING_SOURCE", "binding", "handlers", "notify_interval_s"]

#: The ``task`` binding's source, bound only in a background kernel.
TASK_BINDING_SOURCE = '''"""``task``: the background task this script runs as."""

import asyncio


class _Task:
    """This script's task: show progress on its row, or wake its conversation. Each call
    is sent when made; await it to wait for it (a forgotten await still sends)."""

    def progress(self, message, done=None, total=None):
        """Show progress on the task row (and the script's status); wakes nobody."""
        return asyncio.ensure_future(
            tools.call("__task_progress__", message=str(message), done=done, total=total)
        )

    def notify(self, message, data=None):
        """Wake the conversation that owns this task with ``message`` (and JSON ``data``).
        At most one wake per window per conversation; notifies inside it arrive together."""
        return asyncio.ensure_future(
            tools.call("__task_notify__", message=str(message), data=data)
        )


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


_WINDOWS: dict[str, _Window] = {}
_WINDOWS_LOCK = threading.Lock()


def _send(app: Any, owner: str, entries: list[dict[str, Any]]) -> None:
    """One ``attention`` message to ``owner`` carrying ``entries``, grouped by task."""
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
        content = history.write_content(
            owner, {"type": "clio.code.notify", "tasks": list(tasks.values())}
        )
        history.record_mail(
            MailItem(
                id="mail_" + uuid4().hex,
                recipient_session=owner,
                kind="attention",
                content=content,
                created_at=now(),
                handle=next(iter(tasks)) if len(tasks) == 1 else None,
            )
        )


def _flush(app: Any, owner: str) -> None:
    with _WINDOWS_LOCK:
        window = _WINDOWS.get(owner)
        if window is None or not window.pending:
            return
        entries, window.pending, window.timer = window.pending, [], None
        window.last = time.monotonic()
    _send(app, owner, entries)


def _notify(app: Any, owner: str, entry: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    interval = notify_interval_s()
    with _WINDOWS_LOCK:
        window = _WINDOWS.setdefault(owner, _Window())
        clock = time.monotonic()
        if not window.pending and clock - window.last >= interval:
            window.last = clock
            immediate = True
        else:
            window.pending.append(entry)
            live["coalesced"] = int(live.get("coalesced") or 0) + 1
            if window.timer is None:
                window.timer = threading.Timer(
                    max(0.0, window.last + interval - clock), _flush, args=(app, owner)
                )
                window.timer.daemon = True
                window.timer.start()
            immediate = False
    if immediate:
        _send(app, owner, [entry])
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
