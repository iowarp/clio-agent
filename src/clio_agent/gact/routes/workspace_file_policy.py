"""Directory policy for bounded workspace browsing."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from clio_agent.runtime.sandbox import CHILD_CACHE_DIRNAME

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


#: Cache/staging directory NAMES CLIO itself creates under a workspace root
#: whose contents are never safe to serve as raw bytes — an EXPLICIT, closed
#: set, never a ``.clio-*`` glob or a keyword match on the name (CLAUDE.md
#: superseding principle #1/#2: no keyword/heuristic decisions in core). As of
#: this writing the only one is the sandboxed MCP child cache — the redirected
#: ``APPDATA``/``TEMP``/``XDG_CACHE_HOME`` home for confined child processes
#: (:data:`clio_agent.runtime.sandbox.CHILD_CACHE_DIRNAME` /
#: ``_child_cache_env``), which can hold package-manager or provider
#: credential caches that were never meant to leave the sandbox. Casefolded so
#: the comparison is stable on case-insensitive filesystems (macOS APFS,
#: Windows).
_SERVICE_CACHE_DIRECTORY_NAMES = frozenset({CHILD_CACHE_DIRNAME.casefold()})

#: The workspace's OWN config file, at its one canonical, root-relative
#: location — ``<workspace root>/.clio/config.yaml`` — and nowhere else. A
#: ``config.yaml`` found at any other depth (a user's own nested project, a
#: vendored dependency) is an ordinary workspace file, not CLIO's. The secret
#: tier is documented as env-only by WRITER policy (``conf.py``), which is not
#: an enforced constraint on the file's contents, so this refuses it as a
#: byte-serve regardless — defense in depth, not a guess at what it contains.
_WORKSPACE_CONFIG_RELATIVE_PARTS = (".clio", "config.yaml")


def workspace_read_redaction_reason(relative_path: Path) -> str | None:
    """Return a typed reason to refuse serving ``relative_path``'s raw bytes, or ``None``.

    Security review: the Files view LISTING shows every dot file/folder, ``.clio``
    included (owner ruling: no reason to hide a workspace's own state from itself) —
    but the raw-bytes SERVE (``GET /files/read``) is a narrower, EXPLICIT boundary
    scoped to CLIO's own storage, matched case-insensitively. It is not a general
    secret-detection preview guard: it does not scan file contents, and a genuine
    secret dropped anywhere else in the workspace is not caught by this check.
    """

    parts = tuple(part.casefold() for part in relative_path.parts)
    if any(part in _SERVICE_CACHE_DIRECTORY_NAMES for part in parts):
        return "sandbox_child_cache"
    if parts == _WORKSPACE_CONFIG_RELATIVE_PARTS:
        return "workspace_secret_config"
    return None


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
    "workspace_read_redaction_reason",
]
