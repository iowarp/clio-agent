"""A foreground script that can move to the background while it runs.

A synchronous ``run_code`` holds the turn until its script ends. A long one left the agent
unable to look at it, change course or do anything else: one Tahoe run sat 38 minutes on
a script reading tiles one by one, another 23 minutes on a raster read that should have
taken seconds. Here the script runs on a thread of its own while the call waits, and it
can be handed, still running, to a background task:

- by time: once it has run ``code.foreground_background_after_s`` (default 180 s; 0 never);
- by the person: ``request_background(session_id)`` (the session's running scripts),
  which the API exposes to a "send to background" control.

The hand-off moves nothing: the running kernel, its variables and its dispatcher become
the task's (:func:`clio_agent.gact.native_code` adopts the run), and the scope's next
script starts a new kernel (``replaced_backgrounded``). The call returns at once with
the task's handle and what the script had done, so the agent can wait on it, watch it,
cancel it or work on. Nothing is decided for the agent and nothing is stopped: the
script goes on exactly as it was.
"""

from __future__ import annotations

import contextvars
import threading
import time
from typing import Any, Callable

from clio_agent.runtime import trace

__all__ = [
    "BACKGROUNDED",
    "ForegroundRun",
    "background_after_s",
    "moved_outcome",
    "request_background",
    "running_scripts",
]

#: The outcome status of a script handed to a background task while it ran.
BACKGROUNDED = "backgrounded"

_RUNNING: dict[str, set["ForegroundRun"]] = {}
_RUNNING_LOCK = threading.Lock()


def background_after_s() -> float:
    """Seconds a foreground script runs before it moves to a background task (0: never)."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "code.foreground_background_after_s",
        env="CLIO_CODE_FOREGROUND_BACKGROUND_AFTER_S",
        default=180.0,
        cast=conf.as_float,
    )
    return max(0.0, value)


class ForegroundRun:
    """One synchronous script, run on its own thread so the call can stop waiting."""

    def __init__(self, session_id: str, label: str) -> None:
        self.session_id, self.label = session_id, label
        self.stop = threading.Event()  # the script's loop stops its kernel when set
        self.move = threading.Event()  # hand the running script to a background task
        self.moved_by = ""  # "time" or "person"
        self.live: dict[str, Any] = {}  # dispatcher (calls so far) and kernel
        self.started = time.monotonic()
        self._done = threading.Event()
        self._outcome: dict[str, Any] | None = None
        self._error: BaseException | None = None

    # The script's thread -------------------------------------------------------------

    def start(self, execute: Callable[..., dict[str, Any]], *args: Any) -> None:
        """Run ``execute(*args, cancelled, live)`` on its own thread, in this context."""
        context = contextvars.copy_context()

        def target() -> None:
            try:
                self._outcome = context.run(execute, *args, self.stop.is_set, self.live)
            except BaseException as exc:  # noqa: BLE001 - forwards: re-raised by the waiter
                self._error = exc
            finally:
                self._done.set()

        with _RUNNING_LOCK:
            _RUNNING.setdefault(self.session_id, set()).add(self)
        threading.Thread(target=target, name="code-foreground", daemon=True).start()

    def outcome(self) -> dict[str, Any]:
        """The script's outcome once it ended (raises what the script's runner raised)."""
        self._done.wait()
        self._release()
        if self._error is not None:
            raise self._error
        assert self._outcome is not None
        return self._outcome

    def done(self) -> bool:
        return self._done.is_set()

    def _release(self) -> None:
        with _RUNNING_LOCK:
            runs = _RUNNING.get(self.session_id)
            if runs is not None:
                runs.discard(self)
                if not runs:
                    _RUNNING.pop(self.session_id, None)

    # The call waiting on it ----------------------------------------------------------

    def wait(self, cancelled: Callable[[], bool], poll: float, after_s: float) -> str:
        """Wait until the script ends (``"done"``), the turn is cancelled (``"cancelled"``,
        the script stopped) or it should move to the background (``"move"``)."""
        while not self._done.wait(poll):
            if cancelled():
                self.stop.set()
                self._done.wait()
                return "cancelled"
            if not self.move.is_set() and after_s and time.monotonic() - self.started >= after_s:
                self.moved_by = self.moved_by or "time"
                self.move.set()
            if self.move.is_set():
                return "move"
        return "done"

    def adopt(self, stop: threading.Event) -> dict[str, Any]:
        """The background task's side: wait for the script, its ``stop`` now the task's
        cancel. Returns the outcome (its status is the script's own)."""
        self._release()  # a background task's script, no longer this session's foreground
        while not self._done.wait(0.5):
            if stop.is_set():
                self.stop.set()
        if self._error is not None:
            raise self._error
        assert self._outcome is not None
        return self._outcome

    def elapsed_s(self) -> float:
        return round(time.monotonic() - self.started, 1)

    def calls(self) -> int:
        return len(getattr(self.live.get("dispatcher"), "records", ()) or ())


def moved_outcome(run: ForegroundRun, receipt: dict[str, Any], code: str) -> dict[str, Any]:
    """The call's outcome when its script moved to a background task, and what the
    model reads: where the script is, what it had done, and what changed for it."""
    handle = str(receipt.get("handle") or "")
    calls, elapsed = run.calls(), run.elapsed_s()
    why = (
        "the person asked"
        if run.moved_by == "person"
        else f"it ran past {background_after_s():g} s"
    )
    moved = (
        f"Still running after {elapsed:g} s ({calls} tool call{'s' if calls != 1 else ''} so "
        f"far); moved to the background because {why}."
    )
    text = (
        f"{moved} It is task {handle}{f' ({run.label})' if run.label else ''} and keeps "
        "running as it was, with its variables, in its own kernel; its output and result "
        "arrive when it settles. wait_tasks, observe_tasks, get_task_result and cancel_tasks "
        "take its handle. Your next run_code starts a new Python session: variables defined "
        "before this script stay with the task's session, not the new one."
    )
    trace.event(
        "CODE",
        "foreground_backgrounded by=%s elapsed_s=%s calls=%d task=%s",
        run.moved_by,
        elapsed,
        calls,
        handle,
    )
    return {
        **receipt,
        "status": BACKGROUNDED,
        "code": code,
        "calls": calls,
        "elapsed_s": elapsed,
        "moved_by": run.moved_by,
        "moved": moved,
        "text": text,
    }


def running_scripts(session_id: str) -> list[dict[str, Any]]:
    """The session's foreground scripts now running: label, elapsed seconds, calls."""
    with _RUNNING_LOCK:
        runs = list(_RUNNING.get(session_id, ()))
    return [{"label": r.label, "elapsed_s": r.elapsed_s(), "calls": r.calls()} for r in runs]


def request_background(session_id: str) -> int:
    """The person asks for the session's running foreground scripts to move to the
    background; the number asked to move."""
    with _RUNNING_LOCK:
        runs = [r for r in _RUNNING.get(session_id, ()) if not r.move.is_set()]
    for run in runs:
        run.moved_by = "person"
        run.move.set()
    trace.event(
        "CODE", "foreground_background_requested session=%s scripts=%d", session_id, len(runs)
    )
    return len(runs)
