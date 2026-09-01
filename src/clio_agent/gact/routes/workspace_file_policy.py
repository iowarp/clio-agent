"""Directory policy for bounded workspace browsing."""

from __future__ import annotations

import mimetypes

TEXTUAL_WORKSPACE_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-yaml",
        "application/yaml",
        "application/x-sh",
        "application/toml",
    }
)

FILE_PICKER_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".npm",
    ".venv",
    "venv",
    ".tox",
    "build",
    "dist",
    ".egg-info",
}


def is_internal_workspace_file_directory(name: str) -> bool:
    """Return whether a directory is owned by CLIO rather than the user."""

    return name == ".clio" or name.startswith(".clio-")


def skip_workspace_file_directory(name: str) -> bool:
    """Return whether a directory is too costly or unsafe to browse."""

    return name in FILE_PICKER_SKIP_DIRS


def is_textual_workspace_file(name: str, raw: bytes) -> bool:
    """Return whether a workspace file should be served as decoded text."""

    guessed, _ = mimetypes.guess_type(name)
    if guessed is not None:
        return guessed.startswith("text/") or guessed in TEXTUAL_WORKSPACE_MIME_TYPES
    sample = raw[:8192]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


__all__ = [
    "is_internal_workspace_file_directory",
    "is_textual_workspace_file",
    "skip_workspace_file_directory",
]
