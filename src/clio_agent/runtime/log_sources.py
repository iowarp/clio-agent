"""Where CLIO's own logs live on this computer, and their last lines.

The CLIO server writes its log to standard output; whoever starts it decides where
that goes. These are the same files ``clio logs`` shows (``install/clio`` and
``install/clio.ps1``), plus the desktop app's capture of the server it manages:

* ``server`` -- ``$CLIO_LOG`` when set, else ``<state>/clio-server.<host>.log``
  (the Unix launcher keys it by host because a cluster shares one home), or
  ``<state>/clio-server.log`` and ``<state>/clio-server.err.log`` (the Windows
  launcher, which writes standard error separately).
* ``gact`` -- ``<state>/gact-stderr.log``.
* ``desktop`` -- the desktop app tees the server's output into
  ``backend-boot.log`` in its Tauri log directory: ``~/Library/Logs/<id>`` on
  macOS, ``$XDG_DATA_HOME/<id>/logs`` on Linux, ``%LOCALAPPDATA%\\<id>\\logs`` on
  Windows. ``<id>`` is the branded bundle identifier.

``<state>`` is :func:`clio_agent.paths.user_state_dir`, the launcher's
``CLIO_STATE``. A server started straight from a terminal (``clio-agent serve``)
has no file at all; the report then says so with a typed reason rather than
showing an empty log.
"""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from clio_agent import paths

#: Desktop bundle identifiers: the CLIO brand's, then the product-neutral build's.
DESKTOP_IDENTIFIERS = ("ai.iowarp.clio.desktop", "ai.iowarp.gact.desktop")
DESKTOP_LOG_NAME = "backend-boot.log"

DEFAULT_TAIL_LINES = 500
MAX_TAIL_LINES = 5000
#: A single log line longer than this is cut (a dumped payload, a minified bundle).
MAX_LINE_CHARS = 2000
#: Read at most this many bytes from the end of one file, whatever the line count.
MAX_TAIL_BYTES = 4 * 1024 * 1024

LogStatus = Literal["ok", "empty", "missing", "unreadable"]
LogRole = Literal["server", "gact", "desktop"]


@dataclass(frozen=True)
class LogCandidate:
    """A file CLIO may have written its log to."""

    role: LogRole
    path: Path
    #: Where this location comes from, in a few words ("CLIO_LOG", "launcher").
    origin: str


@dataclass
class LogTail:
    """The last lines of one log file, or why there are none."""

    role: LogRole
    path: Path
    origin: str
    status: LogStatus
    lines: list[str] = field(default_factory=list)
    #: Whether the file holds lines before the returned ones.
    earlier_lines: bool = False
    reason: str = ""
    message: str = ""
    size_bytes: int | None = None
    modified_at: float | None = None


def _platform() -> Literal["linux", "macos", "windows"]:
    return (
        "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
    )


def host_id() -> str:
    """The launcher's ``CLIO_HOST_ID``: the short host name (``hostname -s``)."""

    name = socket.gethostname() or "localhost"
    return name.split(".", 1)[0] or "localhost"


def desktop_log_dirs(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: Literal["linux", "macos", "windows"] | None = None,
) -> list[Path]:
    """Tauri's ``app_log_dir`` for each desktop identifier on this platform."""

    source = os.environ if env is None else env
    base_home = home if home is not None else Path.home()
    system = platform or _platform()
    dirs: list[Path] = []
    for identifier in DESKTOP_IDENTIFIERS:
        if system == "macos":
            dirs.append(base_home / "Library" / "Logs" / identifier)
        elif system == "windows":
            local = source.get("LOCALAPPDATA") or str(base_home / "AppData" / "Local")
            dirs.append(Path(local) / identifier / "logs")
        else:
            data = source.get("XDG_DATA_HOME", "").strip()
            root = Path(data) if data and Path(data).is_absolute() else base_home / ".local/share"
            dirs.append(root / identifier / "logs")
    return dirs


def log_candidates(
    *,
    env: Mapping[str, str] | None = None,
    state_dir: Path | None = None,
    home: Path | None = None,
    platform: Literal["linux", "macos", "windows"] | None = None,
    host: str | None = None,
) -> list[LogCandidate]:
    """Every place ``clio logs`` (or the desktop app) puts CLIO's log, in report order."""

    source = os.environ if env is None else env
    system = platform or _platform()
    state = state_dir if state_dir is not None else paths.user_state_dir()
    candidates: list[LogCandidate] = []
    explicit = source.get("CLIO_LOG", "").strip()
    if explicit:
        candidates.append(LogCandidate("server", Path(explicit), "CLIO_LOG"))
    if system == "windows":
        candidates.append(LogCandidate("server", state / "clio-server.log", "launcher"))
        candidates.append(LogCandidate("server", state / "clio-server.err.log", "launcher"))
    else:
        name = f"clio-server.{host or host_id()}.log"
        candidates.append(LogCandidate("server", state / name, "launcher"))
        # Launchers before the per-host name wrote one shared file.
        candidates.append(LogCandidate("server", state / "clio-server.log", "launcher"))
    candidates.append(LogCandidate("gact", state / "gact-stderr.log", "launcher"))
    for directory in desktop_log_dirs(env=source, home=home, platform=system):
        candidates.append(LogCandidate("desktop", directory / DESKTOP_LOG_NAME, "desktop app"))
    unique: list[LogCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate.path))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def clamp_lines(requested: int | None) -> int:
    """The tail length to read: the default when unset, bounded to ``1..MAX_TAIL_LINES``."""

    if requested is None:
        return DEFAULT_TAIL_LINES
    return max(1, min(int(requested), MAX_TAIL_LINES))


def _tail_bytes(path: Path, lines: int) -> tuple[bytes, bool]:
    """The file's last ``lines`` lines as bytes, reading backwards in blocks.

    Returns the bytes and whether the file holds anything before them.
    """

    block = 64 * 1024
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        position = end
        chunks: list[bytes] = []
        newlines = 0
        read = 0
        while position > 0 and newlines <= lines and read < MAX_TAIL_BYTES:
            step = min(block, position, MAX_TAIL_BYTES - read)
            position -= step
            handle.seek(position)
            chunk = handle.read(step)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
            read += step
    data = b"".join(reversed(chunks))
    split = data.splitlines()
    earlier = position > 0 or len(split) > lines
    return b"\n".join(split[-lines:]), earlier


def read_tail(candidate: LogCandidate, lines: int) -> LogTail:
    """The candidate's last ``lines`` lines, or a typed reason it has none."""

    path = candidate.path
    base = LogTail(role=candidate.role, path=path, origin=candidate.origin, status="missing")
    try:
        stat = path.stat()
    except FileNotFoundError:
        base.reason = "log_file_absent"
        base.message = "No log file at this location."
        return base
    except OSError as exc:
        base.status = "unreadable"
        base.reason = "log_file_unreadable"
        base.message = f"The log file could not be checked: {exc.strerror or exc}"
        return base
    base.size_bytes = stat.st_size
    base.modified_at = stat.st_mtime
    if not path.is_file():
        base.status = "unreadable"
        base.reason = "log_path_not_a_file"
        base.message = "This location is not a file."
        return base
    try:
        data, earlier = _tail_bytes(path, lines)
    except OSError as exc:
        base.status = "unreadable"
        base.reason = "log_file_unreadable"
        base.message = f"The log file could not be read: {exc.strerror or exc}"
        return base
    text = data.decode("utf-8", errors="replace")
    rows = [
        row if len(row) <= MAX_LINE_CHARS else f"{row[:MAX_LINE_CHARS]}… [line cut]"
        for row in text.splitlines()
    ]
    base.lines = rows
    base.earlier_lines = earlier
    if rows:
        base.status = "ok"
    else:
        base.status = "empty"
        base.reason = "log_file_empty"
        base.message = "The log file is empty."
    return base


def collect_log_tails(
    lines: int | None = None, candidates: list[LogCandidate] | None = None
) -> list[LogTail]:
    """The tail of every log location: found files with lines, absent ones with a reason.

    An absent launcher or desktop file is normal (CLIO runs one way at a time), so the
    caller decides which absences to show; every candidate is returned.
    """

    count = clamp_lines(lines)
    return [read_tail(candidate, count) for candidate in (candidates or log_candidates())]


__all__ = [
    "DEFAULT_TAIL_LINES",
    "DESKTOP_IDENTIFIERS",
    "MAX_TAIL_LINES",
    "LogCandidate",
    "LogTail",
    "clamp_lines",
    "collect_log_tails",
    "desktop_log_dirs",
    "host_id",
    "log_candidates",
    "read_tail",
]
