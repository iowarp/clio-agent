"""Per-session scratch folders (TD56): ``<scratch root>/<machine name>/<session id>``.

Every other scratch place is shared: the platform temp dir by every session on the
machine, ``<workspace>/.tmp`` by every session of a workspace, and the allowed roots by
everything. A session's own scratch folder is in its write roots
(:func:`clio_agent.tools.file_policy._with_active_workspace`), is the shell's and the
``run_code`` kernel's ``TMPDIR``
(:func:`clio_agent.runtime.execution_environment.shell_environment`), and is named in the
runtime context of its prompt (:mod:`clio_agent.gact.agents.composition`).

The machine name keeps hosts apart when the root sits on a file system shared by several
nodes (NFS on HPC). A child session has its own id and so its own folder; nothing is
shared by default.

Lifecycle: created when the session first needs it (its shell or kernel environment), removed
when the session is deleted (:func:`remove_session_scratch`), and otherwise swept
:func:`retention_days` after the session's last activity (:func:`sweep`, at server start).
Each removal is recorded with a typed reason.
"""

from __future__ import annotations

import functools
import logging
import re
import shutil
import socket
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from clio_agent import conf, paths
from clio_agent.platform_paths import win_extended_path
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

#: Typed reason: a session's scratch folder was removed.
REMOVED_REASON = "session_scratch_removed"
#: Typed reason: a session's scratch folder could not be removed.
REMOVE_FAILED_REASON = "session_scratch_remove_failed"
#: Typed reason: a session's scratch folder could not be created.
CREATE_FAILED_REASON = "session_scratch_create_failed"
#: Typed reason: a session id is not a single safe path segment; no folder is named.
UNSAFE_ID_REASON = "session_scratch_unsafe_session_id"
#: Typed reason: the machine name sanitized to nothing; the stand-in segment is used.
MACHINE_NAME_EMPTY_REASON = "session_scratch_machine_name_empty"

#: ``why`` values for :func:`remove_session_scratch`.
WHY_SESSION_DELETED = "session_deleted"
WHY_SESSION_GONE = "session_gone"
WHY_RETENTION = "retention_expired"

_SESSION_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_UNSAFE_MACHINE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MACHINE_SEGMENT_CHARS = 64
_MACHINE_STAND_IN = "host"
_FOLDER_MODE = 0o700
_SECONDS_PER_DAY = 86400.0


def machine_name() -> str:
    """This host's name as one path segment (``paths.machine_name`` / ``CLIO_MACHINE_NAME``)."""

    raw = conf.resolve(
        "paths.machine_name", env="CLIO_MACHINE_NAME", default=socket.gethostname(), cast=str
    )
    name = _UNSAFE_MACHINE_CHARS.sub("_", str(raw or "").strip()).lstrip(".")
    name = name[:_MACHINE_SEGMENT_CHARS]
    if not name:
        trace.event("SCRATCH", "%s raw=%r", MACHINE_NAME_EMPTY_REASON, raw)
        return _MACHINE_STAND_IN
    return name


@functools.cache
def _default_scratch_root() -> Path:
    """``<user state dir>/scratch``, resolved once: it is on every file-policy check."""

    return paths.user_state_dir() / "scratch"


def scratch_root() -> Path:
    """The root of every machine's scratch folders (``paths.scratch_root``)."""

    raw = conf.resolve("paths.scratch_root", env="CLIO_SCRATCH_ROOT", default="", cast=str)
    return Path(str(raw)).expanduser() if raw else _default_scratch_root()


def retention_days() -> float:
    """Days a session's scratch folder outlives the session's last activity."""

    return conf.resolve(
        "paths.scratch_retention_days",
        env="CLIO_SCRATCH_RETENTION_DAYS",
        default=7.0,
        cast=conf.as_float,
    )


def allow_system_temp() -> bool:
    """Whether the platform temp dir stays in the write roots (``paths.allow_system_temp``)."""

    return conf.resolve(
        "paths.allow_system_temp", env="CLIO_ALLOW_SYSTEM_TEMP", default=True, cast=conf.as_bool
    )


def system_temp_roots() -> tuple[Path, ...]:
    """The platform temp dir as a write root, or nothing when ``paths.allow_system_temp`` is off."""

    return (Path(tempfile.gettempdir()),) if allow_system_temp() else ()


def machine_dir() -> Path:
    """This machine's folder under :func:`scratch_root`."""

    return scratch_root() / machine_name()


def _safe_session_id(session_id: str | None) -> str:
    sid = (session_id or "").strip()
    return sid if _SESSION_SEGMENT.fullmatch(sid) else ""


def session_scratch_dir(session_id: str | None, *, create: bool = True) -> Path | None:
    """``<root>/<machine>/<session id>`` (mode 0o700), ``None`` for an unsafe or empty id.

    With ``create`` the folder is made when missing; a failure to make it is recorded
    (:data:`CREATE_FAILED_REASON`) and returns ``None``, so callers keep their
    workspace scratch instead of naming a folder that does not exist.
    """

    sid = _safe_session_id(session_id)
    if not sid:
        if (session_id or "").strip():
            trace.event("SCRATCH", "%s session=%r", UNSAFE_ID_REASON, session_id)
        return None
    folder = machine_dir() / sid
    if not create:
        return folder.resolve()
    try:
        folder.mkdir(mode=_FOLDER_MODE, parents=True, exist_ok=True)
        folder.chmod(_FOLDER_MODE)  # mkdir's mode is masked by the umask
    except OSError as exc:
        logger.warning(
            "session scratch not created reason=%s session=%s path=%s error=%r",
            CREATE_FAILED_REASON,
            sid,
            folder,
            exc,
        )
        trace.event("SCRATCH", "%s session=%s path=%s", CREATE_FAILED_REASON, sid, folder)
        return None
    return folder.resolve()


def active_session() -> str:
    """The session that owns the current turn or tool call, or ``""``."""

    from clio_agent.gact import context as gact_context  # noqa: PLC0415 - keep runtime a leaf

    return gact_context.active_session_id() or gact_context.active_tool_session_id()


def active_scratch_dir(*, create: bool = False) -> Path | None:
    """The active session's scratch folder, or ``None`` off-session."""

    sid = active_session()
    return session_scratch_dir(sid, create=create) if sid else None


def remove_session_scratch(session_id: str, *, why: str) -> bool:
    """Remove one session's scratch folder on this machine; return whether one was removed."""

    folder = session_scratch_dir(session_id, create=False)
    if folder is None or not folder.is_dir():
        return False
    try:
        shutil.rmtree(win_extended_path(folder))
    except OSError as exc:
        logger.warning(
            "session scratch remove failed reason=%s why=%s session=%s path=%s error=%r",
            REMOVE_FAILED_REASON,
            why,
            session_id,
            folder,
            exc,
        )
        trace.event(
            "SCRATCH",
            "%s why=%s session=%s path=%s",
            REMOVE_FAILED_REASON,
            why,
            session_id,
            folder,
        )
        return False
    trace.event("SCRATCH", "%s why=%s session=%s path=%s", REMOVED_REASON, why, session_id, folder)
    return True


def sweep(last_activity: Callable[[str], float | None], *, now: float | None = None) -> list[str]:
    """Remove this machine's folders whose session is gone or idle past retention.

    Args:
        last_activity: The session's last activity as epoch seconds, ``None`` when the
            session no longer exists. An exception (the store is not readable) stops the
            sweep without removing that folder.
        now: The current time (epoch seconds), for tests.

    Returns:
        The session ids whose folders were removed.
    """

    root = machine_dir()
    if not root.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - retention_days() * _SECONDS_PER_DAY
    removed: list[str] = []
    for entry in sorted(root.iterdir()):
        sid = _safe_session_id(entry.name)
        if not sid or not entry.is_dir():
            continue
        when = last_activity(sid)
        why = WHY_SESSION_GONE if when is None else WHY_RETENTION if when < cutoff else ""
        if why and remove_session_scratch(sid, why=why):
            removed.append(sid)
    return removed


__all__ = [
    "CREATE_FAILED_REASON",
    "REMOVED_REASON",
    "REMOVE_FAILED_REASON",
    "UNSAFE_ID_REASON",
    "active_scratch_dir",
    "active_session",
    "allow_system_temp",
    "machine_dir",
    "machine_name",
    "remove_session_scratch",
    "retention_days",
    "scratch_root",
    "session_scratch_dir",
    "sweep",
    "system_temp_roots",
]
