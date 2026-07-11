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
``AGENT.md`` frontmatter ``mcp_servers:`` > built-in defaults (fs/shell).

This module is parsing only; ``transport_for`` is the single FastMCP glue that
turns a spec into a transport the existing ``execution.py`` machinery accepts.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

__all__ = [
    "MCPServerSpec",
    "MCPConfigError",
    "MCPSpawnError",
    "BUILTIN_SERVER_NAMES",
    "expand_env",
    "spec_from_declaration",
    "specs_from_mapping",
    "resolve_expert_servers",
    "load_mcp_servers",
    "transport_for",
    "transport_from_spec",
    "MCPTransportError",
]

# clio-agent's built-in universal defaults (always present, not declarations).
# These MUST match what ``tools.gateway._mount_builtins`` actually mounts: a name
# listed here but not mounted is silently un-mountable as a user server (the
# gateway skips it as "builtin") — that phantom ``"web"`` bug is why this set is
# kept in lockstep with the mounts, not aspirational.
BUILTIN_SERVER_NAMES = frozenset({"fs", "shell"})

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")
_URL_PREFIXES = ("http://", "https://")

# A server name becomes the tool-namespace prefix, and ``tools.gateway._namespace_of``
# derives that namespace by splitting a namespaced tool name on the FIRST ``_``. A
# name containing ``_`` (or any char outside ``[a-z0-9-]``) therefore yields a wrong
# namespace (``my_server`` -> ``my``) and misclassifies its tools. Validate at
# declaration so the break surfaces as a structured spec error, not a silent
# downstream mis-namespacing.
_VALID_SERVER_NAME = re.compile(r"^[a-z0-9-]+$")


def _validate_server_name(name: str) -> str | None:
    """Return a structured error string if ``name`` is not a legal MCP server name."""
    if not name:
        return "MCP server name must be non-empty"
    if not _VALID_SERVER_NAME.match(name):
        return (
            f"invalid MCP server name {name!r}: names must match [a-z0-9-] with no "
            "'_' (underscore delimits the tool namespace; see "
            "tools.gateway._namespace_of)"
        )
    return None


class MCPConfigError(ValueError):
    """A declaration could not be parsed (recorded on the spec, not raised)."""


class MCPTransportError(ValueError):
    """A raw ``{transport, command, args, url}`` dict spec cannot become a transport.

    Raised by :func:`transport_from_spec` when the stored spec of a REST-installed
    or agent-blueprint MCP server is unusable: a stdio spec with no ``command``, an
    http/streamable-http/sse spec with no ``url``, or an unknown ``transport`` value
    outside the single canonical accepted set ``{stdio, http, streamable-http,
    sse}``. Callers map it to a client-actionable 4xx (e.g. ``mcp_spec_invalid``)
    rather than letting a divergent inline branch 500 or silently drop the server.
    """


class MCPSpawnError(RuntimeError):
    """A stdio MCP server cannot be spawned — raised loudly, never swallowed.

    Raised by :func:`transport_for` at mount time when a precondition for spawning
    the subprocess is already known to be unmet: the launcher command is not on
    PATH, or the working directory does not exist. Surfacing this precise cause is
    the whole point. Otherwise the subprocess dies post-spawn with a bare
    ``No such file or directory (os error 2)``, the proxy connection drops,
    ``list_tools`` returns an empty namespace, and the expert fails three layers
    downstream as an opaque ``_UnsupportedSessionAgent`` / "tools unavailable" — the
    exact undiagnosable chain the gact-tui team hit on a default deployment.
    """


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
        spec = _spec_from_string(name, value, source=source, env=env)
    elif isinstance(value, Mapping):
        spec = _spec_from_mapping(name, value, source=source, env=env)
    else:
        spec = MCPServerSpec(
            name=name,
            transport="stdio",
            source=source,
            validation_errors=(
                f"unsupported mcp_servers value for {name!r}: {type(value).__name__}",
            ),
        )
    name_error = _validate_server_name(name)
    if name_error:
        spec = replace(spec, validation_errors=(name_error, *spec.validation_errors))
    return spec


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
    ``fs``/``shell`` are provided by core, not part of this map.
    """
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    lookup = env if env is not None else os.environ
    merged: dict[str, MCPServerSpec] = {}

    # lowest precedence first
    for pack_id, servers in dict(pack_servers).items():
        if isinstance(servers, Mapping):
            merged.update(specs_from_mapping(servers, source=f"pack:{pack_id}", env=env))
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    user_mcp = paths.user_config_dir_for(home, lookup) / "mcp.yaml"
    for name, value in _read_mcp_yaml(user_mcp).items():
        merged[name] = spec_from_declaration(name, value, source="user", env=env)
    for name, value in _read_mcp_yaml(cwd / ".clio" / "mcp.yaml").items():
        merged[name] = spec_from_declaration(name, value, source="workspace", env=env)

    return {n: replace(s, name=n) for n, s in merged.items()}


def pdeathsig_wrapped_command(command: str, args: Sequence[str]) -> tuple[str, list[str]]:
    """Make an MCP stdio child die with the clio server, even on a hard kill.

    FastMCP / the mcp SDK spawn stdio MCP servers as plain subprocesses with no
    parent-death link, so a SIGKILL / OOM / crash of the clio server orphans them
    to init -- the recurring ``uvx`` MCP-server pile-up. On Linux we prepend
    ``setpriv --pdeathsig SIGKILL --`` so the kernel reaps the child the instant
    its parent (the spawning clio process) dies. This is defense-in-depth on top of
    the process-group group-kill used for *graceful* shutdown; both enforce the same
    invariant -- no orphaned MCP children. A no-op where ``setpriv`` is unavailable
    (non-Linux / minimal images), where the process-group teardown still applies.
    """
    arg_list = list(args)
    if sys.platform != "linux":
        return command, arg_list
    setpriv = shutil.which("setpriv")
    if not setpriv:
        return command, arg_list
    return setpriv, ["--pdeathsig", "SIGKILL", "--", command, *arg_list]


def _mcp_uv_cache_dir() -> Path:
    """Return the dedicated uv cache dir for MCP stdio spawns (under the user cache).

    A clio-owned cache directory that ``uvx``/``uv run`` MCP launchers use instead of
    the developer's ambient uv cache, isolating them from the concurrent-spawn archive
    race and from ``uv cache prune/clean`` deleting ephemeral envs under a running
    server (astral-sh/uv#11694). Resolved through :mod:`clio_agent.paths` so it honours
    the canonical per-user cache location on every OS.
    """
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    return paths.user_cache_dir() / "mcp-uv-cache"


def transport_for(spec: MCPServerSpec, *, cwd: str | None = None) -> Any:
    """Turn a spec into the ``server`` arg FastMCP's ``Client`` accepts.

    For stdio servers, ``cwd`` (when given) is the working directory the
    subprocess is spawned in, so the tool writes into that directory by default.
    For http servers the ``cwd`` is irrelevant and ignored.
    """
    if spec.transport == "stdio":
        from fastmcp.client.transports import StdioTransport  # noqa: PLC0415

        # Fail-loud preflight (iowarp/clio-agent MCP spawn diagnosability): catch the
        # two preconditions that otherwise make the subprocess die post-spawn with a
        # bare ``os error 2`` and surface them precisely instead. A missing ``cwd`` is
        # the prime default-deployment culprit: the workspace dir is passed both as the
        # subprocess working directory (chdir target) and as ``CLIO_KIT_ARTIFACTS``, so
        # if it does not exist the spawn fails with ENOENT.
        if cwd is not None and not os.path.isdir(cwd):
            raise MCPSpawnError(
                f"MCP server {spec.name!r}: working directory {cwd!r} does not exist, so "
                f"the stdio subprocess cannot start (chdir/artifacts-root ENOENT). "
                f"source={spec.source or 'unknown'}"
            )
        resolved = shutil.which(spec.command) if spec.command else None
        if not resolved:
            raise MCPSpawnError(
                f"MCP server {spec.name!r}: launcher command {spec.command!r} not found on "
                f"PATH (cannot spawn the stdio subprocess). Ensure it is installed and on "
                f"PATH for the clio-agent process. source={spec.source or 'unknown'}"
            )

        # Merge ``os.environ`` under the spec vars: we always hand the subprocess an
        # explicit env (so the UV_CACHE_DIR isolation below can apply), and PATH plus
        # the rest of the parent environment must survive. A bare ``dict(spec.env)``
        # would give the child ONLY the spec vars and drop PATH -> anything it execs by
        # name fails with ``os error 2``.
        env: dict[str, str] = {**os.environ, **dict(spec.env)}
        if cwd:
            # Pin clio-kit's artifacts root to the workspace so staged resources
            # and generated artifacts land in the workspace even when the
            # launcher (e.g. ``uv run --directory <pkg>``) changes the process
            # cwd away from it. ``artifacts_root()`` honours ``CLIO_KIT_ARTIFACTS``
            # before falling back to cwd, so this is the destination regardless of
            # what the model passes or omits.
            env["CLIO_KIT_ARTIFACTS"] = cwd
        # Isolate any uvx/``uv run`` MCP launcher onto a dedicated uv cache, unless the
        # DECLARATION explicitly set ``UV_CACHE_DIR`` (declaration wins; the developer's
        # ambient ``UV_CACHE_DIR`` is deliberately overridden — the whole point is
        # isolation from it). Two failures on the shared cache broke live servers:
        # (1) four concurrent cold-cache ``uvx`` spawns race building the same ephemeral
        # env archive, truncating ``pyvenv.cfg`` ("Cannot find home in ...archive-v0/
        # .../pyvenv.cfg") -> the proxy drops on ``list_tools`` and every tool-declaring
        # expert fails to build; (2) a concurrent ``uv cache prune/clean`` deletes
        # ephemeral envs out from under a RUNNING server (astral-sh/uv#11694). A
        # dedicated, clio-owned cache dir keeps MCP spawns off the shared cache both
        # collide on. Created lazily here so a deployment that never spawns stdio MCP
        # servers pays nothing.
        if "UV_CACHE_DIR" not in spec.env:
            uv_cache = _mcp_uv_cache_dir()
            uv_cache.mkdir(parents=True, exist_ok=True)
            env["UV_CACHE_DIR"] = str(uv_cache)
        return StdioTransport(
            command=resolved,
            args=list(spec.args),
            env=env,
            cwd=cwd,
        )
    return spec.url


# The single canonical accepted transport set for raw dict specs. Every
# REST-installed / agent-blueprint MCP server routes its construction through
# :func:`transport_from_spec`, so this set is the one source of truth — no more
# per-site ``{http}`` vs ``{http, streamable-http}`` vs ``{http, streamable-http,
# sse}`` divergence. ``http``/``streamable-http`` share the Streamable-HTTP wire
# protocol (``StreamableHttpTransport``); ``sse`` is a DISTINCT FastMCP wire
# protocol (``SSETransport``) and is constructed separately in
# :func:`transport_from_spec`. All three are accepted here and by the
# agent-blueprint descriptor validator alike.
_STREAMABLE_HTTP_TRANSPORTS = frozenset({"http", "streamable-http"})
_HTTP_TRANSPORTS = _STREAMABLE_HTTP_TRANSPORTS | frozenset({"sse"})
_CANONICAL_TRANSPORTS = frozenset({"stdio"}) | _HTTP_TRANSPORTS


def transport_from_spec(spec: Mapping[str, Any]) -> Any:
    """Turn a raw ``{transport, command, args, url, env}`` dict into a FastMCP transport.

    This is the ONE construction site for the runtime third-party MCP surface —
    the REST-installed servers and agent-blueprint MCP descriptors stored on
    ``app.state.external_mcp_servers[sid]["spec"]``. It complements
    :func:`transport_for` (which takes the typed :class:`MCPServerSpec` and does
    ``which()`` / cwd / ``CLIO_KIT_ARTIFACTS`` pinning) by covering the dict-spec
    path used by the gact routes and agent builders.

    Accepted ``transport`` values are the single canonical set
    ``{stdio, http, streamable-http, sse}``. ``http``/``streamable-http`` connect
    via ``StreamableHttpTransport(url)``; ``sse`` is a DISTINCT FastMCP wire
    protocol and connects via ``SSETransport(url)`` — the same class FastMCP's own
    ``infer_transport`` selects for an ``/sse`` URL, so a real SSE MCP server's
    tools are actually reachable. ``stdio`` spawns a subprocess whose command is
    first wrapped by :func:`pdeathsig_wrapped_command` so a REST-installed stdio
    child dies with the clio server instead of orphaning on a hard kill (Linux
    only; a no-op passthrough on Windows/macOS, exactly as that helper guards).

    Args:
        spec: The stored dict spec. ``transport`` selects the branch; ``command``
            (+ optional ``args``/``env``) drives stdio; ``url`` drives the http
            family.

    Returns:
        A ``fastmcp`` ``ClientTransport`` (``StdioTransport``,
        ``StreamableHttpTransport``, or ``SSETransport``) ready to hand to
        ``fastmcp.Client``.

    Raises:
        MCPTransportError: The spec is unusable — a stdio spec with no
            ``command``, an http-family spec with no ``url``, or a ``transport``
            outside the canonical accepted set.
    """
    from fastmcp.client.transports import (  # noqa: PLC0415
        SSETransport,
        StdioTransport,
        StreamableHttpTransport,
    )

    transport_kind = str(spec.get("transport") or "").strip().lower()
    if transport_kind == "stdio":
        command = str(spec.get("command") or "").strip()
        if not command:
            raise MCPTransportError("stdio MCP transport spec requires a 'command'")
        raw_args = spec.get("args") or []
        raw_env = spec.get("env") or None
        cmd, cmd_args = pdeathsig_wrapped_command(command, list(raw_args))
        return StdioTransport(
            command=cmd,
            args=cmd_args,
            env=dict(raw_env) if raw_env else None,
        )
    if transport_kind in _HTTP_TRANSPORTS:
        url = str(spec.get("url") or "").strip()
        if not url:
            raise MCPTransportError(f"{transport_kind} MCP transport spec requires a 'url'")
        # SSE and Streamable-HTTP are distinct wire protocols in FastMCP; route
        # each to its own transport class (matching ``infer_transport``) so the
        # server's tools are actually reachable.
        if transport_kind == "sse":
            return SSETransport(url=url)
        return StreamableHttpTransport(url=url)
    raise MCPTransportError(
        f"unknown MCP transport {transport_kind!r} "
        f"(expected one of {sorted(_CANONICAL_TRANSPORTS)})"
    )
