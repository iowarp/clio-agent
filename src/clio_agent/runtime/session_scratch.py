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
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
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


SOCKET_LIMIT = "socket"
PATH_LIMIT = "path"
SHEBANG_LIMIT = "shebang"
#: Each OS's limits. ``socket``: ``sun_path`` is 108 bytes on Linux and Windows and 104 on
#: macOS and the BSDs. ``path``: a DLL load stops at MAX_PATH (260) on Windows without
#: long-path support, ``dlopen`` at PATH_MAX (1024 on macOS, 4096 on Linux). ``shebang``:
#: the kernel reads a ``#!`` line into a fixed buffer (512 bytes on macOS, 256 on Linux 5.1+,
#: 128 before it); a longer interpreter path makes execve fail with ENOEXEC, and glibc then
#: runs the Python entry point with ``/bin/sh``. Windows has no shebang (uv writes .exe
#: launchers).
_OS_LIMITS: dict[str, dict[str, int]] = {
    "windows": {SOCKET_LIMIT: 108, PATH_LIMIT: 260},
    "darwin": {SOCKET_LIMIT: 104, PATH_LIMIT: 1024, SHEBANG_LIMIT: 512},
    "linux": {SOCKET_LIMIT: 108, PATH_LIMIT: 4096, SHEBANG_LIMIT: 256},
    "posix": {SOCKET_LIMIT: 104, PATH_LIMIT: 1024, SHEBANG_LIMIT: 256},
}
_LINUX_OLD_SHEBANG = 128
#: A link: files and sockets made through it stay in the folder it leads to (a symlink on
#: POSIX, a junction on Windows, which an AF_UNIX bind follows as well).
LINK = "link"
#: A real folder of its own: needed where the loader resolves a link (Windows DLLs find
#: their dependencies by real path) or a tool canonicalizes it.
STAND_IN = "stand-in"
_OWNER_FILE = "owner.txt"
#: Typed reason: a short alias for a deep scratch folder could not be made; the folder is used.
SOCKET_ALIAS_FAILED_REASON = "session_scratch_socket_alias_failed"
#: Typed reason: the short stand-in for a deep uv cache could not be made; the cache stays.
SHORT_UV_CACHE_FAILED_REASON = "session_scratch_short_uv_cache_failed"
#: Typed reason: the short folder for clio-core's IPC sockets could not be made.
SHORT_CORE_IPC_FAILED_REASON = "session_scratch_short_core_ipc_failed"

#: Where a short root may sit, tried in order: ``/tmp`` (POSIX only), the system temp, and
#: ``%LOCALAPPDATA%`` (Windows only, for when the system temp is itself deep).
POSIX_TMP = "tmp"
SYSTEM_TEMP = "system-temp"
LOCAL_APP_DATA = "local-app-data"


@dataclass(frozen=True)
class ShortRootRole:
    """What a short root stands in for: the limits it must fit, the room below it, and how."""

    name: str
    #: ``(limit name, headroom)`` pairs; the root must leave each headroom under its limit.
    limits: tuple[tuple[str, int], ...]
    mechanism: str
    failed_reason: str
    #: The bases tried in order; the first whose root fits the budget is used.
    bases: tuple[str, ...] = (SYSTEM_TEMP,)
    #: Used by fenced children: a root in the system temp only while the fence grants it.
    fenced: bool = True


#: ``TMPDIR``/``TEMP``/``TMP``: a ``mkdtemp`` folder and a socket name need about 44 bytes
#: below it (ssh's ``ControlPath`` adds about 35), so ssh's ControlMaster socket, munge,
#: ``multiprocessing`` and Chromium-style singletons fit. A POSIX link may sit in /tmp, the
#: shortest place every user can write and traverse; what is written through it lands in
#: its target.
SOCKET_ROLE = ShortRootRole(
    "socket", ((SOCKET_LIMIT, 44),), LINK, SOCKET_ALIAS_FAILED_REASON, (POSIX_TMP, SYSTEM_TEMP)
)
#: ``UV_CACHE_DIR``: ``uv run --with`` imports packages from the cache itself
#: (``archive-v0/<id>/.../site-packages/...``); CadQuery's deepest native library sits 115
#: characters below it (136 on Linux), so 212 leaves room for deeper wheels. Its entry
#: points start ``#!<cache>/builds-v0/.tmpXXXXXX/bin/python`` (``uvx`` uses
#: ``environments-v2/<name>-<hash>``), so 64 covers the ``#!`` line below the cache.
#: Windows fails on the DLL load ("The filename or extension is too long"), Linux on the
#: shebang (a ``black`` entry point under a 230-character cache), macOS on either.
UV_CACHE_ROLE = ShortRootRole(
    "uv-cache",
    ((PATH_LIMIT, 212), (SHEBANG_LIMIT, 64)),
    STAND_IN,
    SHORT_UV_CACHE_FAILED_REASON,
)
#: ``CLIO_MEMFD_DIR``: clio-core's per-user folder holds its IPC sockets, the longest
#: ``clio_zmq_<port + 3>.ipc`` (18 bytes for any port); 20 adds the separator and the NUL.
#: clio-core refuses a link there (``SystemInfo::CheckMemfdDir`` lstats it), so it gets a
#: real folder. The daemon and its clients are not fenced. Its default sits under the
#: system temp on Windows, so ``%LOCALAPPDATA%`` follows when that temp is itself deep.
CORE_IPC_ROLE = ShortRootRole(
    "core-ipc",
    ((SOCKET_LIMIT, 20),),
    STAND_IN,
    SHORT_CORE_IPC_FAILED_REASON,
    (POSIX_TMP, SYSTEM_TEMP, LOCAL_APP_DATA),
    fenced=False,
)


def _host_os() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux" if sys.platform.startswith("linux") else "posix"


def _kernel_release() -> tuple[int, int]:
    try:
        major, minor = os.uname().release.split(".")[:2]
        return int(major), int("".join(ch for ch in minor if ch.isdigit()) or 0)
    except (AttributeError, ValueError):
        return (0, 0)


def _os_limits() -> dict[str, int]:
    host = _host_os()
    limits = dict(_OS_LIMITS[host])
    if host == "linux" and _kernel_release() < (5, 1):
        limits[SHEBANG_LIMIT] = _LINUX_OLD_SHEBANG
    return limits


def short_root_budget(role: ShortRootRole) -> int:
    """Longest folder ``role`` keeps in place on this OS (each limit less its headroom)."""

    limits = _os_limits()
    return min(limits[name] - room for name, room in role.limits if name in limits)


def _base_dir(kind: str) -> Path | None:
    windows = _host_os() == "windows"
    if kind == POSIX_TMP:
        return Path("/tmp") if not windows and Path("/tmp").is_dir() else None
    if kind == LOCAL_APP_DATA:
        local = os.environ.get("LOCALAPPDATA", "").strip() if windows else ""
        return Path(local) if local else None
    return Path(tempfile.gettempdir())


def _short_path(target: Path, role: ShortRootRole) -> Path:
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(os.path.normcase(str(target)).encode("utf-8")).hexdigest()[:16]
    owner = os.getuid() if hasattr(os, "getuid") else "w"
    bases = [base for kind in role.bases if (base := _base_dir(kind)) is not None]
    shorts = [base / f"clio-{owner}-{digest}" for base in bases or [Path(tempfile.gettempdir())]]
    budget = short_root_budget(role)
    return next((short for short in shorts if len(str(short)) <= budget), shorts[0])


def _same_path(left: str, right: Path) -> bool:
    for prefix in ("\\\\?\\", "\\??\\"):  # a junction's target may carry either
        left = left.removeprefix(prefix)
    return os.path.normcase(left.rstrip("\\/")) == os.path.normcase(str(right).rstrip("\\/"))


def _mine(info: os.stat_result) -> bool:
    return not hasattr(os, "getuid") or info.st_uid == os.getuid()


def _owns_short_root(short: Path, target: Path, role: ShortRootRole) -> bool:
    """Whether ``short`` is this user's ``role`` root for ``target`` (never a foreign name)."""

    try:
        info = short.lstat()
        if not _mine(info):
            return False
        if role.mechanism == LINK:
            is_link = stat.S_ISLNK(info.st_mode) or short.is_junction()
            return is_link and _same_path(os.readlink(short), target)
        if not stat.S_ISDIR(info.st_mode) or short.is_junction():
            return False
        owner = (short / _OWNER_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return _same_path(owner, target)


def _make_short_root(short: Path, target: Path, role: ShortRootRole) -> None:
    if role.mechanism == LINK:
        if _host_os() == "windows":
            import _winapi  # noqa: PLC0415 - CPython's junction call; needs no privilege

            _winapi.CreateJunction(str(target), str(short))
        else:
            os.symlink(target, short)
        return
    short.mkdir(mode=_FOLDER_MODE, parents=True, exist_ok=True)
    info = short.lstat()
    if not stat.S_ISDIR(info.st_mode) or short.is_junction() or not _mine(info):
        raise FileExistsError(f"{short} is not this user's folder")
    if any(entry.name != _OWNER_FILE for entry in short.iterdir()):
        raise FileExistsError(f"{short} holds another {role.name} root")
    (short / _OWNER_FILE).write_text(f"{target}\n", encoding="utf-8")


def short_root(target: Path, role: ShortRootRole) -> Path:
    """``target``, or a short root of this user's standing in for it when it is too deep.

    One code path for every OS: a ``target`` longer than :func:`short_root_budget` gets
    ``<base>/clio-<uid>-<hash of target>``, one per target, so sessions stay apart; the base
    is the first of the role's bases whose root fits. A :data:`LINK` role gets a symlink
    (POSIX, in ``/tmp``) or a junction (Windows) to ``target``, so its files stay in
    ``target``; a :data:`STAND_IN` role gets a real folder whose ``owner.txt`` names
    ``target``. For a fenced role, anything in the system temp is used only while
    ``paths.allow_system_temp`` grants it to fenced children. A name that exists but is not
    this user's root for ``target`` is never used: ``target`` is. Session roots are removed
    with the session's scratch folder (:func:`remove_session_scratch`).
    A :data:`STAND_IN` judges ``target``'s real path: it exists for loaders that resolve
    links, so a short name through a link is still as deep as where it leads.
    """

    target = _role_target(target, role)
    if len(str(target)) <= short_root_budget(role):
        return target
    in_system_temp = role.mechanism == STAND_IN or _host_os() == "windows"
    if role.fenced and in_system_temp and not allow_system_temp():
        return target
    short = _short_path(target, role)
    if len(str(short)) >= len(str(target)):
        return target
    if _owns_short_root(short, target, role):
        return short
    try:
        _make_short_root(short, target, role)
    except (OSError, ImportError) as exc:
        trace.event("SCRATCH", "%s path=%s error=%r", role.failed_reason, target, exc)
        return target
    return short if _owns_short_root(short, target, role) else target


def _role_target(target: Path, role: ShortRootRole) -> Path:
    if role.mechanism != STAND_IN:
        return target
    real = os.path.realpath(target)
    return Path(real[4:] if real.startswith("\\\\?\\") else real)


def _remove_short_root(target: Path, role: ShortRootRole) -> None:
    target = _role_target(target, role)
    short = _short_path(target, role)
    if not _owns_short_root(short, target, role):
        return
    try:
        if role.mechanism == STAND_IN:
            shutil.rmtree(win_extended_path(short), ignore_errors=True)
        elif short.is_junction():
            os.rmdir(short)  # removes the junction, never what it leads to
        else:
            short.unlink()
    except OSError:
        pass


def socket_safe_dir(folder: Path) -> Path:
    """``folder``, or a short link to it when a unix socket below it would not fit."""

    return short_root(folder, SOCKET_ROLE)


def socket_safe_temp(environment: dict[str, str]) -> dict[str, str]:
    """Each deep ``TMPDIR``/``TEMP``/``TMP`` moved to its :func:`socket_safe_dir` link.

    Returns only the changed keys (empty when nothing changes).
    """

    changed: dict[str, str] = {}
    for key in ("TMPDIR", "TEMP", "TMP"):
        current = environment.get(key, "")
        if len(current) <= short_root_budget(SOCKET_ROLE) or not Path(current).is_dir():
            continue
        short = str(socket_safe_dir(Path(current)))
        if short != current:
            changed[key] = short
    return changed


def short_uv_cache(cache: Path) -> Path:
    """``cache``, or a short stand-in for it when it is too deep for uv's native packages."""

    return short_root(cache, UV_CACHE_ROLE)


def short_core_ipc_dir(folder: Path) -> Path | None:
    """A short folder of this user's for clio-core's IPC sockets, ``None`` when ``folder`` fits.

    ``folder`` is the per-user folder clio-core would pick itself. ``None`` also when it is
    too deep and no short folder could be made; that is logged, and ``folder`` stays.
    ``folder`` is judged as written: a socket's ``sun_path`` is the name clio-core binds, so
    a short name through a link (macOS ``/tmp``) fits and a running daemon keeps it.
    """

    budget = short_root_budget(CORE_IPC_ROLE)
    if len(str(folder)) <= budget:
        return None
    short = short_root(folder, CORE_IPC_ROLE)
    if len(str(short)) <= budget:
        return short
    logger.warning(
        "clio-core IPC folder too deep reason=%s path=%s budget=%d",
        SHORT_CORE_IPC_FAILED_REASON,
        folder,
        budget,
    )
    return None


def remove_session_scratch(session_id: str, *, why: str) -> bool:
    """Remove one session's scratch folder on this machine; return whether one was removed."""

    folder = session_scratch_dir(session_id, create=False)
    if folder is None or not folder.is_dir():
        return False
    _remove_short_root(folder, SOCKET_ROLE)
    _remove_short_root(folder / "uv-cache", UV_CACHE_ROLE)
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
    "short_root",
    "short_root_budget",
    "short_uv_cache",
    "socket_safe_dir",
    "socket_safe_temp",
    "sweep",
    "system_temp_roots",
]
