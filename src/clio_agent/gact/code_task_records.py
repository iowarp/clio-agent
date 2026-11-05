"""One counted record per window for a background script's repeated calls (taskable CT5).

A nested call is normally recorded as it happens: a ``started`` and a ``completed``
notification to the tool observer, which writes the transcript parts, the task-event
highway and the artifact index (about seven clio-core writes a call, measured). A script
that updates a surface every second for hours would leave tens of thousands of them.

In a background script's context :data:`~clio_agent.tools.tool_observation.OBSERVATION_GROUP`
is a :class:`CallGrouper`. The gate still checks every call; only the record is grouped:

- consecutive successful calls to one tool within ``code.background_record_window_s`` are
  held and recorded once, as a call carrying the last call's arguments and result plus a
  ``_clio_repeat`` entry (the count and the first and last time);
- a call that failed or asked the person a question is recorded on its own, at once;
- what is held is recorded when its window passes (at the tool's next call) and when the
  script ends.

A held call is recorded in the context of its own last call, so it carries that call's
parent (the ``run_code`` call) and session like any other nested call.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = ["CallGrouper", "record_window_s"]


def record_window_s() -> float:
    """Seconds one counted record covers (``code.background_record_window_s``)."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "code.background_record_window_s",
        env="CLIO_CODE_BACKGROUND_RECORD_WINDOW_S",
        default=60.0,
        cast=float,
    )
    return max(0.0, value)


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


@dataclass
class _Group:
    observer: Any
    name: str
    first: float
    last: float
    count: int
    args: dict[str, Any]
    result: Any
    context: contextvars.Context


@dataclass
class CallGrouper:
    """Holds a background script's repeated successful calls and records them grouped."""

    asked: Any = None  # a callable returning how many questions the task has asked so far
    window_s: float = field(default_factory=record_window_s)
    _groups: dict[str, _Group] = field(default_factory=dict)
    _starts: dict[int, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(
        self,
        observer: Any,
        name: str,
        args: Mapping[str, Any],
        phase: str,
        error: str | None,
        result: Any,
    ) -> Any:
        """Replace one observer notification of a nested call (module doc)."""
        ident = threading.get_ident()
        if phase == "started":
            with self._lock:
                self._starts[ident] = self._questions()
            return None
        if phase != "completed":
            return None  # progress of a held call: the call is not shown on its own
        with self._lock:
            asked_before = self._starts.pop(ident, self._questions())
            individual = error is not None or self._questions() != asked_before
            now = time.time()
            due: list[_Group] = []
            current = self._groups.get(name)
            if individual:
                if current is not None:
                    due.append(self._groups.pop(name))
            elif current is not None and now - current.first < self.window_s:
                current.count += 1
                current.last, current.args, current.result = now, dict(args), result
                current.context = contextvars.copy_context()
            else:
                if current is not None:
                    due.append(current)
                self._groups[name] = _Group(
                    observer, name, now, now, 1, dict(args), result, contextvars.copy_context()
                )
        for group in due:
            self._record(group)
        if individual:
            return self._call(observer, name, dict(args), error, result)
        return None

    def flush(self) -> None:
        """Record everything held (the script ended)."""
        with self._lock:
            groups, self._groups = list(self._groups.values()), {}
        for group in groups:
            self._record(group)

    def _questions(self) -> int:
        return int(self.asked()) if callable(self.asked) else 0

    @staticmethod
    def _call(
        observer: Any, name: str, args: dict[str, Any], error: str | None, result: Any
    ) -> Any:
        from clio_agent.tools.tool_observation import (  # noqa: PLC0415
            OBSERVATION_GROUP,
            notify_tool_observer,
        )

        token = OBSERVATION_GROUP.set(None)  # record this one as it is
        try:
            notify_tool_observer(observer, name, args, "started", None)
            return notify_tool_observer(observer, name, args, "completed", error, result)
        finally:
            OBSERVATION_GROUP.reset(token)

    def _record(self, group: _Group) -> None:
        args = {
            **group.args,
            "_clio_repeat": {
                "count": group.count,
                "first_at": _iso(group.first),
                "last_at": _iso(group.last),
            },
        }
        group.context.run(self._call, group.observer, group.name, args, None, group.result)
