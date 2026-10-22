"""Hook transport adapters behind ONE interface (P2.2).

The dispatcher owns matching/ordering/merging/timeout/failure-posture; an
*adapter* is the thin thing that actually invokes one hook and returns its
:class:`~clio_agent.gact.hooks.wire.HookDecision` (or raises
:class:`~clio_agent.gact.hooks.wire.HookInfraError` on an infra failure). This
slice implements the ``command`` (subprocess) adapter fully — the industry
exit-0/exit-2 wire — and leaves ``http``/``prompt`` as clean seams behind the
same interface (implemented in P2.2's follow-ons, not over-built now).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from clio_agent.gact.hooks.config import HookEntry
from clio_agent.gact.hooks.wire import (
    HookCancelled,
    HookDecision,
    HookEnvelope,
    HookInfraError,
    extract_json_object,
    parse_hook_output,
    record_hook_reason,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from clio_agent.runtime.task_process_tree import TaskProcessTree


class HookAdapter(ABC):
    """One transport for invoking a hook. Returns a decision or raises infra error."""

    @abstractmethod
    def invoke(
        self,
        entry: HookEntry,
        envelope: HookEnvelope,
        *,
        cancel_event: "threading.Event | None" = None,
    ) -> HookDecision:
        """Invoke ``entry`` for ``envelope``.

        Returns the parsed :class:`HookDecision` on success (exit 0 / exit 2).
        Raises :class:`HookInfraError` on any infrastructure failure (timeout,
        crash, missing binary) — DISTINCT from a ``deny`` decision. ``cancel_event``
        (L1 slice), when given, is polled while waiting on the hook; a transport
        that supports killing in-flight work raises :class:`HookCancelled` instead
        of resolving a decision when it trips.
        """


class SubprocessAdapter(HookAdapter):
    """The industry exit-0/exit-2 wire (hooks-research §5.3).

    Spawn the hook process with NO controlling terminal, write the JSON envelope
    to its stdin, read stdout/stderr. Exit 0 => parse stdout (empty => allow).
    Exit 2 => deny, stderr becomes the model-facing reason. Any other exit => a
    non-blocking :class:`HookInfraError` the dispatcher resolves via ``failClosed``.
    """

    def invoke(
        self,
        entry: HookEntry,
        envelope: HookEnvelope,
        *,
        cancel_event: "threading.Event | None" = None,
    ) -> HookDecision:
        import json  # noqa: PLC0415 - local; keeps module import graph lean

        from clio_agent.runtime.task_process_tree import (  # noqa: PLC0415
            TaskProcessTree,
            task_spawn_options,
        )

        argv = [entry.run.command, *entry.run.args]
        # ``default=str`` keeps a non-JSON value in an observation payload from
        # exploding the envelope (a SemanticEvent payload can carry arbitrary objects).
        stdin_blob = json.dumps(self.stdin_body(entry, envelope), default=str)
        env = self.environment(entry, envelope)
        # A manual Popen (rather than subprocess.run) so the timeout path below can
        # kill the whole process GROUP, not just the direct child PID — start_new_session
        # (POSIX) makes the child a group leader, so a hook that forks would otherwise
        # leave orphans running past the timeout kill (a resource leak, worst on CI).
        spawn_options: dict[str, Any] = {
            **task_spawn_options(_no_tty_kwargs()),
            **({"env": env} if env is not None else {}),
        }
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is exec-form (never shell); command is operator-declared
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **spawn_options,
            )
        except FileNotFoundError as exc:
            record_hook_reason(
                "hook_missing_binary",
                hook_id=entry.id,
                event=envelope.hook_event_name,
                command=entry.run.command,
            )
            raise HookInfraError(
                "hook_missing_binary",
                f"hook {entry.id!r} binary not found: {entry.run.command!r}",
                hook_id=entry.id,
            ) from exc
        except OSError as exc:
            # Any other refusal to start the process (a transient Windows ERROR_NOT_ENOUGH_
            # MEMORY / paging-file / handle exhaustion under load): typed and recorded, so
            # the hook visibly did not run instead of the error escaping the dispatch.
            record_hook_reason(
                "hook_spawn_failed",
                hook_id=entry.id,
                event=envelope.hook_event_name,
                command=entry.run.command,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise HookInfraError(
                "hook_spawn_failed",
                f"hook {entry.id!r} could not be started: {exc}",
                hook_id=entry.id,
            ) from exc

        try:
            tree = TaskProcessTree(proc.pid)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        timeout_s = entry.timeout_s if entry.timeout_s > 0 else None
        deadline = time.monotonic() + timeout_s if timeout_s is not None else None
        try:
            if cancel_event is None:
                stdout, stderr = proc.communicate(input=stdin_blob, timeout=timeout_s)
            else:
                stdout, stderr = _communicate_cancellable(
                    proc, stdin_blob, timeout_s, cancel_event, tree
                )
            while tree.active():
                if cancel_event is not None and cancel_event.is_set():
                    raise _HookCancelledWait()
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, entry.timeout_s)
                time.sleep(0.05)
        except subprocess.TimeoutExpired:
            _retire_hook_process(proc, tree)
            record_hook_reason(
                "hook_timeout",
                hook_id=entry.id,
                event=envelope.hook_event_name,
                timeout_ms=entry.timeout_ms,
            )
            raise HookInfraError(
                "hook_timeout",
                f"hook {entry.id!r} exceeded its {entry.timeout_ms}ms timeout",
                hook_id=entry.id,
            ) from None
        except _HookCancelledWait:
            # Cancel contract (L1): kill the WHOLE process tree, never resolve a
            # decision for a cancelled hook (HookCancelled propagates uncaught
            # through HookDispatcher.dispatch, unlike HookInfraError).
            _retire_hook_process(proc, tree)
            record_hook_reason("hook_cancelled", hook_id=entry.id, event=envelope.hook_event_name)
            raise HookCancelled(entry.id, invocation_started=True, cleanup_confirmed=True) from None
        except BaseException:
            # Any other failure (including KeyboardInterrupt) while waiting: clean up
            # the whole process group too, then re-raise unchanged.
            _retire_hook_process(proc, tree)
            raise
        finally:
            tree.close()

        returncode = proc.returncode
        if returncode == 0:
            obj = extract_json_object(
                stdout or "", hook_id=entry.id, event=envelope.hook_event_name
            )
            if obj is None and (stdout or "").strip():
                # exit 0 but stdout carried no JSON object: proceed (exit 0 = allow),
                # but record a diagnosable reason — never a silent fail-open (C7/C8).
                record_hook_reason(
                    "hook_unparseable_stdout",
                    hook_id=entry.id,
                    event=envelope.hook_event_name,
                )
                return HookDecision(decision="allow", hook_id=entry.id)
            return self.decision(obj, entry, envelope)
        if returncode == 2:
            reason = (stderr or "").strip() or f"hook {entry.id!r} denied the call"
            return HookDecision(decision="deny", reason=reason, hook_id=entry.id)
        record_hook_reason(
            "hook_crashed",
            hook_id=entry.id,
            event=envelope.hook_event_name,
            exit_code=returncode,
            stderr=(stderr or "").strip()[:500],
        )
        raise HookInfraError(
            "hook_crashed",
            f"hook {entry.id!r} exited {returncode}: {(stderr or '').strip()[:200]}",
            hook_id=entry.id,
        )

    def stdin_body(self, entry: HookEntry, envelope: HookEnvelope) -> dict[str, Any]:
        """The JSON the hook reads on stdin."""
        return envelope.to_json(hook_id=entry.id)

    def environment(self, entry: HookEntry, envelope: HookEnvelope) -> dict[str, str] | None:
        """The hook process environment (``None`` inherits the server's)."""
        del entry, envelope
        return None

    def decision(
        self, obj: dict[str, Any] | None, entry: HookEntry, envelope: HookEnvelope
    ) -> HookDecision:
        """The decision a hook that exited 0 printed."""
        return parse_hook_output(obj, hook_id=entry.id, event=envelope.hook_event_name)


class _HookCancelledWait(Exception):
    """Internal signal: the ``cancel_event`` tripped while waiting on the hook.

    Never escapes :func:`_communicate_cancellable`'s caller — ``SubprocessAdapter.
    invoke`` catches it, kills the process tree, and raises the public
    :class:`~clio_agent.gact.hooks.wire.HookCancelled` instead.
    """


def _communicate_cancellable(
    proc: "subprocess.Popen[str]",
    stdin_blob: str,
    timeout_s: float | None,
    cancel_event: threading.Event,
    tree: TaskProcessTree,
    poll_s: float = 0.05,
) -> tuple[str, str]:
    """Feed ``stdin_blob`` and wait for the hook to exit, honoring BOTH the
    configured timeout and a cooperative cancel token.

    ``Popen.communicate`` blocks with no way to interrupt it early, so the actual
    communicate call runs on a background thread while THIS thread polls the
    cancel token on a short cadence -- the same poll-don't-disturb shape
    ``turn_watchdog.await_turn_work`` uses for the no-progress watchdog: a hook
    that finishes on its own is never disturbed. A cancel firing first raises
    :class:`_HookCancelledWait`; the timeout firing first raises the stdlib
    ``subprocess.TimeoutExpired`` unchanged, so the caller's existing timeout
    handling is untouched either way.
    """

    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["stdout"], outcome["stderr"] = proc.communicate(input=stdin_blob)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the waiting thread below
            outcome["exc"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout_s if timeout_s is not None else None
    try:
        while worker.is_alive():
            if cancel_event.is_set():
                raise _HookCancelledWait()
            if deadline is not None and time.monotonic() >= deadline:
                assert timeout_s is not None  # noqa: S101 - deadline is only set from timeout_s
                raise subprocess.TimeoutExpired(proc.args, timeout_s)
            worker.join(timeout=poll_s)
    except BaseException:
        # Retire the existing communicator before the caller drains the pipes.
        # Concurrent communicate() calls can otherwise race on the same streams.
        tree.terminate()
        worker.join()
        raise
    if "exc" in outcome:
        raise outcome["exc"]
    return outcome.get("stdout", ""), outcome.get("stderr", "")


def _no_tty_kwargs() -> dict[str, Any]:
    """Return platform kwargs that detach the hook from any controlling terminal.

    Hooks must never own the UI's TTY (hooks-research §5.6): on POSIX start a new
    session; on Windows spawn with no console window. stdin/stdout/stderr are
    piped by ``capture_output``/``input`` so there is no inherited console anyway.
    """

    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": creationflags}
    return {"start_new_session": True}


def _retire_hook_process(proc: subprocess.Popen[str], tree: TaskProcessTree) -> None:
    """Settle the original owned tree and its pipe readers before confirming cancellation."""
    tree.terminate()
    proc.communicate()
    while tree.active():
        time.sleep(0.05)


class _NotImplementedAdapter(HookAdapter):
    """A clean seam for a transport declared but not yet implemented in this slice."""

    def __init__(self, transport: str) -> None:
        self._transport = transport

    def invoke(
        self,
        entry: HookEntry,
        envelope: HookEnvelope,
        *,
        cancel_event: "threading.Event | None" = None,
    ) -> HookDecision:
        del cancel_event
        raise HookInfraError(
            "hook_crashed",
            f"hook {entry.id!r}: run.type {self._transport!r} is not implemented in this build",
            hook_id=entry.id,
        )


def default_adapters() -> dict[str, HookAdapter]:
    """Return the adapter registry keyed by ``run.type``.

    ``command`` is fully implemented; ``http``/``prompt`` are behind the same
    interface as declared seams (P2.2 follow-ons), so a config that references
    them fails with a typed infra error rather than silently doing nothing.
    """

    from clio_agent.gact.hooks.claude_code_hooks import ClaudeCodeHookAdapter  # noqa: PLC0415
    from clio_agent.gact.hooks.plugin_hooks import PythonHookAdapter  # noqa: PLC0415

    return {
        "command": SubprocessAdapter(),
        "claude-code": ClaudeCodeHookAdapter(),
        "python": PythonHookAdapter(),
        "http": _NotImplementedAdapter("http"),
        "prompt": _NotImplementedAdapter("prompt"),
    }
