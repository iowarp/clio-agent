"""Skills, commands, and tool/agent catalog loading for the GACT server.

Extracted from ``clio_agent.gact.app`` (#714) as a behavior-preserving move.
These helpers discover on-disk skills and command-recipe files and flatten the
experts' curated tool lists into a single GACT catalog. They are pure leaf
helpers: none of them read the app's request-scoped contextvars.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

# SKILL.md discovery/parsing is owned by gact.skills (#917); since #918 skills
# no longer materialize as agents — only the frontmatter parser is shared here.
from clio_agent.gact.skills import _parse_skill_frontmatter
from clio_agent.gact.types import AgentDef, Tool, ToolDomain

logger = logging.getLogger(__name__)


def _builtin_agents() -> list[AgentDef]:
    """Return the code-shipped builtin agent catalog: just the react ``main``.

    This USED to silently load whatever Agent Blueprint snapshot happened to be
    pinned as ``DEFAULT_AGENT_BLUEPRINT_ID`` (an installed marketplace default,
    e.g. ``earthscope-gnss-region``) and re-label its rows "builtin" -- so every
    merge that treated this list as ground truth (``GET /v1/agents``' fallback,
    declared-child resolution for spawn, the session-less prompt-context tree,
    ``/v1/catalog/tools``) silently surfaced an installed-but-never-activated
    marketplace pack as if it were part of the product. That is exactly the
    implicit agent selection the blueprint lane's explicit-activation-only ruling
    forbids (owner, 2026-08-05, commit aa906022) -- it just survived in this
    parallel "builtin" seam instead of ``_runtime_active_agent_blueprint_id``.

    The fallback is deleted here too: the ONLY code-shipped agent is
    :func:`_builtin_main_agent`, and every consumer that merges this list with
    genuinely-installed expert packs (loose/workspace/global, or an EXPLICITLY
    activated blueprint/pack) now sees an honest catalog -- no
    discoverable-but-unactivated registry snapshot leaks in.
    """

    return [_builtin_main_agent()]


#: The builtin main's A2UI catalog declaration (v15 S8): the code-shipped agent
#: declares its catalogs like any shipped agent does -- an explicit list, never
#: an implicit "every builtin". ``a2ui_catalogs.activation`` turns it into the
#: session's declaration source when no Agent Blueprint is active.
BUILTIN_MAIN_A2UI_CATALOGS: tuple[str, ...] = ("clio-workspace",)


def builtin_main_catalog_source() -> Any:
    """The builtin main's A2UI catalog declaration source (a builtin-agent unit)."""

    from clio_agent.gact.a2ui_catalogs.declarations import (  # noqa: PLC0415
        parse_catalog_declarations,
    )

    return parse_catalog_declarations(
        list(BUILTIN_MAIN_A2UI_CATALOGS),
        unit_kind="builtin_agent",
        unit_id="builtin:main",
        root=Path(),
    )


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
        tools=sorted({*TOOL_CATALOG, "view_image", "view_pdf"}),
        skills=["work-with-pdfs"],
        metadata={
            "definition_kind": "builtin_main",
            "a2ui_catalogs": list(BUILTIN_MAIN_A2UI_CATALOGS),
        },
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


def _catalog_stub() -> Any:
    """Return a FRESH catalog-only placeholder callable; never executed.

    A distinct function object per declared tool — never shared — so a native
    tool's per-callable markers (:mod:`tool_instrumentation`'s
    ``DOMAIN_ATTR``/``TITLE_ATTR``/``REPRESENTATION_ATTR``) can never
    cross-contaminate between two catalog-only rows built off the same stub:
    each call to this factory hands back its OWN closure.
    """

    def _stub(*_args: Any, **_kwargs: Any) -> str:
        return ""

    return _stub


def _builtin_tool_declarations() -> list[tuple[str, str, str, Any]]:
    """Declare the code-shipped tool surface as ``(name, title, description, tool_obj)``.

    ``tool_obj`` is the constructed dspy/``ClioNativeTool`` for every declared
    tool EXCEPT the four static gateway (fs/shell) names, where it is ``None``
    — those run through the in-process MCP gateway rather than a directly
    constructed ``dspy.Tool``, so their schema/domain come from
    :mod:`clio_agent.tools.catalog` / :mod:`clio_agent.tools.gateway` instead
    (see :mod:`clio_agent.gact.catalog_tool_schemas`).

    ONE declaration pass, shared by :func:`_builtin_tools` (the plain sync
    Tool-row builder every existing consumer uses) and
    ``catalog_tool_schemas.builtin_tool_rows`` (the schema/domain-bearing
    async builder for ``GET /v1/catalog/tools``) — they can never drift on
    name/title/description.
    """

    from clio_agent.gact.agents.auto_tools import build_auto_react_tools  # noqa: PLC0415
    from clio_agent.gact.agents.declared_native_tools import (  # noqa: PLC0415
        resolve_declared_native_tools,
    )
    from clio_agent.gact.agents.spawn_runtime_declarations import (  # noqa: PLC0415
        assemble_spawn_runtime_tools,
    )

    seen: dict[str, tuple[str, str, str, Any]] = {}
    main = _builtin_main_agent()
    # Declared native tools (e.g. view_image, view_pdf) are constructed
    # in-process, not listed by the gateway; the catalog describes the full
    # declared surface.
    _, native_tools, _ = resolve_declared_native_tools(
        main, {}, supports_vision=True, supports_pdf=True
    )
    for tool in native_tools.values():
        _record_declaration(seen, tool)
    for tool_name in main.tools:
        seen.setdefault(tool_name, (tool_name, tool_name.replace("_", " ").title(), "", None))
    for tool in build_auto_react_tools(main, a2ui_producers=True):
        _record_declaration(seen, tool)
    for tool in assemble_spawn_runtime_tools(
        main,
        spawn_agent_task=_catalog_stub(),
        wait_agent_tasks=_catalog_stub(),
        spawn_agents_parallel=_catalog_stub(),
        run_workflow=_catalog_stub(),
        has_declared_children=False,
        can_commission_blueprints=True,
    ):
        _record_declaration(seen, tool)
    return list(seen.values())


def _record_declaration(seen: dict[str, tuple[str, str, str, Any]], tool: Any) -> None:
    """Add one constructed tool's declaration to ``seen``, first name wins.

    The declared title is read via
    :func:`clio_agent.gact.agents.tool_instrumentation.declared_tool_title` (the curated-title
    registry :func:`~clio_agent.gact.agents.tool_instrumentation.native_tool` populates) —
    NOT ``getattr(tool, "title", "")``, which is always empty on a constructed
    ``ClioNativeTool``/``dspy.Tool`` (neither carries a bare ``.title`` attribute).
    """

    name = str(getattr(tool, "name", "") or "").strip()
    if not name or name in seen:
        return
    from clio_agent.gact.agents.tool_instrumentation import declared_tool_title  # noqa: PLC0415

    seen[name] = (
        name,
        declared_tool_title(name) or "",
        str(getattr(tool, "desc", "") or getattr(tool, "description", "") or ""),
        tool,
    )


def _builtin_tools() -> list[Tool]:
    """Return the code-shipped tool surface for a bare CLIO session.

    This is a product catalog, not a session-effective inventory: it includes
    the workspace gateway, universal react tools, and root coordination tools.
    Blueprint/session additions remain visible through the effective-toolset
    endpoint and are deliberately not guessed here.
    """

    return [
        Tool(
            id=name,
            source="builtin",
            name=name,
            title=title or name.replace("_", " ").title(),
            description=description,
            owner=_tool_owner_for_catalog(name),
            tags=_tool_tags_for_catalog(name),
            visible_to=_tool_visible_to_for_catalog(name),
        )
        for name, title, description, _tool in _builtin_tool_declarations()
    ]


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


def _tool_domain_for_catalog(tool_name: str) -> ToolDomain | None:
    """Return the static gateway domain for a catalog tool row, if declared.

    Only the fs/shell :data:`clio_agent.tools.catalog.TOOL_CATALOG` rows carry
    a domain here — every other builtin tool's domain comes from its own
    constructed callable (:func:`clio_agent.gact.agents.tool_instrumentation.tool_domain`),
    not this static lookup. :class:`~clio_agent.tools.catalog.ToolCatalogEntry` keeps
    ``domain`` as a plain ``str`` (that module is a leaf that must not import the pydantic wire
    types), so this is where it is re-validated against the closed
    :data:`~clio_agent.gact.agents.tool_instrumentation.TOOL_DOMAINS` vocabulary and cast to the
    typed :data:`~clio_agent.gact.types.ToolDomain`.
    """
    try:
        from clio_agent.tools.catalog import get_tool_entry
    except ImportError as exc:
        logger.info(
            "tool domain lookup skipped reason=tools_catalog_import_failed tool=%s error=%r",
            tool_name,
            exc,
        )
        return None

    entry = get_tool_entry(tool_name)
    if entry is None or not entry.domain:
        return None

    from clio_agent.gact.agents.tool_instrumentation import TOOL_DOMAINS

    if entry.domain not in TOOL_DOMAINS:
        logger.warning(
            "tool domain unrecognized reason=domain_not_in_TOOL_DOMAINS tool=%s domain=%r",
            tool_name,
            entry.domain,
        )
        return None
    return cast("ToolDomain", entry.domain)
