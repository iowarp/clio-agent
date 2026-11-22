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
import os
import re
import shutil
import socket
import stat
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


#: Unix socket paths hold 107 bytes; a ``TMPDIR`` longer than this leaves too little room for a
#: ``mkdtemp`` folder and a socket name (ssh's ``ControlPath`` adds about 35 bytes).
_SOCKET_DIR_LIMIT = 64
#: Typed reason: a short alias for a deep scratch folder could not be made; the folder is used.
SOCKET_ALIAS_FAILED_REASON = "session_scratch_socket_alias_failed"


def _socket_alias_path(folder: Path) -> Path:
    import hashlib  # noqa: PLC0415

    base = Path("/tmp" if Path("/tmp").is_dir() else tempfile.gettempdir())
    digest = hashlib.sha256(str(folder).encode()).hexdigest()[:16]
    return base / f"clio-{os.getuid()}-{digest}"


def _is_own_alias(alias: Path, folder: Path) -> bool:
    try:
        info = alias.lstat()
        return (
            stat.S_ISLNK(info.st_mode)
            and info.st_uid == os.getuid()
            and os.readlink(alias) == str(folder)
        )
    except OSError:
        return False


def socket_safe_dir(folder: Path) -> Path:
    """``folder``, or a short alias of it when a unix socket below it would not fit.

    The alias is a symlink in ``/tmp`` owned by this user, so files and sockets made through
    it stay in ``folder`` (its write roots, lifecycle and sweep are unchanged). A name that
    exists but is not this user's link to ``folder`` is never followed: ``folder`` is used.
    """

    if os.name != "posix" or len(str(folder)) <= _SOCKET_DIR_LIMIT:
        return folder
    alias = _socket_alias_path(folder)
    if _is_own_alias(alias, folder):
        return alias
    try:
        os.symlink(folder, alias)
    except OSError as exc:
        trace.event("SCRATCH", "%s path=%s error=%r", SOCKET_ALIAS_FAILED_REASON, folder, exc)
        return folder
    return alias if _is_own_alias(alias, folder) else folder


def socket_safe_temp(environment: dict[str, str]) -> dict[str, str]:
    """Each deep ``TMPDIR``/``TEMP``/``TMP`` moved to its :func:`socket_safe_dir` alias.

    Returns only the changed keys (empty when nothing changes), so ssh's ``ControlMaster``
    socket, munge, ``multiprocessing`` and other ``mkdtemp`` sockets fit the 107-byte limit.
    """

    if os.name != "posix":
        return {}
    changed: dict[str, str] = {}
    for key in ("TMPDIR", "TEMP", "TMP"):
        current = environment.get(key, "")
        if len(current) <= _SOCKET_DIR_LIMIT or not Path(current).is_dir():
            continue
        short = str(socket_safe_dir(Path(current)))
        if short != current:
            changed[key] = short
    return changed


def _remove_socket_alias(folder: Path) -> None:
    if os.name != "posix":
        return
    alias = _socket_alias_path(folder)
    if _is_own_alias(alias, folder):
        try:
            alias.unlink()
        except OSError:
            pass


#: Longest uv cache a Windows shell uses in place. ``uv run --with`` imports packages from
#: the cache itself (``archive-v0\<id>\Lib\site-packages\...``), and the Windows DLL loader
#: refuses a path past MAX_PATH unless long paths are enabled, which they are not by default
#: ("DLL load failed ... The filename or extension is too long"). CadQuery's deepest DLL sits
#: 115 characters below the cache, so a cache in a deep scratch folder or workspace fails.
#: A link does not help: the loader finds a DLL's dependencies by its real path.
WINDOWS_UV_CACHE_MAX = 48
_WINDOWS_UV_CACHE_DIR = "clio-u"
#: Typed reason: the short stand-in for a deep uv cache could not be made; the cache stays.
SHORT_UV_CACHE_FAILED_REASON = "session_scratch_short_uv_cache_failed"
_OWNER_FILE = "owner.txt"


def _windows() -> bool:
    return os.name == "nt"


def _short_uv_cache_path(cache: Path) -> Path:
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(os.path.normcase(str(cache)).encode("utf-8")).hexdigest()[:10]
    return Path(tempfile.gettempdir()) / _WINDOWS_UV_CACHE_DIR / digest


def _owns_short_uv_cache(short: Path, cache: Path) -> bool:
    try:
        owner = (short / _OWNER_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return os.path.normcase(owner) == os.path.normcase(str(cache))


def short_uv_cache(cache: Path) -> Path:
    """``cache``, or on Windows a short stand-in for it when it is too deep for uv's packages.

    The stand-in is ``<system temp>\\clio-u\\<hash of cache>``, one per cache, so each
    session keeps its own; the system temp is in every shell's write roots. Its
    ``owner.txt`` names the cache it stands in for, and it is removed with the session's
    scratch folder. Without the system temp (``paths.allow_system_temp`` off) the cache
    stays where it is.
    """

    if not _windows() or len(str(cache)) <= WINDOWS_UV_CACHE_MAX or not allow_system_temp():
        return cache
    short = _short_uv_cache_path(cache)
    if len(str(short)) >= len(str(cache)):
        return cache
    if _owns_short_uv_cache(short, cache):
        return short
    try:
        short.mkdir(parents=True, exist_ok=True)
        if any(entry.name != _OWNER_FILE for entry in short.iterdir()):
            raise FileExistsError(f"{short} holds another cache")
        (short / _OWNER_FILE).write_text(f"{cache}\n", encoding="utf-8")
    except OSError as exc:
        trace.event("SCRATCH", "%s path=%s error=%r", SHORT_UV_CACHE_FAILED_REASON, cache, exc)
        return cache
    return short


def _remove_short_uv_cache(cache: Path) -> None:
    if not _windows():
        return
    short = _short_uv_cache_path(cache)
    if _owns_short_uv_cache(short, cache):
        shutil.rmtree(win_extended_path(short), ignore_errors=True)


def remove_session_scratch(session_id: str, *, why: str) -> bool:
    """Remove one session's scratch folder on this machine; return whether one was removed."""

    folder = session_scratch_dir(session_id, create=False)
    if folder is None or not folder.is_dir():
        return False
    _remove_socket_alias(folder)
    _remove_short_uv_cache(folder / "uv-cache")
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
    "short_uv_cache",
    "sweep",
    "system_temp_roots",
]
