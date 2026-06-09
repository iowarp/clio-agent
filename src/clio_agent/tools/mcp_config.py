"""Standard MCP server declarations — low-friction, human-writable.

clio-agent core does not hardcode domain tool servers. Domain/case tools are
ordinary MCP servers, consumed like any installable MCP. A marketplace pack
declares the servers it needs right in its ``AGENT.md`` frontmatter, as a simple
``name: <command-or-url>`` map — no JSON, no nested objects required:

    mcp_servers:
      files: npx -y @modelcontextprotocol/server-filesystem /data
      weather: uvx weather-mcp serve
      notion: https://mcp.notion.com/mcp

The value is either a **command string** (shlex-split into command + args; stdio)
or an **http(s) URL**. ``${VAR}`` / ``${VAR:-default}`` expansion is supported.
For the rare case that needs env vars or headers, a mapping value is also
accepted (``{command, args, env}`` or ``{url, headers}``), but the string form is
the documented, default surface.

Scopes (highest precedence wins, merged by name): workspace
``<cwd>/.clio/mcp.yaml`` > user ``<config>/clio-agent/mcp.yaml`` > pack
``AGENT.md`` frontmatter ``mcp_servers:`` > built-in defaults (fs/shell/web).

This module is parsing only; ``transport_for`` is the single FastMCP glue that
turns a spec into a transport the existing ``execution.py`` machinery accepts.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

__all__ = [
    "MCPServerSpec",
    "MCPConfigError",
    "BUILTIN_SERVER_NAMES",
    "expand_env",
    "spec_from_declaration",
    "specs_from_mapping",
    "resolve_expert_servers",
    "load_mcp_servers",
    "transport_for",
]

# clio-agent's built-in universal defaults (always present, not declarations).
BUILTIN_SERVER_NAMES = frozenset({"fs", "shell", "web"})

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")
_URL_PREFIXES = ("http://", "https://")


class MCPConfigError(ValueError):
    """A declaration could not be parsed (recorded on the spec, not raised)."""


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
    """Expand ``${VAR}`` and ``${VAR:-default}``; raise if a required var is unset."""
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


def _spec_from_string(
    name: str, value: str, *, source: str, env: Mapping[str, str] | None
) -> MCPServerSpec:
    """The default form: a command string (stdio) or an http(s) URL."""
    errors: list[str] = []
    try:
        text = expand_env(value, env=env).strip()
    except MCPConfigError as exc:
        return MCPServerSpec(
            name=name, transport="stdio", source=source, validation_errors=(str(exc),)
        )

    if text.startswith(_URL_PREFIXES):
        return MCPServerSpec(name=name, transport="http", url=text, source=source)

    parts = shlex.split(text)
    if not parts:
        errors.append("empty MCP command/url declaration")
        return MCPServerSpec(
            name=name, transport="stdio", source=source, validation_errors=tuple(errors)
        )
    return MCPServerSpec(
        name=name,
        transport="stdio",
        command=parts[0],
        args=tuple(parts[1:]),
        source=source,
    )


def _spec_from_mapping(
    name: str, entry: Mapping[str, Any], *, source: str, env: Mapping[str, str] | None
) -> MCPServerSpec:
    """Advanced form (only when env/headers/timeout are needed)."""
    errors: list[str] = []
    declared = str(entry.get("type") or "").strip().lower()
    has_command = bool(entry.get("command"))
    has_url = bool(entry.get("url"))
    transport: Literal["stdio", "http"] = (
        "http"
        if declared in {"http", "streamable-http", "sse"}
        or (not declared and has_url and not has_command)
        else "stdio"
    )

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
            if isinstance(raw_args, str):
                raw_args = shlex.split(raw_args)
            elif not isinstance(raw_args, Sequence):
                errors.append("'args' must be a list or string")
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

    timeout_ms: int | None = None
    if entry.get("timeout") is not None:
        try:
            timeout_ms = int(entry["timeout"])
        except (TypeError, ValueError):
            errors.append("'timeout' must be an integer (milliseconds)")

    return MCPServerSpec(
        name=name,
        transport=transport,
        command=command,
        args=args,
        env=env_map,
        url=url,
        headers=headers,
        always_load=bool(entry.get("alwaysLoad") or entry.get("always_load") or False),
        timeout_ms=timeout_ms,
        source=source,
        validation_errors=tuple(errors),
    )


def spec_from_declaration(
    name: str,
    value: Any,
    *,
    source: str = "",
    env: Mapping[str, str] | None = None,
) -> MCPServerSpec:
    """Normalize one ``mcp_servers`` value (string command/url, or mapping)."""
    if isinstance(value, str):
        return _spec_from_string(name, value, source=source, env=env)
    if isinstance(value, Mapping):
        return _spec_from_mapping(name, value, source=source, env=env)
    return MCPServerSpec(
        name=name,
        transport="stdio",
        source=source,
        validation_errors=(f"unsupported mcp_servers value for {name!r}: {type(value).__name__}",),
    )


def specs_from_mapping(
    servers: Mapping[str, Any],
    *,
    source: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, MCPServerSpec]:
    """Build specs from a ``name -> declaration`` mapping (e.g. AGENT.md frontmatter)."""
    return {
        str(name): spec_from_declaration(str(name), value, source=source, env=env)
        for name, value in servers.items()
    }


def resolve_expert_servers(
    global_specs: Mapping[str, MCPServerSpec],
    selection: Any,
    *,
    env: Mapping[str, str] | None = None,
    source: str = "expert",
) -> dict[str, MCPServerSpec]:
    """Resolve the MCP servers a single expert gets in its context.

    Two-level model: ``AGENT.md`` declares the pack-global servers (``global_specs``);
    each expert's frontmatter ``mcp_servers`` then says which it wants locally:

    - a **list of names** -> pick those from ``global_specs`` (an undeclared name
      is recorded as a validation error, not silently dropped),
    - a **mapping** ``name -> command/url`` -> expert-LOCAL servers (added to, or
      overriding by name, the global set for this expert only).

    An expert that declares no ``mcp_servers`` simply gets nothing extra; its
    fine-grained tool visibility still comes from its ``tools:`` list.
    """
    out: dict[str, MCPServerSpec] = {}
    if isinstance(selection, Mapping):
        return specs_from_mapping(selection, source=source, env=env)
    if isinstance(selection, (list, tuple)):
        for raw in selection:
            name = str(raw).strip()
            if not name:
                continue
            if name in global_specs:
                out[name] = global_specs[name]
            else:
                out[name] = MCPServerSpec(
                    name=name,
                    transport="stdio",
                    source=source,
                    validation_errors=(f"expert references undeclared mcp server {name!r}",),
                )
    return out


def _read_mcp_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    servers = data.get("mcp_servers") if isinstance(data, Mapping) else None
    return {str(k): v for k, v in servers.items()} if isinstance(servers, Mapping) else {}


def load_mcp_servers(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    pack_servers: Mapping[str, Mapping[str, Any]] = (),  # type: ignore[assignment]
    env: Mapping[str, str] | None = None,
) -> dict[str, MCPServerSpec]:
    """Discover + merge declared MCP servers across all scopes.

    Precedence (highest wins, merged whole by name): workspace
    ``<cwd>/.clio/mcp.yaml`` > user ``<config>/clio-agent/mcp.yaml`` > pack
    frontmatter (``pack_servers`` = ``{pack_id: {name: declaration}}``). Built-in
    ``fs``/``shell``/``web`` are provided by core, not part of this map.
    """
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    lookup = env if env is not None else os.environ
    merged: dict[str, MCPServerSpec] = {}

    # lowest precedence first
    for pack_id, servers in dict(pack_servers).items():
        if isinstance(servers, Mapping):
            merged.update(specs_from_mapping(servers, source=f"pack:{pack_id}", env=env))
    config_home = Path(lookup.get("XDG_CONFIG_HOME") or (home / ".config"))
    for name, value in _read_mcp_yaml(config_home / "clio-agent" / "mcp.yaml").items():
        merged[name] = spec_from_declaration(name, value, source="user", env=env)
    for name, value in _read_mcp_yaml(cwd / ".clio" / "mcp.yaml").items():
        merged[name] = spec_from_declaration(name, value, source="workspace", env=env)

    return {n: replace(s, name=n) for n, s in merged.items()}


def transport_for(spec: MCPServerSpec, *, cwd: str | None = None) -> Any:
    """Turn a spec into the ``server`` arg FastMCP's ``Client`` accepts.

    For stdio servers, ``cwd`` (when given) is the working directory the
    subprocess is spawned in, so the tool writes into that directory by default.
    For http servers the ``cwd`` is irrelevant and ignored.
    """
    if spec.transport == "stdio":
        from fastmcp.client.transports import StdioTransport  # noqa: PLC0415

        return StdioTransport(
            command=spec.command,
            args=list(spec.args),
            env=dict(spec.env) or None,
            cwd=cwd,
        )
    return spec.url
