"""Agent Plugins 1.0.0 manifest and MCP configuration validation.

Implements the normative rules of the Agent Plugins Specification v1.0.0
(sections 4, 5, 7.2 and 8) for ``plugin.json`` and ``mcp.json``, plus the
validation CLIO defines for its own ``ai.iowarp.clio`` extension namespace.
Schemas are never fetched while loading a plugin.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from clio_agent.plugins.records import Diagnostic
from clio_agent.sdk.versions import PluginRef, VersionRangeError, valid_plugin_id

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
NAMESPACE = "ai.iowarp.clio"
MANIFEST_FIELDS = (
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
)
_STRING_FIELDS = ("version", "description", "homepage", "repository", "license")
_EXTENSION_FIELDS = (
    "kind",
    "requires",
    "members",
    "model",
    "prefers",
    "workflow_state",
    "catalogs",
)
_STDIO_FIELDS = {"type", "command", "args", "env", "cwd"}
_REMOTE_FIELDS = {"type", "url", "headers"}
_RESERVED_ENV = {"PLUGIN_ROOT", "PLUGIN_DATA"}


class ManifestError(ValueError):
    """``plugin.json`` violates a fatal rule; the plugin must not load."""


def within(root: Path, candidate: Path) -> bool:
    """Whether ``candidate`` resolves inside ``root`` (spec section 4.1)."""

    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def read_manifest(root: Path, plugin: str = "") -> tuple[dict[str, Any], list[Diagnostic]]:
    """Load and validate ``plugin.json``. Raises :class:`ManifestError` when fatal."""

    path = root / "plugin.json"
    if not path.is_file():
        raise ManifestError(f"{root}: missing plugin.json")
    if not within(root, path):
        raise ManifestError(f"{root}: plugin.json resolves outside the plugin root")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: the manifest must be a JSON object")
    return validate_manifest(data, plugin=plugin or root.name)


def validate_manifest(
    data: Mapping[str, Any], *, plugin: str = ""
) -> tuple[dict[str, Any], list[Diagnostic]]:
    """Validate a manifest object, returning the cleaned manifest and warnings."""

    warnings: list[Diagnostic] = []
    cleaned: dict[str, Any] = {}
    for key in data:
        if key not in MANIFEST_FIELDS:
            warnings.append(
                Diagnostic(
                    "warning",
                    "manifest_unknown_field",
                    f"plugin.json field {key!r} is not part of Agent Plugins 1.0.0; ignored",
                    plugin,
                )
            )
    schema = data.get("$schema")
    if schema != PLUGIN_SCHEMA:
        raise ManifestError(
            f"{plugin}: unsupported or missing $schema {schema!r}; expected {PLUGIN_SCHEMA}"
        )
    cleaned["$schema"] = schema
    name = data.get("name")
    if not isinstance(name, str) or not valid_plugin_id(name):
        raise ManifestError(
            f"{plugin}: name {name!r} must be 1-64 characters of a-z, 0-9, '-' or '.', "
            "start and end alphanumeric, with no '--' or '..'"
        )
    cleaned["name"] = name
    for key in _STRING_FIELDS:
        if key in data:
            if not isinstance(data[key], str):
                raise ManifestError(f"{name}: {key} must be a string")
            cleaned[key] = data[key]
    if "author" in data:
        author = data["author"]
        if not isinstance(author, dict) or any(
            k not in ("name", "email", "url") or not isinstance(v, str) for k, v in author.items()
        ):
            raise ManifestError(f"{name}: author may only hold string name, email and url")
        cleaned["author"] = dict(author)
    if "keywords" in data:
        keywords = data["keywords"]
        if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
            raise ManifestError(f"{name}: keywords must be a list of strings")
        cleaned["keywords"] = list(keywords)
    extensions = data.get("extensions")
    if extensions is not None and not isinstance(extensions, dict):
        warnings.append(
            Diagnostic(
                "warning", "manifest_extensions_ignored", "extensions is not an object", name
            )
        )
        extensions = None
    if isinstance(extensions, dict):
        for namespace, value in extensions.items():
            if namespace == NAMESPACE and not isinstance(value, dict):
                raise ManifestError(f"{name}: extensions.{NAMESPACE} must be an object")
        cleaned["extensions"] = dict(extensions)
    return cleaned, warnings


def clio_extension(manifest: Mapping[str, Any], plugin: str) -> dict[str, Any]:
    """Validate and return the ``ai.iowarp.clio`` extension object."""

    ext = dict((manifest.get("extensions") or {}).get(NAMESPACE) or {})
    unknown = sorted(set(ext) - set(_EXTENSION_FIELDS))
    if unknown:
        raise ManifestError(f"{plugin}: unknown {NAMESPACE} field(s): {', '.join(unknown)}")
    kind = ext.get("kind", "plugin")
    if kind not in ("plugin", "pack"):
        raise ManifestError(f"{plugin}: {NAMESPACE}.kind must be 'plugin' or 'pack'")
    for key in ("requires", "members"):
        values = ext.get(key, [])
        if not isinstance(values, list):
            raise ManifestError(f"{plugin}: {NAMESPACE}.{key} must be a list of plugin ids")
        for value in values:
            try:
                PluginRef.parse(str(value))
            except VersionRangeError as exc:
                raise ManifestError(f"{plugin}: {NAMESPACE}.{key}: {exc}") from exc
    catalogs = ext.get("catalogs", [])
    if not isinstance(catalogs, list) or not all(isinstance(c, str) and c for c in catalogs):
        raise ManifestError(f"{plugin}: {NAMESPACE}.catalogs must list built-in A2UI catalog ids")
    for key in ("model", "prefers", "workflow_state"):
        if key in ext and not isinstance(ext[key], dict):
            raise ManifestError(f"{plugin}: {NAMESPACE}.{key} must be an object")
    if kind == "pack" and not ext.get("members"):
        raise ManifestError(f"{plugin}: a pack must list its members")
    return ext


def read_mcp_config(root: Path, plugin: str) -> tuple[dict[str, dict[str, Any]], list[Diagnostic]]:
    """Load ``mcp.json`` (spec section 7.2). Invalid entries are skipped and reported."""

    path = root / "mcp.json"
    if not path.exists():
        return {}, []
    diagnostics: list[Diagnostic] = []
    if not path.is_file() or not within(root, path):
        return {}, [Diagnostic("error", "mcp_config_invalid", "mcp.json is not a file", plugin)]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [Diagnostic("error", "mcp_config_invalid", f"mcp.json: {exc}", plugin)]
    if (
        not isinstance(data, dict)
        or set(data) != {"$schema", "mcpServers"}
        or data.get("$schema") != MCP_SCHEMA
        or not isinstance(data.get("mcpServers"), dict)
    ):
        return {}, [
            Diagnostic(
                "error",
                "mcp_config_invalid",
                f"mcp.json must hold exactly $schema ({MCP_SCHEMA}) and an mcpServers object",
                plugin,
            )
        ]
    servers: dict[str, dict[str, Any]] = {}
    for name, entry in data["mcpServers"].items():
        error = validate_server(root, str(name), entry)
        if error:
            diagnostics.append(Diagnostic("error", "mcp_server_invalid", error, plugin))
            continue
        servers[str(name)] = dict(entry)
    return servers, diagnostics


def validate_server(root: Path, name: str, entry: Any) -> str:
    """Return why one ``mcpServers`` entry is invalid, or an empty string."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        return f"server {name!r}: names use letters, digits, '-' and '_'"
    if not isinstance(entry, dict):
        return f"server {name!r}: must be an object"
    kind = entry.get("type")
    if kind == "stdio":
        extra = set(entry) - _STDIO_FIELDS
        if extra:
            return f"server {name!r}: unknown stdio field(s) {sorted(extra)}"
        command = entry.get("command")
        if not isinstance(command, str) or not command or " " in command.strip():
            return f"server {name!r}: command must be one executable token"
        if "/" in command and not command.startswith("./"):
            return f"server {name!r}: command must be a bare name or a ./ plugin path"
        if command.startswith("./") and not within(root, root / command):
            return f"server {name!r}: command escapes the plugin root"
        args = entry.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            return f"server {name!r}: args must be a list of strings"
        env = entry.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            return f"server {name!r}: env must map names to strings"
        if _RESERVED_ENV & set(env):
            return f"server {name!r}: env may not set PLUGIN_ROOT or PLUGIN_DATA"
        cwd = entry.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                return f"server {name!r}: cwd must be a string"
            if not (
                cwd.startswith("./")
                or cwd in ("${PLUGIN_ROOT}", "${PLUGIN_DATA}")
                or cwd.startswith(("${PLUGIN_ROOT}/", "${PLUGIN_DATA}/"))
            ):
                return f"server {name!r}: cwd must be ./..., ${{PLUGIN_ROOT}} or ${{PLUGIN_DATA}}"
            if cwd.startswith("./") and not within(root, root / cwd):
                return f"server {name!r}: cwd escapes the plugin root"
        return ""
    if kind in ("streamable-http", "sse"):
        extra = set(entry) - _REMOTE_FIELDS
        if extra:
            return f"server {name!r}: unknown {kind} field(s) {sorted(extra)}"
        url = entry.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname:
            return f"server {name!r}: url must be an absolute http(s) URL"
        if parsed.username or parsed.password or parsed.fragment:
            return f"server {name!r}: url may not carry user information or a fragment"
        loopback = parsed.hostname in ("localhost", "127.0.0.1", "::1") or (
            parsed.hostname.startswith("127.")
        )
        if parsed.scheme == "http" and not loopback:
            return f"server {name!r}: non-loopback endpoints must use https"
        headers = entry.get("headers", {})
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            return f"server {name!r}: headers must map names to strings"
        lowered = [key.lower() for key in headers]
        if len(lowered) != len(set(lowered)):
            return f"server {name!r}: a header name repeats with different casing"
        return ""
    return f"server {name!r}: unknown type {kind!r}"
