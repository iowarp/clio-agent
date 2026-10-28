"""Recorded absolute paths that outlive a move of the folder holding them (owner module).

CLIO's files record absolute paths into its own roots: plugin records point into
snapshots, service records name their service directories, learned MCP launch plans
name environment interpreters, and a Python virtual environment names itself in its
console scripts and activation scripts. When the folder holding them is moved (a
backup restored elsewhere, a renamed home, a moved ``CLIO_AGENT_HOME``), those paths
still name the OLD location, and following them reads or runs another copy.

This module holds the shared mechanics; each store decides when to apply them:

* :func:`relocate_text` / :func:`relocate_json_file` rewrite every whole path under an
  old root (either separator) to the same path under the new root;
* :func:`venv_recorded_path` / :func:`venv_is_stale` read the location a virtual
  environment was built at. A stale environment is rebuilt, never run: its scripts
  start the interpreter of the old location;
* :func:`record` / :func:`recorded` keep this process's relocations for
  ``/v1/health`` (the ``boot`` block's ``relocations``), each also logged with its
  typed reason.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

Moves = Sequence[tuple[str, str]]

_lock = threading.Lock()
_records: list[dict[str, Any]] = []

#: The location a virtual environment was created at, from its activation scripts:
#: ``VIRTUAL_ENV='<path>'`` (POSIX) or ``@for %%i in ("<path>") do @set "VIRTUAL_ENV=``
#: (Windows, uv).
_ACTIVATE = (
    ("bin/activate", re.compile(r"^VIRTUAL_ENV=(['\"]?)(?P<path>[^'\"$\r\n]+)\1\s*$", re.M)),
    (
        "Scripts/activate.bat",
        re.compile(r"(?:@for %%i in \(\"(?P<path>[^\"]+)\"\) do @set \"VIRTUAL_ENV=)", re.I),
    ),
)


def _under(path: str, root: str) -> str | None:
    """``path`` relative to ``root`` (``""`` for ``root`` itself), or ``None``."""
    root = root.rstrip("/\\")
    if path == root:
        return ""
    if path.startswith(root) and path[len(root)] in "/\\":
        return path[len(root) :]
    return None


def relocated(path: str, moves: Moves) -> str:
    """``path`` under its new root when an old root holds it, else unchanged.

    When roots nest (a new root inside an old one, as ``<data>/cte/hosts/<host>`` lies
    inside ``<data>/cte``), the deepest root holding the path decides.
    """
    best: tuple[str, str | None, str] | None = None
    for old, new in moves:
        for root, target in ((old, new), (new, None)):
            rest = _under(path, root)
            if rest is not None and (best is None or len(root) > len(best[0])):
                best = (root, target, rest)
    if best is None or best[1] is None:
        return path
    return best[1].rstrip("/\\") + best[2]


def relocate_text(text: str, moves: Moves) -> str:
    """Every whole path under an old root in ``text`` (never a sibling such as
    ``<old>-backup``) rewritten to its new root."""
    for old, _new in moves:
        pattern = re.compile(
            r"(?<![^\s\"'=(])" + re.escape(old.rstrip("/\\")) + r"(?:[/\\][^\s\"']*)?(?![^\s\"')])"
        )
        text = pattern.sub(lambda match: relocated(match[0], moves), text)
    return text


def _relocate_value(value: Any, moves: Moves) -> Any:
    if isinstance(value, str):
        return relocate_text(value, moves)
    if isinstance(value, list):
        return [_relocate_value(item, moves) for item in value]
    if isinstance(value, dict):
        return {relocate_text(k, moves): _relocate_value(v, moves) for k, v in value.items()}
    return value


def atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` in one rename, synced first."""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def relocate_json_file(path: Path, moves: Moves, *, indent: int | None = None) -> bool:
    """Rewrite the paths under an old root in one JSON file; ``True`` when it changed.

    An unreadable file is left as it is: its owner reports it when it reads it.
    """
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return False
    moved = _relocate_value(data, moves)
    if moved == data:
        return False
    atomic_write(path, json.dumps(moved, indent=indent, sort_keys=True).encode("utf-8"))
    return True


def relocate_json_files(paths: Iterable[Path], moves: Moves) -> list[str]:
    """:func:`relocate_json_file` over ``paths`` (the indentation each owner writes)."""
    return [
        str(path) for path in paths if path.is_file() and relocate_json_file(path, moves, indent=1)
    ]


def venv_recorded_path(environment: Path) -> str | None:
    """Where the virtual environment at ``environment`` was created, from its scripts."""
    for name, pattern in _ACTIVATE:
        try:
            text = (environment / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = pattern.search(text)
        if match:
            return match["path"].strip()
    return None


def venv_is_stale(environment: Path) -> bool:
    """Whether the environment was created at another location (moved, or copied).

    Its console scripts then start the OLD location's interpreter, so it must be
    rebuilt before anything runs from it.
    """
    recorded_at = venv_recorded_path(environment)
    if recorded_at is None:
        return False
    return os.path.normcase(os.path.realpath(recorded_at)) != os.path.normcase(
        os.path.realpath(environment)
    )


def record(entry: dict[str, Any], *, log: bool = True) -> None:
    """Keep a relocation for ``/v1/health``; log it with its typed reason unless the
    caller already did."""
    if log:
        logger.warning(
            "reason=%s %s",
            entry.get("reason"),
            " ".join(f"{key}={value}" for key, value in entry.items() if key != "reason"),
        )
    with _lock:
        _records.append(dict(entry))


def recorded() -> list[dict[str, Any]]:
    """The relocations this process made, oldest first."""
    with _lock:
        return [dict(entry) for entry in _records]
