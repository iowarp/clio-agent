"""Directory policy for bounded workspace browsing."""

from __future__ import annotations

import mimetypes
import re
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


_SECRET_LIKE_FILENAME = re.compile(r"token|credential", re.IGNORECASE)


def workspace_read_redaction_reason(relative_path: Path) -> str | None:
    """Return a typed reason to refuse serving ``relative_path``'s raw bytes, or ``None``.

    Security review (S3 follow-up): the Files view LISTING shows every dot file/folder,
    ``.clio`` included (owner ruling: no reason to hide a workspace's own state from
    itself) — but the raw-bytes SERVE (``GET /files/read``) is a narrower boundary, by
    NAME rather than content-sniffing:

    - ``.clio-child-cache`` is the redirected ``APPDATA``/``TEMP``/``XDG_CACHE_HOME``
      home for sandboxed MCP child processes (:data:`clio_agent.runtime.sandbox.
      CHILD_CACHE_DIRNAME`); it can hold package-manager or provider credential caches
      that were never meant to leave the sandbox.
    - The workspace config file (``.clio/config.yaml``) is documented as env-only for
      secrets (``conf.py``'s secret tier), but that is a WRITER-side policy, not an
      enforced constraint on the file's contents — refuse it as a byte-serve
      regardless, defense in depth.
    - Any other filename that looks credential-shaped (``*token*``, ``*credential*``)
      is refused on the same NAME-based principle: cheap, honest, and never bypassed
      by a file that happens to be plain text.

    None of this hides the path from the LISTING — only the raw-byte read is refused.
    """

    parts = relative_path.parts
    if CHILD_CACHE_DIRNAME in parts:
        return "sandbox_child_cache"
    if relative_path.name == "config.yaml" and ".clio" in parts:
        return "workspace_secret_config"
    if _SECRET_LIKE_FILENAME.search(relative_path.name):
        return "credential_like_filename"
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
