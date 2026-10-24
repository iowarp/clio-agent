"""Import Claude Code plugins into the internal plugin record.

One importer per external format; this is the one for the Claude Code plugin
layout (``.claude-plugin/plugin.json`` with some of ``.mcp.json``, ``skills/``,
``hooks/hooks.json``, ``agents/*.md`` and ``commands/*.md``). It gives no source
special treatment. The mapping:

* A plugin whose only content is ``dependencies`` becomes a pack, so its members
  stay individually removable. ``dependencies`` on a plugin with content of its
  own become hard requirements.
* ``.mcp.json`` entries become the plugin's MCP servers (started lazily).
* ``skills/`` stays portable as is.
* ``hooks/hooks.json`` events map onto CLIO hook events; an event with no CLIO
  equivalent is reported with a typed reason. Other clients' hook files
  (``codex.json``, ``opencode.js``) are ignored.
* ``agents/*.md`` become defined subagents. Claude Code tool names map to CLIO
  tools; an unmappable tool is a typed import error, and a mapping that cannot
  carry the original tool's full behavior is a typed ``tool_mapped_lossy``
  warning. A ``model:`` pin becomes a model preference, not a requirement.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from clio_agent.plugins.command_text import expand_plugin_text
from clio_agent.plugins.frontmatter import FrontmatterError, split_frontmatter
from clio_agent.plugins.loader import _skills, referenced_files, tree_digests
from clio_agent.plugins.manifest import validate_server, within
from clio_agent.plugins.records import (
    CommandRecord,
    Diagnostic,
    HookRecord,
    PluginRecord,
    SubagentRecord,
)
from clio_agent.sdk.versions import PluginRef, VersionRangeError, valid_plugin_id

#: Claude Code tool name -> (CLIO tool names, the plugin that provides them).
TOOL_MAP: dict[str, tuple[tuple[str, ...], str]] = {
    "Read": (("fs_read_file",), "workspace"),
    "Glob": (("fs_list_directory",), "workspace"),
    "Grep": (("fs_search_text",), "workspace"),
    "Edit": (("fs_edit_file",), "workspace"),
    "MultiEdit": (("fs_edit_file",), "workspace"),
    "Write": (("fs_write_file",), "workspace"),
    "Bash": (("shell_bash",), "shell"),
    "WebFetch": (("web_fetch",), "web"),
    "WebSearch": (("web_search",), "web"),
    "Task": (("spawn_agent_task",), "delegation"),
    "Agent": (("spawn_agent_task",), "delegation"),
}

#: Claude Code tools whose CLIO counterpart covers only part of their behavior. Each
#: use is reported as a ``tool_mapped_lossy`` warning naming what the import loses.
LOSSY_TOOL_MAP: dict[str, str] = {
    "Glob": "Glob matches paths recursively by pattern; fs_list_directory lists one "
    "folder's immediate entries (fs_search_text's file_glob narrows files by pattern)",
    "Grep": "Grep matches regular expressions with output modes; fs_search_text "
    "matches literal text and returns bounded line-numbered context",
}

#: Claude Code hook event -> CLIO hook event. Others are reported as unmapped.
HOOK_EVENT_MAP: dict[str, str] = {
    "PreToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse",
    "UserPromptSubmit": "UserPromptSubmit",
    "UserPromptExpansion": "UserPromptExpansion",
    "Stop": "Stop",
    "SubagentStop": "SubagentStop",
    "SessionStart": "SessionStart",
    "SessionEnd": "SessionEnd",
    "PreCompact": "PreCompact",
}

_PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"
_EXECUTABLE_GLOBS = (".mcp.json", "hooks/hooks.json")


def _claude_manifest(root: Path) -> dict[str, Any]:
    path = root / ".claude-plugin" / "plugin.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _servers(root: Path, plugin: str) -> tuple[dict[str, dict[str, Any]], list[Diagnostic]]:
    path = root / ".mcp.json"
    if not path.is_file():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [Diagnostic("error", "mcp_config_invalid", f".mcp.json: {exc}", plugin)]
    raw = data.get("mcpServers", data) if isinstance(data, dict) else {}
    servers: dict[str, dict[str, Any]] = {}
    diagnostics: list[Diagnostic] = []
    for name, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            diagnostics.append(
                Diagnostic(
                    "error", "mcp_server_invalid", f"server {name!r} is not an object", plugin
                )
            )
            continue
        converted = _server(entry)
        error = validate_server(root, str(name), converted)
        if error:
            diagnostics.append(Diagnostic("error", "mcp_server_invalid", error, plugin))
            continue
        servers[str(name)] = converted
    return servers, diagnostics


def _expand(value: str) -> str:
    return value.replace(_PLUGIN_ROOT, "${PLUGIN_ROOT}")


def _server(entry: dict[str, Any]) -> dict[str, Any]:
    kind = str(entry.get("type") or ("stdio" if entry.get("command") else "streamable-http"))
    if kind == "http":
        kind = "streamable-http"
    if kind == "stdio":
        out: dict[str, Any] = {"type": "stdio", "command": str(entry.get("command") or "")}
        if entry.get("args"):
            out["args"] = [_expand(str(arg)) for arg in entry["args"]]
        if entry.get("env"):
            out["env"] = {str(k): _expand(str(v)) for k, v in entry["env"].items()}
        if entry.get("cwd"):
            out["cwd"] = _expand(str(entry["cwd"]))
        return out
    out = {"type": kind, "url": str(entry.get("url") or "")}
    if entry.get("headers"):
        out["headers"] = {str(k): str(v) for k, v in entry["headers"].items()}
    return out


def map_matcher(matcher: str) -> tuple[str, list[str]]:
    """Translate a Claude Code tool matcher into a CLIO tool regex.

    Returns ``(pattern, unmapped)``. A matcher of ``*`` or empty matches every tool.
    """

    text = str(matcher or "").strip()
    if text in ("", "*", ".*"):
        return "", []
    names: list[str] = []
    unmapped: list[str] = []
    for token in (part.strip() for part in text.split("|")):
        if not token:
            continue
        mapped = TOOL_MAP.get(token)
        if mapped is None:
            unmapped.append(token)
            continue
        names.extend(name for name in mapped[0] if name not in names)
    return "|".join(re.escape(name) for name in names), unmapped


def lossy_diagnostic(tokens: list[str], where: str, plugin: str) -> list[Diagnostic]:
    """A ``tool_mapped_lossy`` warning when ``tokens`` name a partially mapped tool."""

    lossy = [token for token in dict.fromkeys(tokens) if token in LOSSY_TOOL_MAP]
    if not lossy:
        return []
    detail = "; ".join(LOSSY_TOOL_MAP[token] for token in lossy)
    message = f"{where} maps {', '.join(lossy)} to CLIO tools that differ: {detail}"
    return [Diagnostic("warning", "tool_mapped_lossy", message, plugin)]


def _hooks(root: Path, plugin: str) -> tuple[list[HookRecord], list[Diagnostic], list[Path]]:
    path = root / "hooks" / "hooks.json"
    if not path.is_file():
        return [], [], []
    diagnostics: list[Diagnostic] = []
    scripts: list[Path] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], [Diagnostic("error", "hooks_invalid", f"hooks/hooks.json: {exc}", plugin)], []
    hooks: list[HookRecord] = []
    events = data.get("hooks", {}) if isinstance(data, dict) else {}
    for event, groups in (events or {}).items():
        clio_event = HOOK_EVENT_MAP.get(event)
        if clio_event is None:
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "hook_event_unmapped",
                    f"hook event {event!r} has no CLIO equivalent and does not run",
                    plugin,
                )
            )
            continue
        for group_index, group in enumerate(groups or []):
            if clio_event == "UserPromptExpansion":
                # Its matcher names commands (``plugin:command``), not tools: kept as written.
                pattern, unmapped = str(group.get("matcher") or "").strip(), list[str]()
            else:
                matcher = str(group.get("matcher") or "")
                pattern, unmapped = map_matcher(matcher)
                tokens = [token.strip() for token in matcher.split("|")]
                diagnostics.extend(lossy_diagnostic(tokens, f"{event} matcher", plugin))
            if unmapped:
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        "hook_matcher_unmapped",
                        f"{event} matcher names tools with no CLIO equivalent: {', '.join(unmapped)}",
                        plugin,
                    )
                )
                if not pattern:
                    continue
            for hook_index, entry in enumerate(group.get("hooks") or []):
                if entry.get("type") != "command":
                    diagnostics.append(
                        Diagnostic(
                            "warning",
                            "hook_type_unsupported",
                            f"{event} hook type {entry.get('type')!r} is not supported",
                            plugin,
                        )
                    )
                    continue
                command = str(entry.get("command") or "").replace(_PLUGIN_ROOT, str(root))
                try:
                    argv = shlex.split(command)
                except ValueError as exc:
                    diagnostics.append(
                        Diagnostic("error", "hooks_invalid", f"{event} command: {exc}", plugin)
                    )
                    continue
                if not argv:
                    continue
                for token in argv:
                    candidate = Path(token)
                    if candidate.is_absolute() and within(root, candidate) and candidate.is_file():
                        scripts.append(candidate)
                hooks.append(
                    HookRecord(
                        id=f"{plugin}.{event.lower()}-{group_index}-{hook_index}",
                        event=clio_event,
                        kind="command",
                        command=argv[0],
                        args=tuple(argv[1:]),
                        tools=pattern,
                        timeout_ms=int(float(entry.get("timeout") or 30) * 1000),
                    )
                )
    return hooks, diagnostics, scripts


def _subagents(root: Path, plugin: str) -> tuple[list[SubagentRecord], list[Diagnostic]]:
    base = root / "agents"
    if not base.is_dir():
        return [], []
    out: list[SubagentRecord] = []
    diagnostics: list[Diagnostic] = []
    for path in sorted(base.glob("*.md")):
        try:
            meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, FrontmatterError) as exc:
            diagnostics.append(
                Diagnostic("error", "subagent_invalid", f"{path.name}: {exc}", plugin)
            )
            continue
        meta = meta or {}
        name = str(meta.get("name") or path.stem)
        raw_tools = meta.get("tools")
        tool_names: list[str] = []
        providers: list[str] = []
        unmapped: list[str] = []
        if raw_tools is not None:
            listed = raw_tools if isinstance(raw_tools, list) else str(raw_tools).split(",")
            for token in (str(t).strip() for t in listed):
                if not token:
                    continue
                mapped = TOOL_MAP.get(token)
                if mapped is None:
                    unmapped.append(token)
                    continue
                tool_names.extend(n for n in mapped[0] if n not in tool_names)
                if mapped[1] not in providers:
                    providers.append(mapped[1])
            tokens = [str(t).strip() for t in listed]
            diagnostics.extend(lossy_diagnostic(tokens, f"subagent {name!r}", plugin))
        if unmapped:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "tool_unmapped",
                    f"subagent {name!r} uses tools with no CLIO equivalent: {', '.join(unmapped)}",
                    plugin,
                )
            )
            continue
        prefers = {"model": str(meta["model"])} if meta.get("model") else {}
        if meta.get("effort"):
            prefers["effort"] = str(meta["effort"])
        out.append(
            SubagentRecord(
                id=name,
                description=str(meta.get("description") or ""),
                prompt=expand_plugin_text(body.strip(), root),
                plugins=(plugin, *providers),
                tools=tuple(tool_names) if raw_tools is not None else None,
                prefers=prefers,
            )
        )
    return out, diagnostics


def _commands(root: Path) -> list[CommandRecord]:
    base = root / "commands"
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.glob("*.md")):
        try:
            meta, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, FrontmatterError):
            meta = None
        out.append(
            CommandRecord(
                id=path.stem, description=str((meta or {}).get("description") or ""), path=str(path)
            )
        )
    return out


def import_claude_plugin(
    root: Path, *, entry: dict[str, Any] | None = None, marketplace: str = ""
) -> PluginRecord:
    """Import the Claude Code plugin at ``root`` (its marketplace ``entry`` supplies defaults)."""

    root = root.resolve()
    entry = dict(entry or {})
    manifest = _claude_manifest(root)
    plugin = str(manifest.get("name") or entry.get("name") or root.name)
    diagnostics: list[Diagnostic] = []
    if not valid_plugin_id(plugin):
        diagnostics.append(
            Diagnostic(
                "error", "plugin_name_invalid", f"plugin name {plugin!r} is not a valid id", plugin
            )
        )
    dependencies: list[str] = []
    for value in manifest.get("dependencies") or entry.get("dependencies") or []:
        name = value.get("name") if isinstance(value, dict) else value
        try:
            dependencies.append(str(PluginRef.parse(str(name))))
        except VersionRangeError as exc:
            diagnostics.append(Diagnostic("error", "dependency_invalid", str(exc), plugin))
    # A marketplace entry (or the plugin's manifest) may name exactly which skill
    # folders this plugin brings; several plugins can then share one source folder.
    listed = entry.get("skills", manifest.get("skills"))
    if isinstance(listed, str):
        listed = [listed]
    only = [str(item) for item in listed] if isinstance(listed, list) else None
    skills, skill_diags = _skills(root, plugin, only)
    servers, server_diags = _servers(root, plugin)
    hooks, hook_diags, scripts = _hooks(root, plugin)
    subagents, subagent_diags = _subagents(root, plugin)
    commands = _commands(root)
    diagnostics += skill_diags + server_diags + hook_diags + subagent_diags
    executable = list(_EXECUTABLE_GLOBS) + [p.relative_to(root).as_posix() for p in scripts]
    referenced = referenced_files(root, servers)
    exec_digest, text_digest = tree_digests(root, executable, referenced=referenced)
    has_content = bool(skills or servers or hooks or subagents or commands)
    is_pack = bool(dependencies) and not has_content
    return PluginRecord(
        id=plugin,
        version=str(manifest.get("version") or entry.get("version") or "0.0.0"),
        description=str(manifest.get("description") or entry.get("description") or ""),
        kind="pack" if is_pack else "plugin",
        format="claude-code",
        root=str(root),
        marketplace=marketplace,
        requires=() if is_pack else tuple(dependencies),
        members=tuple(dependencies) if is_pack else (),
        skills=tuple(skills),
        mcp_servers=servers,
        hooks=tuple(hooks),
        subagents=tuple(subagents),
        commands=tuple(commands),
        diagnostics=tuple(diagnostics),
        executable_digest=exec_digest,
        text_digest=text_digest,
        keywords=tuple(str(k) for k in entry.get("keywords") or manifest.get("keywords") or ()),
    )
