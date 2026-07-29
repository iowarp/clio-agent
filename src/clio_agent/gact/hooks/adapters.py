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
import os
import signal
import subprocess
import sys
from abc import ABC, abstractmethod
from typing import Any

from clio_agent.gact.hooks.config import HookEntry
from clio_agent.gact.hooks.wire import (
    HookDecision,
    HookEnvelope,
    HookInfraError,
    extract_json_object,
    parse_hook_output,
    record_hook_reason,
)

logger = logging.getLogger(__name__)


class HookAdapter(ABC):
    """One transport for invoking a hook. Returns a decision or raises infra error."""

    @abstractmethod
    def invoke(self, entry: HookEntry, envelope: HookEnvelope) -> HookDecision:
        """Invoke ``entry`` for ``envelope``.

        Returns the parsed :class:`HookDecision` on success (exit 0 / exit 2).
        Raises :class:`HookInfraError` on any infrastructure failure (timeout,
        crash, missing binary) — DISTINCT from a ``deny`` decision.
        """


class SubprocessAdapter(HookAdapter):
    """The industry exit-0/exit-2 wire (hooks-research §5.3).

    Spawn the hook process with NO controlling terminal, write the JSON envelope
    to its stdin, read stdout/stderr. Exit 0 => parse stdout (empty => allow).
    Exit 2 => deny, stderr becomes the model-facing reason. Any other exit => a
    non-blocking :class:`HookInfraError` the dispatcher resolves via ``failClosed``.
    """

    def invoke(self, entry: HookEntry, envelope: HookEnvelope) -> HookDecision:
        import json  # noqa: PLC0415 - local; keeps module import graph lean

        argv = [entry.run.command, *entry.run.args]
        # ``default=str`` keeps a non-JSON value in an observation payload from
        # exploding the envelope (a SemanticEvent payload can carry arbitrary objects).
        stdin_blob = json.dumps(envelope.to_json(hook_id=entry.id), default=str)
        # A manual Popen (rather than subprocess.run) so the timeout path below can
        # kill the whole process GROUP, not just the direct child PID — start_new_session
        # (POSIX) makes the child a group leader, so a hook that forks would otherwise
        # leave orphans running past the timeout kill (a resource leak, worst on CI).
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is exec-form (never shell); command is operator-declared
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **_no_tty_kwargs(),
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

        timeout_s = entry.timeout_s if entry.timeout_s > 0 else None
        try:
            stdout, stderr = proc.communicate(input=stdin_blob, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
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
        except BaseException:
            # Any other failure (including KeyboardInterrupt) while waiting: clean up
            # the whole process group too, then re-raise unchanged.
            _kill_process_group(proc)
            raise

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
            return parse_hook_output(obj, hook_id=entry.id, event=envelope.hook_event_name)
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


def _kill_process_group(proc: "subprocess.Popen[str]") -> None:
    """Kill the whole process group/session spawned for a hook, not just its PID.

    ``_no_tty_kwargs`` starts the hook with ``start_new_session=True`` on POSIX,
    which makes the hook process the leader of its own process group/session — so
    a hook that forks a child (or execs a wrapper script that does) leaves that
    child running past a timeout kill unless the whole GROUP is signalled
    (``os.killpg``), not just the direct child PID. On Windows no new process
    group is created (``CREATE_NO_WINDOW`` only detaches the console), so the
    happy-path behavior is unchanged there: kill the direct child, using
    ``taskkill /T`` first on a best-effort basis to catch any child tree it spawned.

    Best-effort and guarded: the process may already have exited by the time this
    runs, and ``os.killpg``/``os.getpgid`` do not exist on Windows at all, so every
    step is wrapped so a cleanup failure never masks the timeout that triggered it.
    """

    if sys.platform == "win32":
        try:
            subprocess.run(  # noqa: S603,S607 - fixed argv, best-effort tree kill
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:  # noqa: BLE001,S110 - best-effort; proc.kill() below is the fallback
            pass
        try:
            proc.kill()
        except Exception:  # noqa: BLE001,S110 - process may already be gone
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001,S110 - reap is best-effort; never mask the timeout
            pass
        return

    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = None
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
                proc.wait(timeout=2)
                return
            except ProcessLookupError:
                return
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except Exception:  # noqa: BLE001,S110 - fall through to the plain-kill fallback
                pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001,S110 - reap is best-effort
                pass
            return
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001,S110 - process may already be gone
        pass


class _NotImplementedAdapter(HookAdapter):
    """A clean seam for a transport declared but not yet implemented in this slice."""

    def __init__(self, transport: str) -> None:
        self._transport = transport

    def invoke(self, entry: HookEntry, envelope: HookEnvelope) -> HookDecision:
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

    return {
        "command": SubprocessAdapter(),
        "http": _NotImplementedAdapter("http"),
        "prompt": _NotImplementedAdapter("prompt"),
    }
