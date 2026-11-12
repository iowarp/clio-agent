"""Portable local names for provider entries, keeping each provider's own name.

Providers such as Google Drive, SFTP, Globus and GitHub allow names that cannot
be written safely on every CLIO host (``<<REVIEW QUESTIONS>>.pdf``, ``a:b``,
``CON.txt``). Such an entry is listed under a deterministic portable alias; the
original name stays in ``FileEntry.remote_path`` for display and provider reads.
Traversal (``.``/``..``), empty names and absolute paths are never mapped.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath

UNSAFE_CHARACTERS = frozenset('<>:"|?*')
RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}
)
UNSAFE_PATH = "Source entry has an unsafe relative path"
_ALIAS = re.compile(r"~[0-9a-f]{8}(?:\..*)?$")
_MAX_STEM = 200


def reserved_part(part: str) -> bool:
    """Whether Windows treats this name as a device on every directory."""
    return part.split(".", 1)[0].upper() in RESERVED_NAMES


def portable_part(part: str) -> bool:
    """Whether one name component can be written on Windows, macOS and Linux."""
    return bool(
        part
        and part not in {".", ".."}
        and "/" not in part
        and "\\" not in part
        and not any(ord(char) < 32 or char in UNSAFE_CHARACTERS for char in part)
        and not part.endswith((" ", "."))
        and not reserved_part(part)
    )


def portable_name(name: str, key: str = "") -> str | None:
    """Return ``name`` when portable, else a deterministic alias; None if unrepresentable.

    The alias replaces unsafe characters with ``_`` and adds ``~`` plus eight hex
    digits of the original name's SHA-256 before the extension, so distinct
    provider names never share an alias in practice and the extension still
    identifies the file type. A ``key`` (such as a provider file ID) forces an
    alias that also distinguishes same-named siblings.
    """
    if name in {"", ".", ".."} or any(0xD800 <= ord(char) <= 0xDFFF for char in name):
        return None
    if portable_part(name) and not key:
        return name
    clean = "".join(
        "_" if ord(char) < 32 or char in UNSAFE_CHARACTERS or char in "/\\" else char
        for char in name
    ).rstrip(" .")
    clean = clean or "_"
    dot = clean.find(".", 1)
    stem, suffix = (clean, "") if dot < 0 else (clean[:dot], clean[dot:])
    if len(suffix) > 32:
        stem, suffix = clean, ""
    digest = hashlib.sha256(f"{name}\0{key}".encode() if key else name.encode()).hexdigest()[:8]
    return f"{stem[:_MAX_STEM]}~{digest}{suffix}"


def listed_child(
    parent: str, remote_parent: str, name: str, key: str = ""
) -> tuple[str, str] | None:
    """Return the portable and provider paths of one listed name, or None to skip it.

    ``parent`` is the portable path of the listed folder and ``remote_parent``
    its provider path. A name that would mean traversal is never listed.
    """
    alias = portable_name(name, key)
    if alias is None:
        return None
    remote = posixpath.join(remote_parent, name) if remote_parent else name
    try:
        check_remote_relative(remote)
    except ValueError:
        return None
    return posixpath.join(parent, alias), remote


def check_remote_relative(value: str) -> str:
    """Accept a provider-relative path only when it stays inside the selected root."""
    if (
        not value
        or value.startswith("/")
        or "\0" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(UNSAFE_PATH)
    return value


def resolve_remote(path: str, children: Callable[[str], Iterable[str]]) -> str:
    """Translate a portable source path back to the provider's own names.

    Only components shaped like an alias are looked up, by listing their provider
    parent with ``children(remote_parent)``. Every other component is already the
    provider's name, because a portable name is never aliased. Siblings keyed by their
    own name resolve too; siblings keyed by a provider ID are only reachable through
    their listed ``remote_path``.
    """
    remote: list[str] = []
    for part in PurePosixPath(path).parts:
        if _ALIAS.search(part):
            try:
                names = list(children("/".join(remote)))
            except FileNotFoundError:
                names = []
            part = next(
                (
                    name
                    for name in names
                    if part in {portable_name(name), portable_name(name, name)}
                ),
                part,
            )
        remote.append(part)
    return check_remote_relative("/".join(remote))


def plain_error(exc: BaseException) -> str:
    """Return a user-facing sentence, never pydantic's multi-line validation report."""
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        errors = exc.errors(include_url=False)
        message = str(errors[0].get("msg", "")) if errors else ""
        return message.removeprefix("Value error, ") or "A source entry is not valid"
    return str(exc)


def skipped_notice(count: int) -> str:
    """Explain entries left out of a listing without failing the whole source."""
    items = "1 item" if count == 1 else f"{count} items"
    return (
        f"{items} could not be listed, such as Google Forms, shortcuts or names that are "
        "not valid file paths. Everything else in this source is available."
    )
