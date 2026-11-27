"""Remove a folder CLIO owns, including what a fenced child made inside it.

On Windows a fenced child runs as Codex's sandbox account. Python 3.13+ gives every
``mkdir(mode=0o700)`` (``tempfile.mkdtemp``) a protected DACL that grants only SYSTEM,
Administrators and that account, so the person CLIO runs as can neither list nor delete
the folder, and cannot take it over without elevation. The sandbox account owns it and
can, so a tree the person cannot remove is removed again through the fence: a confined
``rd /s /q`` whose only write root is that tree. Every outcome leaves a typed trace;
nothing is dropped silently.

:func:`grant_person` is the same route for a folder that must stay (a workspace): the
fenced identity grants the person full control, so they can delete it themselves.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from clio_agent.platform_paths import TreeRemovalError, win_extended_path
from clio_agent.platform_paths import remove_tree as _remove_as_person
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

#: Typed reason: the tree was removed, through the fence for what the person could not.
REMOVED_THROUGH_FENCE_REASON = "fenced_removal_done"
#: Typed reason: the tree survived both the person's removal and the fenced one.
REMOVE_FAILED_REASON = "fenced_removal_failed"
#: Typed reason: the fence's identity granted the person control of a tree.
GRANTED_REASON = "fenced_grant_done"
#: Typed reason: the fence's identity could not grant the person control of a tree.
GRANT_FAILED_REASON = "fenced_grant_failed"
_TIMEOUT_S = 300.0


def remove_tree(path: str | Path, *, why: str) -> bool:
    """Delete ``path`` completely; return whether it is gone.

    The person's removal runs first (read-only files are cleared and retried). What
    survives on Windows is removed by a fenced child when the fence is active. A tree that
    still survives is traced with :data:`REMOVE_FAILED_REASON`, the person's error and the
    fenced command's own output.
    """

    target = Path(path)
    try:
        _remove_as_person(target, attempts=2)
        return True
    except TreeRemovalError as exc:
        first = f"{exc}"
    fenced = _run_as_fence(target, ["rd", "/s", "/q", win_extended_path(target)])
    if fenced is not None:
        try:
            _remove_as_person(target, attempts=2)  # the root the person made, now empty
        except TreeRemovalError:
            pass
        if not os.path.lexists(win_extended_path(target)):
            trace.event("SANDBOX", "%s why=%s path=%s", REMOVED_THROUGH_FENCE_REASON, why, target)
            return True
    logger.warning("tree not removed reason=%s why=%s path=%s", REMOVE_FAILED_REASON, why, target)
    trace.event(
        "SANDBOX",
        "%s why=%s path=%s error=%s fenced=%s",
        REMOVE_FAILED_REASON,
        why,
        target,
        first,
        "fence inactive" if fenced is None else fenced or "(no output)",
    )
    return False


def grant_person(path: str | Path, *, why: str) -> bool:
    """Grant the person full control of ``path`` and below, through the fence's identity.

    For a folder that stays (the workspace) whose fenced-made parts the person cannot open.
    Returns whether the grant ran without error; the outcome is traced either way.
    """

    target = Path(path)
    sid = _person_sid()
    output = None
    if sid:
        grant = f"*{sid}:(OI)(CI)F"
        output = _run_as_fence(target, ["icacls", str(target), "/grant", grant, "/T", "/C", "/Q"])
    ok = output is not None and "failed processing 0 files" in output.lower()
    reason = GRANTED_REASON if ok else GRANT_FAILED_REASON
    detail = "fence inactive" if output is None else output.strip() or "(no output)"
    trace.event("SANDBOX", "%s why=%s path=%s output=%s", reason, why, target, detail)
    return ok


def _person_sid() -> str:
    try:
        line = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return line.rsplit(",", 1)[-1].strip('"') if line.startswith('"') else ""


def _run_as_fence(target: Path, command: list[str]) -> str | None:
    """Run ``command`` (a ``cmd.exe`` builtin or tool) confined to ``target``.

    Returns its combined output, or ``None`` when no Windows fence is active to run it.
    """

    if os.name != "nt" or not target.is_dir():
        return None
    from clio_agent.runtime import sandbox  # noqa: PLC0415 - heavy import on demand

    state = sandbox.current_state()
    if state is None or not state.active or state.mechanism != sandbox.MECHANISM_CODEX:
        return None
    try:
        confined = sandbox.wrap_confined(
            "cmd.exe",
            ["/d", "/c", *command],
            write_roots=[target],
            net_policy=sandbox.NET_DENY,
            profile=sandbox.PROFILE_SHELL,
            cwd=target.parent,
        )
    except sandbox.SandboxCompositionError as exc:
        return f"fence could not compose: {exc}"
    try:
        done = subprocess.run(
            [confined.command, *confined.args],
            cwd=target.parent,
            env={**os.environ, **confined.env_overlay},
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_TIMEOUT_S,
            check=False,
            **confined.popen_kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        confined.release()
    return f"exit={done.returncode} {done.stdout}{done.stderr}".strip()


__all__ = [
    "GRANTED_REASON",
    "GRANT_FAILED_REASON",
    "REMOVED_THROUGH_FENCE_REASON",
    "REMOVE_FAILED_REASON",
    "grant_person",
    "remove_tree",
]
