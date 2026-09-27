"""An installed Agent Blueprint pack's files: copy, checksum, install metadata.

Owner module for the filesystem work of putting a pack on disk (split out of
``gact/agent_blueprints.py``, file-size ratchet #774). Every call here uses the
extended-length helpers in :mod:`clio_agent.platform_paths`: a pack copied under a
long user dir (a long user name, a packaged app's ``LocalCache`` redirection) or
staged beside a random suffix by the default-registry sync crosses Windows' 260
character limit deep in the tree, where an ordinary path raises ``[WinError 206]
The filename or extension is too long``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from clio_agent.platform_paths import tree_files, win_extended_path

#: The install metadata file at a pack's root (excluded from the pack checksum).
INSTALL_METADATA_NAME = ".clio-install.md"


def write_install_metadata(root: Path, metadata: dict[str, Any]) -> None:
    """Write ``metadata`` as ``key: value`` lines to the pack's install metadata file."""
    lines = ["# CLIO Agent Blueprint install metadata", ""]
    for key, value in metadata.items():
        lines.append(f"{key}: {value}")
    with open(win_extended_path(root / INSTALL_METADATA_NAME), "w", encoding="utf-8") as out:
        out.write("\n".join(lines).strip() + "\n")


def tree_checksum(root: Path) -> str:
    """SHA-256 over every file's relative path and bytes, in sorted order.

    The install metadata file is excluded. The value matches what earlier builds
    recorded (``sorted(root.rglob("*"))``, ``str(path.relative_to(root))``), so an
    existing install's recorded checksum still compares equal.
    """
    digest = hashlib.sha256()
    for relative, os_path in tree_files(root):
        if relative.name == INSTALL_METADATA_NAME:
            continue
        digest.update(str(relative).encode())
        with open(os_path, "rb") as handle:
            digest.update(handle.read())
    return digest.hexdigest()
