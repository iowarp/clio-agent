"""Bounded, read-only inspection of an installed plugin's own source files."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from clio_agent.plugins.records import PluginRecord

TEXT_SUFFIXES = frozenset(
    {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".toml", ".js", ".ts", ".tsx", ".html", ".css"}
)
EXCLUDED_DIRECTORIES = frozenset({".git", ".venv", "node_modules", "__pycache__"})
MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 512 * 1024
MAX_FILES = 128
MAX_DIRECTORIES = 256


def inspect_plugin_files(
    record: PluginRecord, *, inherited_root: Path | None = None
) -> dict[str, Any]:
    """Read text within this record's root without importing or executing source.

    Limits and unreadable files are explicit. Symlinked files/directories never
    extend the installed record's root into unrelated filesystem content.
    """
    root = Path(record.root).resolve()
    inherited: dict[str, Path] = {}
    if inherited_root is not None:
        for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE", "NOTICE.md", "COPYING"):
            candidate = inherited_root / name
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and candidate.resolve().is_relative_to(inherited_root.resolve())
                and not (root / name).exists()
            ):
                inherited[name] = candidate
    files: list[dict[str, Any]] = []
    total = 0
    visited = 0
    incomplete = False
    if not root.is_dir():
        return {"files": files, "incomplete": True, "reason": "Plugin source is unavailable."}

    def unreadable_directory(_error: OSError) -> None:
        nonlocal incomplete
        incomplete = True

    for folder, directories, names in os.walk(
        root, followlinks=False, onerror=unreadable_directory
    ):
        visited += 1
        if visited > MAX_DIRECTORIES:
            incomplete = True
            break
        directories[:] = sorted(
            name
            for name in directories
            if name not in EXCLUDED_DIRECTORIES
            and not (Path(folder) / name).is_symlink()
            and (Path(folder) / name).resolve().is_relative_to(root)
        )
        for name in sorted(names):
            path = Path(folder) / name
            if (
                path.suffix.lower() not in TEXT_SUFFIXES
                and name not in {"LICENSE", "NOTICE", "COPYING"}
                or path.is_symlink()
            ):
                incomplete = True
                continue
            if not path.resolve().is_relative_to(root):
                continue
            if len(files) >= MAX_FILES or total >= MAX_TOTAL_BYTES:
                incomplete = True
                break
            relative = path.relative_to(root).as_posix()
            try:
                size = path.stat().st_size
                budget = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total)
                with path.open("rb") as source:
                    raw = source.read(budget)
                truncated = size > len(raw)
                text = raw.decode("utf-8", errors="replace" if truncated else "strict")
            except (OSError, UnicodeDecodeError):
                files.append(
                    {
                        "path": relative,
                        "text": "",
                        "bytes": 0,
                        "truncated": False,
                        "sha256": "",
                        "error": "This file could not be read as text.",
                    }
                )
                incomplete = True
                continue
            total += len(raw)
            incomplete |= truncated
            files.append(
                {
                    "path": relative,
                    "text": text,
                    "bytes": size,
                    "truncated": truncated,
                    "sha256": hashlib.sha256(raw).hexdigest() if not truncated else "",
                    "error": "",
                }
            )
        if len(files) >= MAX_FILES or total >= MAX_TOTAL_BYTES:
            incomplete = True
            break
    # A skill-only local variation must retain the source's license/notice,
    # even when the marketplace keeps those beside the skill directory.
    for name, path in inherited.items():
        try:
            if len(files) >= MAX_FILES or path.stat().st_size > min(
                MAX_FILE_BYTES, MAX_TOTAL_BYTES - total
            ):
                incomplete = True
                continue
            budget = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total)
            with path.open("rb") as source:
                raw = source.read(budget + 1)
            if len(raw) > budget:
                incomplete = True
                continue
            files.append(
                {
                    "path": name,
                    "text": raw.decode("utf-8"),
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "truncated": False,
                    "error": "",
                }
            )
            total += len(raw)
        except (OSError, UnicodeDecodeError):
            incomplete = True
    return {"files": files, "incomplete": incomplete, "reason": ""}
