"""Skills, commands, and tool/agent catalog loading for the GACT server.

Extracted from ``clio_agent.gact.app`` (#714) as a behavior-preserving move.
These helpers discover on-disk skills and command-recipe files and flatten the
experts' curated tool lists into a single GACT catalog. They are pure leaf
helpers: none of them read the app's request-scoped contextvars.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.gact.agent_blueprints import (
    DEFAULT_AGENT_BLUEPRINT_ID,
    load_agent_blueprints,
)

# SKILL.md discovery/parsing is owned by gact.skills (#917); since #918 skills
# no longer materialize as agents — only the frontmatter parser is shared here.
from clio_agent.gact.skills import _parse_skill_frontmatter
from clio_agent.gact.types import AgentDef, Tool


def _builtin_agents() -> list[AgentDef]:
    """Return default registry Agent Blueprint rows, if installed.

    The historical in-repo builtin blueprint directory is no longer a runtime
    source. This function name remains for older call sites, but its rows come
    from normal Agent Blueprint discovery and retain registry install metadata.
    """

    builtin_rows = []
    for row in load_agent_blueprints(blueprint_id=DEFAULT_AGENT_BLUEPRINT_ID):
        metadata = {
            **row.metadata,
            "source_blueprint": "default_registry",
            # routes_to is derived from the blueprint's own metadata (children),
            # never a core-hardcoded expert list.
            "routes_to": row.metadata.get("routes_to", []),
        }
        if row.parent_id:
            metadata.setdefault("parent", row.parent_id)
        builtin_rows.append(row.model_copy(update={"metadata": metadata}))
    return builtin_rows


def _builtin_main_agent() -> AgentDef:
    """The in-code react ``main`` a session with NO activated Agent Blueprint runs.

    Owner ruling (2026-08-05): a session never resolves a DISCOVERABLE Agent
    Blueprint it did not activate. A bare session must still work (RULE 2), so
    it executes THIS shipped definition — code, not disk discovery. It runs on
    the same react runtime as blueprint mains (``definition_kind: builtin_main``
    routes it through ``_build_blueprint_dspy_module``), and its tool surface is
    the universal in-process builtins (``clio_agent.tools.catalog.TOOL_CATALOG``,
    the fs/shell tools every host tool fleet mounts). Loose expert-pack experts
    declaring ``parent_id: main`` hang off it in the builtin/expert-pack
    hierarchy, so they remain reachable as declared spawn children.
    """

    from clio_agent.tools.catalog import TOOL_CATALOG  # noqa: PLC0415

    return AgentDef(
        id="main",
        source="builtin",
        title="CLIO Main Agent",
        description=("Built-in react main executed by sessions with no activated Agent Blueprint."),
        tier=1,
        specialization="orchestrator",
        module={"kind": "react"},
        prompt_id="clio.chat",
        tools=sorted(TOOL_CATALOG),
        metadata={"definition_kind": "builtin_main"},
    )


def _load_command_files_from_disk(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    extra_roots: list[tuple[Path, str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Discover CLIO/Claude-compatible Markdown command recipe files."""
    import os

    rows: dict[str, dict[str, Any]] = {}
    roots: list[tuple[Path, str, dict[str, Any]]] = [
        (root, source, {})
        for root, source in _command_search_roots(home or Path.home(), cwd or Path(os.getcwd()))
    ]
    roots.extend(extra_roots or [])
    for root, source, extra_metadata in roots:
        if not root.exists() or not root.is_dir():
            continue
        for md in sorted(root.glob("*.md"), key=lambda path: str(path).lower()):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 - unreadable skill markdown skipped
                continue
            meta, body = _parse_skill_frontmatter(text)
            command_id = _normalize_file_command_id(meta, md)
            if not command_id:
                continue
            description = str(meta.get("description") or "").strip()
            if not description:
                for line in body.splitlines():
                    line = line.strip()
                    if line:
                        description = line[:240]
                        break

            status = str(meta.get("status") or "available").strip() or "available"
            disabled_reason = str(
                meta.get("disabled_reason") or meta.get("disabled-reason") or ""
            ).strip()
            shell_fields = ("shell", "exec", "run", "command_line", "command-line")
            if any(key in meta for key in shell_fields):
                status = "unsupported"
                disabled_reason = disabled_reason or (
                    "direct local shell execution is not supported by CLIO user commands"
                )

            enabled = _truthy_command_field(meta.get("enabled"), status == "available")
            if status != "available":
                enabled = False
            agent_id = str(
                meta.get("agent")
                or meta.get("agent_id")
                or meta.get("target_agent")
                or meta.get("target-agent")
                or "main"
            ).strip()
            command = {
                "id": command_id,
                "title": str(meta.get("title") or command_id).strip(),
                "description": description,
                "source": "user",
                "status": status,
                "enabled": enabled,
                "error": str(
                    meta.get("error") or ("not_supported" if status == "unsupported" else "")
                ),
                "disabled_reason": disabled_reason,
                "agent_id": agent_id,
                "agent_source": "command_file",
                "command_path": md.as_posix(),
                "command_source": source,
                "invocation": (
                    "agent"
                    if _truthy_command_field(
                        meta.get("agent-invocable", meta.get("agent_invocable")),
                        False,
                    )
                    else "user"
                ),
                "user_invocable": _truthy_command_field(
                    meta.get("user-invocable", meta.get("user_invocable")),
                    True,
                ),
                "agent_invocable": _truthy_command_field(
                    meta.get("agent-invocable", meta.get("agent_invocable")),
                    False,
                ),
                "argument_hint": str(meta.get("argument-hint") or meta.get("argument_hint") or ""),
                "arguments": meta.get("arguments") or [],
                "prompt_template": body,
                "prompt_profile": str(
                    meta.get("prompt-profile") or meta.get("prompt_profile") or ""
                ),
                **extra_metadata,
            }
            rows.setdefault(command_id, command)
    return list(rows.values())


def _command_search_roots(home: Path, cwd: Path) -> list[tuple[Path, str]]:
    """Return command roots in precedence order; first matching id wins."""
    import os  # noqa: PLC0415

    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    return [
        (cwd / ".clio" / "commands", "clio_workspace"),
        (cwd / ".claude" / "commands", "claude_workspace"),
        (paths.user_config_dir_for(home, os.environ) / "commands", "clio_user"),
        (home / ".claude" / "commands", "claude_user"),
    ]


def _normalize_file_command_id(meta: Mapping[str, Any], path: Path) -> str:
    raw = meta.get("slash_id") or meta.get("slash-id") or meta.get("name") or path.stem
    value = str(raw or "").strip()
    if not value:
        return ""
    return value if value.startswith("/") else f"/{value}"


def _truthy_command_field(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


def _builtin_tools() -> list[Tool]:
    """Flatten the experts' curated tool lists into a single GACT
    Tool catalog. Stable ids (same strings the experts reference),
    backend flag `builtin`. The names MAY duplicate across experts
    (e.g. read_file) — we dedupe by id so GET /v1/catalog/tools has
    one row per distinct tool."""

    seen: dict[str, Tool] = {}
    for agent in _builtin_agents():
        if agent.tier not in {2, 3}:
            continue
        for tool_name in agent.tools:
            if tool_name in seen:
                continue
            seen[tool_name] = Tool(
                id=tool_name,
                source="builtin",
                name=tool_name,
                title=tool_name.replace("_", " ").title(),
                owner=_tool_owner_for_catalog(tool_name),
                tags=_tool_tags_for_catalog(tool_name),
                visible_to=_tool_visible_to_for_catalog(tool_name),
            )
    return list(seen.values())


def _tool_owner_for_catalog(tool_name: str) -> str:
    """Return static owner metadata for a catalog tool row."""
    try:
        from clio_agent.tools.catalog import tool_owner

        return tool_owner(tool_name)
    except Exception:  # noqa: BLE001 - tool metadata lookup optional; empty on any failure
        return ""


def _tool_tags_for_catalog(tool_name: str) -> list[str]:
    """Return static tag metadata for a catalog tool row."""
    try:
        from clio_agent.tools.catalog import tool_tags

        return sorted(tool_tags(tool_name))
    except Exception:  # noqa: BLE001 - tool metadata lookup optional; empty on any failure
        return []


def _tool_visible_to_for_catalog(tool_name: str) -> list[str]:
    """Return static visibility metadata for a catalog tool row."""
    try:
        from clio_agent.tools.catalog import tool_visible_scopes

        return tool_visible_scopes(tool_name)
    except Exception:  # noqa: BLE001 - tool metadata lookup optional; empty on any failure
        return []
