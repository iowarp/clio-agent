"""Atomic user-level MCP declaration persistence.

The user ``mcp.yaml`` is shared by every workspace.  This module owns the
read/modify/write transaction so configuration routes never replace unrelated
top-level settings or sibling server declarations.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from clio_agent import paths

_CONFIG_LOCK = threading.RLock()


class McpUserConfigurationError(ValueError):
    """The durable user MCP configuration could not be read or updated safely."""


def user_mcp_configuration_path() -> Path:
    """Return the active user's durable MCP declaration path."""

    return paths.user_config_dir() / "mcp.yaml"


def _read_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise McpUserConfigurationError(f"could not read {path}: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise McpUserConfigurationError(f"{path} must contain a YAML mapping")
    document = dict(loaded)
    servers = document.get("mcp_servers", {})
    if servers is None:
        document["mcp_servers"] = {}
    elif not isinstance(servers, Mapping):
        raise McpUserConfigurationError(f"{path} mcp_servers must be a mapping")
    else:
        document["mcp_servers"] = dict(servers)
    return document


def _write_document(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(
        dict(document),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise McpUserConfigurationError(f"could not write {path}: {exc}") from exc


def get_user_mcp_server(name: str) -> dict[str, Any] | None:
    """Read one named user-level MCP declaration."""

    with _CONFIG_LOCK:
        document = _read_document(user_mcp_configuration_path())
        declaration = document.get("mcp_servers", {}).get(name)
        return dict(declaration) if isinstance(declaration, Mapping) else None


def mcp_server_arg_value(declaration: Mapping[str, Any] | None, flag: str) -> str:
    """Return the value following one exact argv flag in a saved declaration."""

    if not isinstance(declaration, Mapping):
        return ""
    args = declaration.get("args")
    if not isinstance(args, (list, tuple)):
        return ""
    values = [str(value) for value in args]
    try:
        index = values.index(flag)
    except ValueError:
        return ""
    return values[index + 1].strip() if index + 1 < len(values) else ""


def configured_web_remote_url() -> str:
    """Return the durable Web Search endpoint shared by MCP and document ingestion."""

    return mcp_server_arg_value(get_user_mcp_server("web"), "--remote-url")


def set_user_mcp_server(name: str, declaration: Mapping[str, Any]) -> dict[str, Any]:
    """Create or replace one named declaration with an atomic file swap."""

    with _CONFIG_LOCK:
        path = user_mcp_configuration_path()
        document = _read_document(path)
        servers = dict(document.get("mcp_servers", {}))
        servers[name] = dict(declaration)
        document["mcp_servers"] = servers
        _write_document(path, document)
        return dict(servers[name])


def remove_user_mcp_server(name: str) -> bool:
    """Remove one named declaration while preserving every unrelated value."""

    with _CONFIG_LOCK:
        path = user_mcp_configuration_path()
        document = _read_document(path)
        servers = dict(document.get("mcp_servers", {}))
        removed = servers.pop(name, None) is not None
        if not removed:
            return False
        document["mcp_servers"] = servers
        _write_document(path, document)
        return True
