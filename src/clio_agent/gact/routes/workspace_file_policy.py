"""Directory policy for bounded workspace browsing."""

from __future__ import annotations

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


def skip_workspace_file_directory(name: str) -> bool:
    """Return whether a directory is service-owned or too costly to browse."""

    return name in FILE_PICKER_SKIP_DIRS or name == ".clio" or name.startswith(".clio-")
