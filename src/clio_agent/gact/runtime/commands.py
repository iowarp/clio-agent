"""Slash-command table assembly for the GACT server (#714 decomposition).

This leaf owns the read-only assembly of the backend slash-command catalog: the
built-in command rows, the per-agent command projection, the on-disk command-file
+ skill discovery roots, and the planner-visible / agent-invocable filtering. Two
surfaces consume it, so it lives here as the single source rather than as a
``build_app`` closure:

* :mod:`clio_agent.gact.routes.catalog` -- ``GET /v1/commands`` and the command
  dispatch route enumerate + look up the table.
* :mod:`clio_agent.gact.app` -- ``_prompt_render_context_for_request`` injects the
  agent-invocable command list into the render context for ``POST .../render``.

Every function takes ``app`` explicitly (it reads ``app.state.sessions`` /
``workspaces`` / ``user_agents`` and the runtime blueprint resolvers); the module
imports only leaf packages (catalog, agents.resolution, types, the dependency-free
:mod:`clio_agent.optimizer.stub`, stdlib) and never loads
:mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agents.resolution import (
    _runtime_active_agent_blueprint_id,
    _runtime_active_agent_blueprint_path,
)
from clio_agent.gact.catalog import (
    _load_command_files_from_disk,
    _truthy_command_field,
)
from clio_agent.gact.skills import SkillCatalog, SkillRef
from clio_agent.gact.types import AgentDef
from clio_agent.optimizer.stub import (
    OPTIMIZER_NOT_IMPLEMENTED_MESSAGE,
    OPTIMIZER_NOT_IMPLEMENTED_REASON,
)
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

# The overlay-aware runtime agent resolver (the ``build_app`` closure that layers
# session blueprint-overlay rows before the base resolver). It is passed in rather
# than imported so this leaf uses the exact same resolution the route would, with
# no behaviour drift from the un-overlaid base resolver.
ResolveRuntimeDynamicAgent = Callable[..., "AgentDef | None"]

BACKEND_COMMANDS: list[dict[str, Any]] = [
    {
        "id": "/clear",
        "title": "Clear session messages",
        "description": "Drop the in-memory log for the active session (does NOT touch ARC).",
        "source": "builtin",
        "status": "available",
        "enabled": True,
        "error": "",
    },
    {
        "id": "/cache-stats",
        "title": "ARC cache stats",
        "description": "Append the current ARC cache hit/miss counters as a system message.",
        "source": "builtin",
        "status": "available",
        "enabled": True,
        "error": "",
    },
    {
        "id": "/dump-trace",
        "title": "Dump last reasoning trace",
        "description": "Append the last assistant turn's DSPy reasoning (when available).",
        "source": "builtin",
        "status": "available",
        "enabled": True,
        "error": "",
    },
    {
        # #1081 (P4.3): the /cron user command surfaces the scheduling triad
        # (create/list/delete) that the model reaches as cron_create/cron_list/
        # cron_delete tools + the existing HTTP CRUD (routes/schedules.py). A
        # recurring or one-shot future turn for the active session, evaluated in the
        # session's LOCAL timezone, with anti-runaway clamps enforced server-side.
        "id": "/cron",
        "title": "Schedule a turn (cron / one-shot)",
        "description": (
            "Create, list, or delete scheduled turns for this session — a 5-field cron "
            "(local timezone) or a one-shot run_at/delay. Clamped against runaway."
        ),
        "source": "builtin",
        "status": "available",
        "enabled": True,
        "error": "",
        "aliases": ["/schedule"],
    },
    {
        # #1079 (P4.1): the /loop user command STARTS an autonomous loop — it re-drives
        # this session's turn repeatedly (self-paced via the P4.3 scheduler) toward
        # continued work, under first-class typed bounds (max iters / wall-clock / tokens
        # / no-progress) with a bounded fallback and cancel-both. The model self-paces via
        # the loop_wakeup tool; ending/cancelling the session cancels the pending wakeup.
        "id": "/loop",
        "title": "Start an autonomous loop",
        "description": (
            "Re-drive this session repeatedly toward continued work: /loop [interval] "
            "<prompt>. Self-paced, hard-bounded (iters/wall-clock/tokens/no-progress), "
            "and cancelled when the session ends. Never runs away."
        ),
        "source": "builtin",
        "status": "available",
        "enabled": True,
        "error": "",
    },
    {
        # #801: the optimizer stays as a research surface — this row projects
        # the uniform structured not-implemented stub (reason code + #633
        # pointer) that every optimizer entry point shares.
        "id": "/optimize",
        "title": "Optimize active expert",
        "description": OPTIMIZER_NOT_IMPLEMENTED_MESSAGE,
        "source": "builtin",
        "status": "unavailable",
        "enabled": False,
        "error": OPTIMIZER_NOT_IMPLEMENTED_REASON,
        "disabled_reason": OPTIMIZER_NOT_IMPLEMENTED_MESSAGE,
    },
]


def normalize_command_id(raw: Any) -> str:
    """Coerce any command reference to a leading-slash id (``foo`` -> ``/foo``)."""

    value = str(raw or "").strip()
    if not value:
        return ""
    return value if value.startswith("/") else f"/{value}"


def command_defs_from_agent(agent_def: AgentDef) -> list[dict[str, Any]]:
    """Project an agent definition's declared slash commands into wire rows."""

    metadata = agent_def.metadata if isinstance(agent_def.metadata, Mapping) else {}
    raw_defs: list[Any] = []
    for key in ("commands", "slash_commands", "slash-commands"):
        value = metadata.get(key)
        if isinstance(value, list):
            raw_defs.extend(value)
        elif value:
            raw_defs.append(value)
    for key in ("command", "slash_command", "slash-command"):
        value = metadata.get(key)
        if value:
            raw_defs.append(value)

    rows: list[dict[str, Any]] = []
    for raw in raw_defs:
        if isinstance(raw, str):
            command_id = normalize_command_id(raw)
            row: dict[str, Any] = {}
        elif isinstance(raw, Mapping):
            command_id = normalize_command_id(
                raw.get("id") or raw.get("name") or raw.get("command")
            )
            row = dict(raw)
        else:
            continue
        if not command_id:
            continue
        status = str(row.get("status") or "available")
        enabled = _truthy_command_field(row.get("enabled"), status == "available")
        if status != "available":
            enabled = False
        agent_invocable = _truthy_command_field(
            row.get("agent_invocable", row.get("agent-invocable")),
            False,
        )
        user_invocable = _truthy_command_field(
            row.get("user_invocable", row.get("user-invocable")),
            True,
        )
        rows.append(
            {
                "id": command_id,
                "title": str(row.get("title") or agent_def.title or command_id),
                "description": str(row.get("description") or agent_def.description or ""),
                "source": "user",
                "status": status,
                "enabled": enabled,
                "error": str(row.get("error") or ""),
                "disabled_reason": str(row.get("disabled_reason") or ""),
                "agent_id": agent_def.id,
                "agent_source": agent_def.source,
                "invocation": str(row.get("invocation") or "agent"),
                "user_invocable": user_invocable,
                "agent_invocable": agent_invocable,
                "argument_hint": str(row.get("argument_hint") or row.get("argument-hint") or ""),
                "arguments": row.get("arguments") or [],
                "prompt_template": str(
                    row.get("prompt_template")
                    or row.get("prompt-template")
                    or row.get("prompt")
                    or metadata.get("prompt_template")
                    or metadata.get("prompt-template")
                    or ""
                ),
            }
        )
    return rows


def command_cwd_for_request(
    app: "FastAPI", session_id: str = "", workspace_id: str = ""
) -> Path | None:
    """Resolve the workspace root used to discover on-disk command files."""

    wid = workspace_id
    if session_id:
        sess = app.state.sessions.get(session_id)
        if sess is not None:
            wid = wid or str(getattr(sess, "workspace_id", "") or "")
    if not wid:
        return None
    ws = app.state.workspaces.get(wid)
    if ws is None:
        return None
    root_path = str(getattr(ws, "root_path", "") or "")
    return Path(root_path).expanduser() if root_path else None


def active_blueprint_command_roots(
    app: "FastAPI", session_id: str = ""
) -> list[tuple[Path, str, dict[str, Any]]]:
    """Resolve the active Agent Blueprint's ``commands/`` root for a session."""

    if not session_id:
        return []
    blueprint_id = _runtime_active_agent_blueprint_id(app, session_id)
    blueprint_path = _runtime_active_agent_blueprint_path(app, session_id)
    if blueprint_path is None:
        sess = app.state.sessions.get(session_id)
        metadata = getattr(sess, "metadata", {}) if sess is not None else {}
        if isinstance(metadata, Mapping):
            raw_definition = str(
                metadata.get("active_agent_blueprint_definition_path") or ""
            ).strip()
            if raw_definition:
                blueprint_path = Path(raw_definition).expanduser()
    if not blueprint_id or blueprint_path is None:
        return []
    blueprint_root = blueprint_path.parent if blueprint_path.is_file() else blueprint_path
    return [
        (
            blueprint_root / "commands",
            "agent_blueprint",
            {
                "agent_blueprint_id": blueprint_id,
                "agent_blueprint_root": str(blueprint_root),
                "command_scope": "agent_blueprint",
            },
        )
    ]


def command_context_for_request(
    app: "FastAPI", session_id: str = "", workspace_id: str = ""
) -> tuple[Path | None, list[tuple[Path, str, dict[str, Any]]]]:
    """Return (cwd, extra command roots) for a request's command discovery."""

    return (
        command_cwd_for_request(app, session_id, workspace_id),
        active_blueprint_command_roots(app, session_id),
    )


def user_command_rows(
    app: "FastAPI",
    cwd: Path | None = None,
    extra_roots: list[tuple[Path, str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Discover user command files + skill/agent commands, deduped by id."""

    rows: dict[str, dict[str, Any]] = {}
    for command in _load_command_files_from_disk(cwd=cwd, extra_roots=extra_roots):
        rows.setdefault(command["id"], command)
    agents = [AgentDef(**row.to_wire()) for row in app.state.user_agents.list()]
    for agent_def in agents:
        for command in command_defs_from_agent(agent_def):
            rows.setdefault(command["id"], command)
    catalog = SkillCatalog(cwd=cwd)
    skill_refs: dict[str, Any] = {}
    for ref in catalog.discover():
        skill_refs[ref.id] = ref  # scan order = global first, workspace last: workspace WINS
    for err in catalog.scan_errors:
        # No-silent-fallback: a skill whose commands vanish must say why.
        trace.event("SKILLS", "skill file skipped: %s (%s)", err.get("path"), err.get("error"))
    for ref in skill_refs.values():
        for command in command_defs_from_skill(ref):
            rows.setdefault(command["id"], command)
    return sorted(rows.values(), key=lambda row: row["id"])


_SKILL_COMMAND_KEYS = (
    "command",
    "slash_command",
    "slash-command",
    "commands",
    "slash_commands",
    "slash-commands",
    "prompt_template",
    "prompt-template",
)


def command_defs_from_skill(ref: SkillRef) -> list[dict[str, Any]]:
    """Project a skill's frontmatter command declarations into wire rows (#918).

    Skills no longer materialize as agents, so a skill-declared slash command
    dispatches to ``main`` with the skill body as the prompt template when the
    frontmatter does not provide one — invoking /<command> runs the skill's
    procedure instead of routing to a deleted pseudo-agent.
    """

    stash = {key: ref.meta[key] for key in _SKILL_COMMAND_KEYS if key in ref.meta}
    if not any("command" in key for key in stash):
        return []
    # The skill BODY is the procedure and must always reach the model (the old
    # skill-agent carried it as its system prompt): a declared template is the
    # invocation shape, composed WITH the body — never instead of it.
    declared = str(stash.pop("prompt_template", "") or "").strip()
    declared = declared or str(stash.pop("prompt-template", "") or "").strip()
    stash.pop("prompt-template", None)
    stash["prompt_template"] = (
        f"{declared}\n\n## Skill procedure ({ref.id})\n{ref.body}"
        if declared and ref.body.strip()
        else (declared or ref.body)
    )
    shim = AgentDef(
        id="main",
        source="skill",
        title=ref.title,
        description=ref.description,
        metadata=stash,
    )
    return command_defs_from_agent(shim)


def all_command_rows(
    app: "FastAPI",
    cwd: Path | None = None,
    extra_roots: list[tuple[Path, str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """The full command table: built-ins layered under user/agent commands."""

    rows = {command["id"]: dict(command) for command in BACKEND_COMMANDS}
    for command in user_command_rows(app, cwd=cwd, extra_roots=extra_roots):
        rows.setdefault(command["id"], command)
    return list(rows.values())


def agent_allowed_command_ids(agent_def: AgentDef) -> set[str]:
    """The set of command ids an agent is permitted to invoke."""

    ids = {normalize_command_id(command_id) for command_id in agent_def.commands}
    metadata = agent_def.metadata if isinstance(agent_def.metadata, Mapping) else {}
    for key in ("commands", "allowed_commands", "agent_commands"):
        value = metadata.get(key)
        values = value if isinstance(value, list) else [value] if value else []
        for item in values:
            if isinstance(item, str):
                ids.add(normalize_command_id(item))
            elif isinstance(item, Mapping):
                ids.add(normalize_command_id(item.get("id") or item.get("command")))
    ids.discard("")
    return ids


def planner_command_rows(
    app: "FastAPI",
    resolve_runtime_dynamic_agent: ResolveRuntimeDynamicAgent,
    agent_id: str = "",
    cwd: Path | None = None,
    extra_roots: list[tuple[Path, str, dict[str, Any]]] | None = None,
    session_id: str = "",
) -> list[dict[str, Any]]:
    """The planner-visible, agent-invocable subset of the command table.

    ``resolve_runtime_dynamic_agent`` is the overlay-aware resolver supplied by the
    caller so the allowed-command filter matches the agent the route would resolve.
    """

    allowed: set[str] | None = None
    if agent_id:
        agent_def = resolve_runtime_dynamic_agent(agent_id, session_id=session_id)
        allowed = agent_allowed_command_ids(agent_def) if agent_def is not None else set()
    rows: list[dict[str, Any]] = []
    for command in all_command_rows(app, cwd=cwd, extra_roots=extra_roots):
        command_id = str(command.get("id") or "")
        planner_visible = (
            command.get("enabled") is not False
            and command.get("status") == "available"
            and command.get("agent_invocable") is True
            and command.get("agent_source") != "shell"
            and (allowed is None or command_id in allowed)
        )
        enriched = {**command, "planner_visible": planner_visible}
        if planner_visible:
            rows.append(enriched)
    return rows
