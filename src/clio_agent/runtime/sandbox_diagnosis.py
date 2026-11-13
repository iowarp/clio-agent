"""Why a confined call failed at the sandbox boundary, in the call's own result (#1905).

Every agent-driven spawn is fenced (:mod:`clio_agent.runtime.sandbox`). When the fence
blocks a write, the child only sees an OS denial: a non-zero exit and stderr. This module
turns that denial into one typed diagnosis the confined seam attaches to its result, so
the model reads which folder was blocked and how to ask for it, and the provenance code
(:mod:`clio_agent.gact.artifacts.violations`) records the same object instead of parsing
the denial a second time.

:func:`diagnose_boundary` works in three steps, all of which must agree for a confirmed
diagnosis:

1. a denial signature in stderr (:data:`DENIAL_SIGNATURES`, one table for every platform;
   Windows text is localized, so its numeric codes match on their own);
2. candidate paths, first from the denial lines, then from the command, relative ones
   resolved against ``cwd``;
3. the territory check: only candidates outside every write root the seam gave the fence
   stay. A denied path inside the territory (a read-only file in the workspace), or one
   the person's own account could not write either (an ordinary file permission, so a
   folder grant would not help), is an ordinary permission error and gets no diagnosis.

A denial with no path at all gives a hedged ``possible`` diagnosis. An exit code of 740
or "requires elevation" gives ``elevation_required``, which no folder grant can fix. On
the honest floor (mechanism ``none``) nothing is diagnosed: no fence can have caused it.

The diagnosis only adds a field; it never changes the exit code, stdout or stderr, never
reruns anything and never raises a permission prompt by itself.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Literal

KIND_WRITE_DENIED = "write_denied"
KIND_ELEVATION_REQUIRED = "elevation_required"
KIND_OFFICE_IN_SHELL = "office_in_shell"
CONFIRMED = "confirmed"
POSSIBLE = "possible"

#: Which confined seam produced the denial: the retry the message names depends on it.
Seam = Literal["shell", "script", "tool"]

#: ``ERROR_ELEVATION_REQUIRED``: Windows refuses to start a program that needs admin.
ELEVATION_EXIT_CODE = 740

#: The denial table: ``(name, pattern)``. A ``path`` group, when present, is the denied
#: path itself. Windows messages are localized, so the numeric codes and exception names
#: match on their own; the English text forms are what POSIX shells and tools print.
DENIAL_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # PowerShell: "Access to the path 'C:\...' is denied."
    ("ERROR_ACCESS_DENIED", re.compile(r"Access to the path '(?P<path>[^']+)' is denied")),
    ("ERROR_ACCESS_DENIED", re.compile(r"\bUnauthorizedAccessException\b")),
    ("ERROR_ACCESS_DENIED", re.compile(r"\[WinError 5\]")),
    ("ERROR_ACCESS_DENIED", re.compile(r"0x80070005", re.IGNORECASE)),
    ("ERROR_ACCESS_DENIED", re.compile(r"\bAccess is denied\b", re.IGNORECASE)),
    ("EACCES", re.compile(r"\[Errno 13\]")),
    ("EROFS", re.compile(r"\[Errno 30\]")),
    ("EPERM", re.compile(r"\[Errno 1\]")),
    ("EROFS", re.compile(r"\bRead-only file system\b", re.IGNORECASE)),
    ("EPERM", re.compile(r"\bOperation not permitted\b", re.IGNORECASE)),
    ("EACCES", re.compile(r"\bPermission denied\b", re.IGNORECASE)),
    ("EACCES", re.compile(r"\bEACCES\b")),
    ("EROFS", re.compile(r"\bEROFS\b")),
    ("EPERM", re.compile(r"\bEPERM\b")),
)

#: #2139: AppKit LibreOffice aborts (SIGABRT) under the macOS shell fence.
_OFFICE_COMMAND = re.compile(r"\b(?:soffice|libreoffice)\b", re.IGNORECASE)
_ABORT_EXIT_CODES = (134, -6)
_ELEVATION_TEXT = re.compile(r"requires elevation|\bERROR_ELEVATION_REQUIRED\b", re.IGNORECASE)
#: Quoted text in a denial line (straight and typographic quotes, as GNU tools print).
_QUOTED = re.compile(r"'([^'\r\n]+)'|\"([^\"\r\n]+)\"|‘([^’\r\n]+)’|“([^”\r\n]+)”")
#: Each quote kind alone, so a path quoted inside another quote (a ``repr``) is found too.
_QUOTE_KINDS = tuple(
    re.compile(f"{a}([^{b}\r\n]+){b}") for a, b in (("'", "'"), ('"', '"'), ("‘", "’"), ("“", "”"))
)
#: The token right before an OS error text ("bash: line 1: /p: Read-only file system").
_BEFORE_STRERROR = re.compile(
    r"(\S+): (?:permission denied|read-only file system|operation not permitted|access is denied)",
    re.IGNORECASE,
)
#: PowerShell's CategoryInfo target, ``(C:\\path\\file.csv:FileInfo)``: the denied path in
#: any UI language, next to the ``UnauthorizedAccessException`` name.
_CATEGORY_TARGET = re.compile(r"\((?P<path>[A-Za-z]:\\[^:()\r\n]+):\w+\)")
_TRIM = "'\"‘’“”()[]{}<>,;"
#: Bound on the stderr scanned (a denial is a short line; a huge stream is mostly output).
_MAX_SCAN_CHARS = 200_000


@dataclass(frozen=True)
class BoundaryDiagnosis:
    """One sandbox-boundary diagnosis: the typed ``boundary`` field of a confined result.

    ``paths`` are the denied paths outside the territory; ``request_roots`` the folders to
    ask for (the nearest existing parent of each); ``message`` plain instructions to the
    model, generated from the diagnosis so it always names the real paths. ``signature``
    names the denial-table row that matched (``EACCES``, ``ERROR_ACCESS_DENIED``, ...).
    """

    kind: str
    confidence: str
    mechanism: str
    paths: tuple[str, ...] = ()
    request_roots: tuple[str, ...] = ()
    message: str = ""
    signature: str = ""

    def to_payload(self) -> dict[str, Any]:
        """The ``boundary`` field as it appears on a tool result."""
        return {
            "kind": self.kind,
            "confidence": self.confidence,
            "mechanism": self.mechanism,
            "paths": list(self.paths),
            "request_roots": list(self.request_roots),
            "message": self.message,
            "signature": self.signature,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> BoundaryDiagnosis | None:
        """Read a ``boundary`` field back; ``None`` when it is not one."""
        if not isinstance(payload, Mapping) or payload.get("kind") not in (
            KIND_WRITE_DENIED,
            KIND_ELEVATION_REQUIRED,
            KIND_OFFICE_IN_SHELL,
        ):
            return None
        return cls(
            kind=str(payload["kind"]),
            confidence=str(payload.get("confidence") or POSSIBLE),
            mechanism=str(payload.get("mechanism") or ""),
            paths=tuple(str(p) for p in payload.get("paths") or ()),
            request_roots=tuple(str(p) for p in payload.get("request_roots") or ()),
            message=str(payload.get("message") or ""),
            signature=str(payload.get("signature") or ""),
        )


@dataclass(frozen=True)
class _Flavor:
    """How paths read on the platform the denial came from."""

    windows: bool
    native: bool  # the paths are on this host's own filesystem
    pure: type[PurePath]

    def absolute(self, text: str) -> bool:
        return self.pure(text).is_absolute() or (self.windows and text.startswith("\\\\"))


def _flavor(platform: str) -> _Flavor:
    windows = platform.startswith("win")
    return _Flavor(
        windows=windows,
        native=windows == (os.name == "nt"),
        pure=PureWindowsPath if windows else PurePosixPath,
    )


def diagnose_boundary(
    *,
    exit_code: int | None,
    stderr: str,
    command: str | None,
    cwd: Path | str | None,
    write_roots: Sequence[Path | str],
    mechanism: str,
    platform: str = sys.platform,
    seam: Seam = "shell",
    narrowed: bool = False,
) -> BoundaryDiagnosis | None:
    """Diagnose one failed confined call; ``None`` when the fence did not cause it.

    ``write_roots`` must be the roots the seam gave the fence, so the diagnosis can never
    disagree with the territory that was enforced. ``mechanism`` is the fence that ran
    (``none`` on the floor, which never yields a diagnosis). ``seam`` picks the retry the
    message names: a shell command retries with ``access_paths``; a script or another
    tool asks for the folder through a shell call, since only the shell declares paths.
    ``narrowed`` says the territory was cut down for this call (plan mode, a read-only
    task, a workflow step's own folder), where no folder grant widens it, so the message
    names the folders the call may write instead of a retry. An exit code of 0 is no
    failure and gets nothing.
    """
    if not mechanism or mechanism == "none" or exit_code == 0:
        return None
    text = (stderr or "")[-_MAX_SCAN_CHARS:]
    if exit_code == ELEVATION_EXIT_CODE or _ELEVATION_TEXT.search(text):
        return BoundaryDiagnosis(
            kind=KIND_ELEVATION_REQUIRED,
            confidence=CONFIRMED,
            mechanism=mechanism,
            message=_elevation_message(),
            signature="ERROR_ELEVATION_REQUIRED",
        )
    if (
        platform == "darwin"
        and exit_code in _ABORT_EXIT_CODES
        and _OFFICE_COMMAND.search(command or "")
    ):
        return BoundaryDiagnosis(
            kind=KIND_OFFICE_IN_SHELL,
            confidence=CONFIRMED,
            mechanism=mechanism,
            message=_office_message(),
            signature="SIGABRT",
        )
    signature, lines = _denial_lines(text)
    if not signature:
        return None
    flavor = _flavor(platform)
    candidates = _line_candidates(lines, flavor) or _command_candidates(command or "", flavor)
    if not candidates:
        return BoundaryDiagnosis(
            kind=KIND_WRITE_DENIED,
            confidence=POSSIBLE,
            mechanism=mechanism,
            message=_narrowed_message(write_roots, POSSIBLE)
            if narrowed
            else _possible_message(seam),
            signature=signature,
        )
    resolved = _dedupe(_resolve(c, cwd, flavor) for c in candidates)
    outside = [p for p in resolved if not within_roots(p, write_roots, platform=platform)]
    blocked = [p for p in outside if _person_could_write(p, flavor)]
    if not blocked:
        return None  # an ordinary permission error inside the territory, or outside the fence
    if narrowed:
        return BoundaryDiagnosis(
            kind=KIND_WRITE_DENIED,
            confidence=CONFIRMED,
            mechanism=mechanism,
            paths=tuple(blocked),
            message=_narrowed_message(write_roots, CONFIRMED),
            signature=signature,
        )
    roots = _dedupe(nearest_existing_parent(p, platform=platform) for p in blocked)
    return BoundaryDiagnosis(
        kind=KIND_WRITE_DENIED,
        confidence=CONFIRMED,
        mechanism=mechanism,
        paths=tuple(blocked),
        request_roots=tuple(roots),
        message=write_denied_message(roots, seam=seam),
        signature=signature,
    )


def diagnose_confined(
    confinement: Any,
    *,
    exit_code: int | None,
    stderr: str,
    command: str | None,
    cwd: Path | str | None,
    seam: Seam = "shell",
    narrowed: bool | None = None,
) -> dict[str, Any] | None:
    """The ``boundary`` payload for a seam holding its spawn's ``SandboxResult``.

    Reads the mechanism and the write roots the seam gave :func:`sandbox.wrap_confined`
    (``details["write_roots"]``) from ``confinement``; ``None`` when there is nothing.
    ``narrowed`` defaults to the active tool scope (a read-only task or a workflow step's
    folder, the narrowing :func:`sandbox_roots.effective_write_roots` applied).
    """
    if confinement is None or not getattr(confinement, "active", False):
        return None
    details = getattr(confinement, "details", None) or {}
    diagnosis = diagnose_boundary(
        exit_code=exit_code,
        stderr=stderr,
        command=command,
        cwd=cwd,
        write_roots=tuple(details.get("write_roots") or ()),
        mechanism=str(getattr(confinement, "mechanism", "") or ""),
        seam=seam,
        narrowed=_scope_narrowed() if narrowed is None else narrowed,
    )
    return diagnosis.to_payload() if diagnosis is not None else None


#: The ``signature`` of a diagnosis made from a call's declared write targets, not its text.
WRITE_TARGET_SIGNATURE = "write_target_outside_territory"


def diagnose_write_targets(
    targets: Sequence[Path | str],
    *,
    write_roots: Sequence[Path | str],
    mechanism: str,
    platform: str = sys.platform,
    seam: Seam = "tool",
) -> BoundaryDiagnosis | None:
    """Diagnose a failed confined call from the local paths it was asked to write.

    A confined stdio MCP server reports its failures as prose, which may describe anything:
    a remote host's ``Permission denied`` reads like a fenced write. This diagnosis reads no
    text. It needs a fence that ran (``mechanism`` not ``none``) and absolute local
    ``targets`` the call's arguments name as writes; only targets outside ``write_roots``
    that the person's own account could write stay. ``None`` when none does.
    """
    if not mechanism or mechanism == "none":
        return None
    flavor = _flavor(platform)
    absolute = [str(t) for t in targets if str(t) and flavor.absolute(str(t))]
    resolved = _dedupe(_resolve(t, None, flavor) for t in absolute)
    outside = [p for p in resolved if not within_roots(p, write_roots, platform=platform)]
    blocked = [p for p in outside if _person_could_write(p, flavor)]
    if not blocked:
        return None
    roots = _dedupe(nearest_existing_parent(p, platform=platform) for p in blocked)
    return BoundaryDiagnosis(
        kind=KIND_WRITE_DENIED,
        confidence=CONFIRMED,
        mechanism=mechanism,
        paths=tuple(blocked),
        request_roots=tuple(roots),
        message=write_denied_message(roots, seam=seam),
        signature=WRITE_TARGET_SIGNATURE,
    )


def _scope_narrowed() -> bool:
    from clio_agent.tools.write_scope import (  # noqa: PLC0415 - avoid import cycle
        active_tool_read_only,
        active_tool_write_root,
    )

    return active_tool_read_only() or active_tool_write_root() is not None


def hedged(diagnosis: BoundaryDiagnosis, seam: Seam = "tool") -> BoundaryDiagnosis:
    """The same diagnosis at ``possible`` confidence (roots recomputed, not the seam's)."""
    if diagnosis.confidence == POSSIBLE or diagnosis.kind != KIND_WRITE_DENIED:
        return diagnosis
    return BoundaryDiagnosis(
        kind=diagnosis.kind,
        confidence=POSSIBLE,
        mechanism=diagnosis.mechanism,
        paths=diagnosis.paths,
        request_roots=diagnosis.request_roots,
        message=_possible_message(seam, diagnosis.request_roots),
        signature=diagnosis.signature,
    )


# --------------------------------------------------------------------------- #
# Territory: the same normalization the fence's roots get.                     #
# --------------------------------------------------------------------------- #


def within_roots(
    path: Path | str, roots: Sequence[Path | str], *, platform: str = sys.platform
) -> bool:
    """Whether ``path`` lies inside any of ``roots`` (symlinks resolved on this host).

    Paths from another platform's flavor (a Windows denial read on a POSIX test host) are
    compared lexically, case-insensitively for Windows.
    """
    flavor = _flavor(platform)
    if flavor.native:
        target = _resolved(Path(path))
        return any(target.is_relative_to(_resolved(Path(root))) for root in roots)
    pure = flavor.pure(str(path))
    return any(pure.is_relative_to(flavor.pure(str(root))) for root in roots)


def nearest_existing_parent(path: str, *, platform: str = sys.platform) -> str:
    """The folder to ask for: ``path`` itself when it is a directory, else its nearest
    existing parent (a grant on a path that does not exist widens nothing). Off this
    host's flavor, or when nothing exists, the immediate parent."""
    flavor = _flavor(platform)
    pure = flavor.pure(path)
    if flavor.native:
        for candidate in (Path(path), *Path(path).parents):
            if _is_dir(candidate):
                return str(candidate)
    return str(pure.parent)


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return path


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _person_could_write(path: str, flavor: _Flavor) -> bool:
    """Whether the server's own account may write there (POSIX on this host only).

    The fence confines the child, never the server, so a folder the server could not write
    either is an ordinary file permission (``/etc``) that no grant would fix. Windows
    ``os.access`` ignores ACLs, so there every candidate stays.
    """
    if not flavor.native or flavor.windows:
        return True
    parent = Path(nearest_existing_parent(path))
    return not _is_dir(parent) or os.access(parent, os.W_OK)


# --------------------------------------------------------------------------- #
# Signatures and candidate paths.                                              #
# --------------------------------------------------------------------------- #


def _denial_lines(text: str) -> tuple[str, list[str]]:
    """The first matching signature's name and every line that matches any signature."""
    first = ""
    lines: list[str] = []
    for line in text.splitlines():
        for name, pattern in DENIAL_SIGNATURES:
            if pattern.search(line):
                first = first or name
                lines.append(line)
                break
    return first, lines


def _line_candidates(lines: Sequence[str], flavor: _Flavor) -> list[str]:
    """Paths named in the denial lines: an explicit path group, quoted text, then the
    token right before the OS error text."""
    found: list[str] = []
    for line in lines:
        for _name, pattern in DENIAL_SIGNATURES:
            match = pattern.search(line)
            if match and "path" in pattern.groupindex:
                found.append(match.group("path"))
        found.extend(q for q in _quoted(line) if _path_like(q, flavor, quoted=True))
        if flavor.windows:
            found.extend(m.group("path") for m in _CATEGORY_TARGET.finditer(line))
    if not found:
        for line in lines:
            for match in _BEFORE_STRERROR.finditer(line):
                token = match.group(1).strip(_TRIM)
                if _path_like(token, flavor, quoted=False):
                    found.append(token)
    return found


def _command_candidates(command: str, flavor: _Flavor) -> list[str]:
    """Path-like tokens in the command (quoted strings, then bare words)."""
    found = [q for q in _quoted(command) if _path_like(q, flavor, quoted=True)]
    bare = _QUOTED.sub(" ", command)
    for word in bare.split():
        token = word.split("=", 1)[-1].lstrip("0123456789>&<|").strip(_TRIM)
        if _path_like(token, flavor, quoted=False):
            found.append(token)
    return found


def _quoted(text: str) -> list[str]:
    return [m.group(1) for kind in _QUOTE_KINDS for m in kind.finditer(text)]


def _path_like(token: str, flavor: _Flavor, *, quoted: bool) -> bool:
    if not token or "://" in token or len(token) > 1024 or token.startswith("-"):
        return False
    if flavor.absolute(token) or token.startswith("~"):
        return True
    if " " in token:
        return False
    separators = ("/", "\\") if flavor.windows else ("/",)
    if any(sep in token for sep in separators):
        return True
    return quoted and bool(re.fullmatch(r"[^/\\]+\.\w{1,8}", token))


def _resolve(candidate: str, cwd: Path | str | None, flavor: _Flavor) -> str:
    text = candidate
    if text.startswith("~") and flavor.native:
        text = str(Path(text).expanduser())
    if flavor.absolute(text) or cwd is None:
        return str(flavor.pure(text))
    return str(flavor.pure(str(cwd)) / text)


def _dedupe(items: Any) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


# --------------------------------------------------------------------------- #
# Messages: plain instructions to the model.                                   #
# --------------------------------------------------------------------------- #


def _folders(roots: Sequence[str]) -> str:
    if len(roots) == 1:
        return f"{roots[0]} is not a permitted folder"
    return f"{', '.join(roots[:-1])} and {roots[-1]} are not permitted folders"


def write_denied_message(roots: Sequence[str], *, seam: Seam = "shell") -> str:
    """What the model reads when the fence blocked a write: the folders and the retry."""
    access = f"access_paths={json.dumps(list(roots))}"
    if seam == "shell":
        retry = f"Run the command again with {access} and an access_reason"
    else:
        retry = (
            f"Scripts cannot ask for folders: do this write with a shell_bash call that "
            f"declares {access} and an access_reason"
        )
    return (
        f"CLIO's sandbox blocked this write. {_folders(roots)}. {retry}; CLIO will ask the "
        "person to approve the folder first. Check whether the command partly ran before "
        "retrying."
    )


def _possible_message(seam: Seam, roots: Sequence[str] = ()) -> str:
    if roots:
        return (
            f"This failure may have been caused by CLIO's sandbox: {_folders(roots)}. If "
            f"this call writes there, request it by calling shell_bash with access_paths="
            f"{json.dumps(list(roots))} and an access_reason; CLIO will ask the person to "
            "approve the folder first."
        )
    where = "the command" if seam == "shell" else "the script" if seam == "script" else "it"
    return (
        "This failure may have been caused by CLIO's sandbox, but CLIO couldn't tell which "
        f"path was blocked. If {where} writes outside the workspace, retry with access_paths "
        "naming that folder."
    )


def _narrowed_message(write_roots: Sequence[Path | str], confidence: str) -> str:
    folders = ", ".join(str(root) for root in write_roots) or "no folder"
    lead = (
        "CLIO's sandbox blocked this write"
        if confidence == CONFIRMED
        else "This failure may have been caused by CLIO's sandbox"
    )
    return (
        f"{lead}. This call may write only in {folders} (plan mode, a read-only task or a "
        "workflow step's own folder), and a folder grant does not widen that. Write inside "
        "those folders instead."
    )


def _office_message() -> str:
    return (
        "On macOS, LibreOffice can't start inside CLIO's shell sandbox, so don't retry soffice "
        "here. To convert an Office file to PDF or see how it looks, call prepare_document "
        "with action='render' on it: the result's 'pdf' field is the converted PDF. For an "
        "XLSX recalculation, use action='recalculate'."
    )


def _elevation_message() -> str:
    return (
        "This needs administrator rights. CLIO's sandbox can't elevate a command, so no "
        "permission prompt can be shown from here. Tell the person the exact file to run and "
        "that Windows will ask them to approve the change. Don't try other ways to elevate."
    )


# --------------------------------------------------------------------------- #
# The model lane for a seam that cannot attach the field itself (stdio MCP).   #
# --------------------------------------------------------------------------- #

_CALL_BOUNDARY: ContextVar[tuple[str, BoundaryDiagnosis] | None] = ContextVar(
    "call_boundary", default=None
)


def publish_call_boundary(call_id: str, diagnosis: BoundaryDiagnosis) -> None:
    """Publish a diagnosis made after the call (a stdio MCP server's error) for the model
    observation of ``call_id``, as the artifact identity is published."""
    _CALL_BOUNDARY.set((str(call_id or ""), diagnosis))


def take_call_boundary(call_id: str) -> BoundaryDiagnosis | None:
    """Read and clear the diagnosis published for ``call_id`` (consume-once)."""
    published = _CALL_BOUNDARY.get()
    _CALL_BOUNDARY.set(None)
    if not published or not call_id or published[0] != str(call_id):
        return None
    return published[1]


def result_boundary(result: Any) -> BoundaryDiagnosis | None:
    """The ``boundary`` field a confined seam attached to ``result`` (top level, or in an
    MCP result's structured content); ``None`` when there is none."""
    if not isinstance(result, Mapping):
        return None
    found = BoundaryDiagnosis.from_payload(result.get("boundary"))
    if found is not None:
        return found
    for key in ("structuredContent", "structured_content"):
        nested = result.get(key)
        if isinstance(nested, Mapping):
            found = BoundaryDiagnosis.from_payload(nested.get("boundary"))
            if found is not None:
                return found
    return None


def result_text(result: Any, *, _depth: int = 0) -> str:
    """The string leaves of a (nested) tool result joined by lines, bounded in depth."""
    if isinstance(result, str):
        return result
    if _depth > 6:
        return ""
    if isinstance(result, Mapping):
        values: Any = result.values()
    elif isinstance(result, (list, tuple)):
        values = result
    else:
        return ""
    parts = (result_text(value, _depth=_depth + 1) for value in values)
    return "\n".join(part for part in parts if part)


__all__ = [
    "CONFIRMED",
    "DENIAL_SIGNATURES",
    "ELEVATION_EXIT_CODE",
    "KIND_ELEVATION_REQUIRED",
    "KIND_OFFICE_IN_SHELL",
    "KIND_WRITE_DENIED",
    "POSSIBLE",
    "WRITE_TARGET_SIGNATURE",
    "BoundaryDiagnosis",
    "diagnose_boundary",
    "diagnose_confined",
    "diagnose_write_targets",
    "hedged",
    "nearest_existing_parent",
    "publish_call_boundary",
    "result_boundary",
    "result_text",
    "take_call_boundary",
    "within_roots",
    "write_denied_message",
]
