"""Standard MCP server declarations (Claude-Code-style ``mcpServers``).

clio-agent core does not hardcode domain tool servers. Domain/case tools are
ordinary MCP servers — our in-home ones live in clio-kit and are consumed like
any installable MCP — declared by name -> transport, exactly like Claude Code:

    {
      "mcpServers": {
        "ndp": {"command": "uvx", "args": ["--from", "clio-kit", "clio-kit", "mcp-server", "ndp"]},
        "notion": {"type": "http", "url": "https://mcp.notion.com/mcp"}
      }
    }

A marketplace pack declares the servers it needs in a sibling ``mcp.json``; a
user/site may add or override servers at ``~/.config/clio-agent/mcp.json`` (user)
or ``<cwd>/.clio/mcp.json`` (workspace). Precedence (highest wins, by name):
workspace > user > pack > built-in defaults — matching Claude Code's scope model.

This module is dependency-free parsing only; ``transport_for`` is the single
FastMCP-specific glue that turns a spec into a transport the existing
``execution.py`` machinery already accepts.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "MCPServerSpec",
    "MCPConfigError",
    "expand_env",
    "spec_from_entry",
    "load_mcp_servers",
    "transport_for",
]

# Names reserved for clio-agent's built-in universal defaults (always present).
BUILTIN_SERVER_NAMES = frozenset({"fs", "shell", "web"})

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")


class MCPConfigError(ValueError):
    """A declaration could not be parsed (kept on the spec, not raised globally)."""


@dataclass(frozen=True)
class MCPServerSpec:
    """A normalized, transport-agnostic MCP server declaration."""

    name: str
    transport: Literal["stdio", "http"]
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    always_load: bool = False
    timeout_ms: int | None = None
    source: str = ""
    validation_errors: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return not self.validation_errors


def expand_env(value: str, *, env: Mapping[str, str] | None = None) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` against ``env`` (default os.environ).

    Raises ``MCPConfigError`` if a ``${VAR}`` with no default is unset — matching
    Claude Code's "config fails to parse on missing required var" behavior.
    """
    source = os.environ if env is None else env

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        has_default = match.group(2) is not None
        default = match.group(3) if has_default else None
        if name in source and source[name] != "":
            return source[name]
        if has_default:
            return default or ""
        raise MCPConfigError(f"required environment variable ${{{name}}} is unset")

    return _ENV_PATTERN.sub(_sub, value)


def _expand_seq(values: Sequence[Any], *, env: Mapping[str, str] | None) -> tuple[str, ...]:
    return tuple(expand_env(str(v), env=env) for v in values)


def _expand_map(values: Mapping[str, Any], *, env: Mapping[str, str] | None) -> dict[str, str]:
    return {str(k): expand_env(str(v), env=env) for k, v in values.items()}


def spec_from_entry(
    name: str,
    entry: Mapping[str, Any],
    *,
    source: str = "",
    env: Mapping[str, str] | None = None,
) -> MCPServerSpec:
    """Normalize one ``mcpServers`` entry into an ``MCPServerSpec``.

    Parse/validation problems are recorded in ``validation_errors`` (the server is
    returned but marked unusable) rather than raised, so one bad entry never
    breaks the whole config.
    """
    errors: list[str] = []
    declared = str(entry.get("type") or "").strip().lower()
    has_command = bool(entry.get("command"))
    has_url = bool(entry.get("url"))

    if declared in {"http", "streamable-http", "sse"} or (
        not declared and has_url and not has_command
    ):
        transport: Literal["stdio", "http"] = "http"
    else:
        transport = "stdio"

    command = ""
    args: tuple[str, ...] = ()
    env_map: dict[str, str] = {}
    url = ""
    headers: dict[str, str] = {}
    try:
        if transport == "stdio":
            command = expand_env(str(entry.get("command", "")), env=env).strip()
            if not command:
                errors.append("stdio MCP server requires a 'command'")
            raw_args = entry.get("args") or []
            if not isinstance(raw_args, Sequence) or isinstance(raw_args, (str, bytes)):
                errors.append("'args' must be a list")
                raw_args = []
            args = _expand_seq(raw_args, env=env)
            raw_env = entry.get("env") or {}
            if not isinstance(raw_env, Mapping):
                errors.append("'env' must be a mapping")
                raw_env = {}
            env_map = _expand_map(raw_env, env=env)
        else:
            url = expand_env(str(entry.get("url", "")), env=env).strip()
            if not url:
                errors.append("http MCP server requires a 'url'")
            raw_headers = entry.get("headers") or {}
            if not isinstance(raw_headers, Mapping):
                errors.append("'headers' must be a mapping")
                raw_headers = {}
            headers = _expand_map(raw_headers, env=env)
    except MCPConfigError as exc:
        errors.append(str(exc))

    timeout_raw = entry.get("timeout")
    timeout_ms: int | None = None
    if timeout_raw is not None:
        try:
            timeout_ms = int(timeout_raw)
        except (TypeError, ValueError):
            errors.append("'timeout' must be an integer (milliseconds)")

    return MCPServerSpec(
        name=str(name),
        transport=transport,
        command=command,
        args=args,
        env=env_map,
        url=url,
        headers=headers,
        always_load=bool(entry.get("alwaysLoad", False)),
        timeout_ms=timeout_ms,
        source=source,
        validation_errors=tuple(errors),
    )


def _read_mcp_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, Mapping) else None
    return {str(k): v for k, v in servers.items()} if isinstance(servers, Mapping) else {}


def _specs_from_file(
    path: Path, source: str, *, env: Mapping[str, str] | None
) -> dict[str, MCPServerSpec]:
    out: dict[str, MCPServerSpec] = {}
    for name, entry in _read_mcp_json(path).items():
        if isinstance(entry, Mapping):
            out[name] = spec_from_entry(name, entry, source=source, env=env)
    return out


def load_mcp_servers(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    pack_roots: Sequence[Path] = (),
    env: Mapping[str, str] | None = None,
) -> dict[str, MCPServerSpec]:
    """Discover + merge declared MCP servers across all scopes.

    Precedence (highest wins, merged whole by name, Claude-Code-style):
    workspace ``<cwd>/.clio/mcp.json`` > user ``<home>/.config/clio-agent/mcp.json``
    > each pack ``<pack_root>/mcp.json``. Built-in ``fs``/``shell``/``web`` are
    provided directly by core and are not part of this map.
    """
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    lookup = env if env is not None else os.environ
    merged: dict[str, MCPServerSpec] = {}

    # lowest precedence first; later writes override earlier by name
    for root in pack_roots:
        for name, spec in _specs_from_file(
            Path(root) / "mcp.json", f"pack:{Path(root).name}", env=env
        ).items():
            merged[name] = spec
    config_home = Path(lookup.get("XDG_CONFIG_HOME") or (home / ".config"))
    for name, spec in _specs_from_file(
        config_home / "clio-agent" / "mcp.json", "user", env=env
    ).items():
        merged[name] = spec
    for name, spec in _specs_from_file(cwd / ".clio" / "mcp.json", "workspace", env=env).items():
        merged[name] = spec

    # Reserved names never silently shadow the built-in defaults' identity.
    return {n: replace(s, name=n) for n, s in merged.items()}


def transport_for(spec: MCPServerSpec) -> Any:
    """Turn a spec into the ``server`` argument FastMCP's ``Client`` accepts.

    The single FastMCP-specific glue — generalizes the old clio-kit-only
    ``clio_kit_transport``: a declared stdio command becomes a ``StdioTransport``;
    an http url is passed through (FastMCP ``Client`` accepts an http(s) URL).
    """
    if spec.transport == "stdio":
        from fastmcp.client.transports import StdioTransport  # noqa: PLC0415

        return StdioTransport(
            command=spec.command,
            args=list(spec.args),
            env=dict(spec.env) or None,
        )
    return spec.url
