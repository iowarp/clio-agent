"""Explicit trust for a workspace's ``mcp.yaml``.

A workspace ``mcp.yaml`` (``<workspace>/.clio-agent/shared/mcp.yaml``) declares
MCP servers, and a stdio server is a command CLIO starts. The workspace is often
a repository someone else wrote, so its servers are not mounted until the person
trusts that file's exact content. Trust is a SHA-256 of the file's bytes, kept
per file in ``trusted-workspace-mcp.json`` in the user's config directory,
outside any workspace. Editing the file (a ``git pull``, a branch switch) needs
trusting again.

An untrusted file is skipped with a logged reason and recorded for the MCP
listing's ``degradations`` (:func:`untrusted_workspace_mcp_snapshot`), which
names the command that trusts it: ``clio-agent mcp trust``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from clio_agent.platform_paths import atomic_write_text

logger = logging.getLogger(__name__)

WORKSPACE_MCP_UNTRUSTED = "workspace_mcp_untrusted"
TRUST_FILE_NAME = "trusted-workspace-mcp.json"

_UNTRUSTED: list[dict[str, str]] = []
_UNTRUSTED_LOCK = threading.Lock()


@dataclass(frozen=True)
class WorkspaceMcpFile:
    """One workspace ``mcp.yaml`` and whether its current content is trusted."""

    path: Path
    digest: str
    trusted: bool


def trust_store_path(home: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Where trusted workspace ``mcp.yaml`` digests are kept (the user config dir)."""
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    return paths.user_config_dir_for(home or Path.home(), env or os.environ) / TRUST_FILE_NAME


def _key(path: Path) -> str:
    return str(path.resolve())


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(store: Path) -> dict[str, str]:
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("workspace mcp trust store unreadable path=%s error=%s", store, exc)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def inspect_workspace_mcp(
    path: Path, *, home: Path | None = None, env: Mapping[str, str] | None = None
) -> WorkspaceMcpFile | None:
    """Return ``path``'s digest and trust state, or ``None`` when it does not exist."""
    if not path.is_file():
        return None
    digest = _digest(path)
    trusted = _load(trust_store_path(home, env)).get(_key(path)) == digest
    return WorkspaceMcpFile(path=path, digest=digest, trusted=trusted)


def workspace_mcp_trusted(
    path: Path, *, home: Path | None = None, env: Mapping[str, str] | None = None
) -> bool:
    """Whether ``path`` may be merged: absent, or its content is trusted.

    An untrusted file is logged with ``reason=workspace_mcp_untrusted`` and recorded
    for :func:`untrusted_workspace_mcp_snapshot`.
    """
    state = inspect_workspace_mcp(path, home=home, env=env)
    with _UNTRUSTED_LOCK:
        _UNTRUSTED.clear()  # this snapshot describes the latest discovery pass
    if state is None or state.trusted:
        return True
    logger.warning(
        "workspace mcp.yaml not loaded reason=%s path=%s sha256=%s; "
        "review it, then run `clio-agent mcp trust` in that workspace",
        WORKSPACE_MCP_UNTRUSTED,
        path,
        state.digest,
    )
    with _UNTRUSTED_LOCK:
        _UNTRUSTED.append({"path": str(path), "sha256": state.digest})
    return False


def untrusted_workspace_mcp_snapshot() -> list[dict[str, str]]:
    """``{"path", "sha256"}`` rows skipped by the last ``load_mcp_servers`` call."""
    with _UNTRUSTED_LOCK:
        return list(_UNTRUSTED)


def trust_workspace_mcp(
    path: Path,
    *,
    expected_digest: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> WorkspaceMcpFile:
    """Trust ``path``'s current content.

    Args:
        path: The workspace ``mcp.yaml``.
        expected_digest: The digest the person reviewed; when given, the file must
            still have it, so a file changed after review is not trusted.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ValueError: if the content no longer matches ``expected_digest``.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = _digest(path)
    if expected_digest is not None and expected_digest != digest:
        raise ValueError(f"{path} changed since it was reviewed (sha256 is now {digest})")
    store = trust_store_path(home, env)
    trusted = _load(store)
    trusted[_key(path)] = digest
    store.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(store, json.dumps(trusted, indent=2, sort_keys=True) + "\n", file_mode=0o600)
    with _UNTRUSTED_LOCK:
        _UNTRUSTED[:] = [row for row in _UNTRUSTED if row["path"] != str(path)]
    return WorkspaceMcpFile(path=path, digest=digest, trusted=True)
