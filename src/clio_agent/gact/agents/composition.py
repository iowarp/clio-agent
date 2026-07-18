"""Prompt composition / dynamic-context rendering for the GACT server (#714).

This module owns the prompt-side helpers carved out of ``clio_agent.gact.app``:
applying the prompt registry to a resolved ``AgentDef`` and rendering the
CLIO-owned dynamic context exposed to prompt templates (the enabled-agent tree,
declared tools/commands, provider summary, the orchestrator-identity briefing for
experts that have children, and the active-workspace grounding block).

These are pure rendering queries over ``app.state`` + the on-disk catalog (no DSPy,
no state mutation). They take the ``FastAPI`` app as an explicit parameter and
import only the shared runtime base + gact leaves -- never ``gact.app`` -- so the
dependency graph stays acyclic.

Composition co-depends with :mod:`~clio_agent.gact.agents.resolution`: resolution
applies the prompt registry while rendering rows, and the orchestrator briefing
here needs resolution's child-row lookup. The cycle is broken by reaching back
into ``resolution`` through the package module namespace at call time (resolution,
in turn, imports the concrete helpers from this module at load).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from clio_agent.gact.catalog import (
    _builtin_agents,
    _builtin_tools,
    _load_command_files_from_disk,
)
from clio_agent.gact.expert_packs import load_expert_packs, validate_expert_hierarchy
from clio_agent.gact.runtime.app_state import per_app_dict
from clio_agent.gact.types import AgentDef
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.prompts import PromptRegistry


def _agent_prompt_request(agent_def: "AgentDef") -> tuple[str, str]:
    """Return prompt id/profile requested by an agent definition."""

    metadata = agent_def.metadata if isinstance(agent_def.metadata, Mapping) else {}
    params = agent_def.parameters if isinstance(agent_def.parameters, Mapping) else {}
    prompt_id = str(
        getattr(agent_def, "prompt_id", "")
        or metadata.get("prompt_id")
        or metadata.get("prompt")
        or params.get("prompt_id")
        or params.get("prompt")
        or ""
    ).strip()
    prompt_profile = str(
        getattr(agent_def, "prompt_profile", "")
        or metadata.get("prompt_profile")
        or metadata.get("profile")
        or params.get("prompt_profile")
        or params.get("profile")
        or ""
    ).strip()
    return prompt_id, prompt_profile


def _prompt_resolution_metadata(resolved: Any, *, requested_profile: str = "") -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "id": getattr(resolved, "id", ""),
            "profile": getattr(resolved, "profile", ""),
            "requested_profile": requested_profile,
            "scope": getattr(resolved, "scope", ""),
            "source_path": getattr(resolved, "source_path", ""),
            "provider": getattr(resolved, "provider", ""),
            "model": getattr(resolved, "model", ""),
            "checksum": getattr(resolved, "checksum", ""),
            "fallback_profile": getattr(resolved, "fallback_profile", ""),
            "validation_errors": list(getattr(resolved, "validation_errors", []) or []),
        }.items()
        if v not in ("", [], None)
    }


def _agent_rows_prompt_render_context(rows: list["AgentDef"]) -> dict[str, str]:
    """Render an agent tree for prompt placeholders without loading more rows."""

    enabled_agents = [agent for agent in rows if getattr(agent, "enabled", True)]
    by_parent: dict[str, list["AgentDef"]] = {}
    for agent in enabled_agents:
        by_parent.setdefault(agent.parent_id or "", []).append(agent)

    def render_tree(parent_id: str = "", depth: int = 0) -> list[str]:
        lines: list[str] = []
        for agent in sorted(by_parent.get(parent_id, []), key=lambda row: (row.tier, row.id)):
            indent = "  " * depth
            detail = f" - {agent.description}" if agent.description else ""
            lines.append(f"{indent}- {agent.id}: {agent.title}{detail}")
            lines.extend(render_tree(agent.id, depth + 1))
        return lines

    return {
        "agents.available_tree": "\n".join(render_tree()) or "(no enabled experts)",
        "agents.available_flat": "\n".join(
            f"- {agent.id}: {agent.title}"
            for agent in sorted(enabled_agents, key=lambda row: row.id)
        )
        or "(no enabled experts)",
    }


def _apply_prompt_registry_to_agent(
    app: "FastAPI",
    agent_def: "AgentDef",
    *,
    prompt_registry: "PromptRegistry | None" = None,
    render_context: dict[str, str] | None = None,
) -> "AgentDef":
    """Resolve an agent's prompt registry reference into runtime prompt text."""

    prompt_id, prompt_profile = _agent_prompt_request(agent_def)
    if not prompt_id:
        return agent_def
    registry = prompt_registry or getattr(app.state, "prompt_registry", None)
    if registry is None:
        return agent_def
    resolved = (
        registry.render(prompt_id, profile=prompt_profile, context=render_context)
        if render_context is not None
        else registry.resolve(prompt_id, profile=prompt_profile)
    )
    metadata = dict(agent_def.metadata)
    if resolved is None:
        metadata["prompt_resolution"] = {
            "id": prompt_id,
            "requested_profile": prompt_profile,
            "status": "missing",
        }
        return agent_def.model_copy(update={"metadata": metadata})
    resolution = _prompt_resolution_metadata(resolved, requested_profile=prompt_profile)
    resolution["status"] = "resolved" if resolved.text.strip() else "invalid"
    metadata["prompt_resolution"] = resolution
    updates: dict[str, Any] = {"metadata": metadata}
    if resolved.text.strip():
        agent_body = agent_def.system_prompt.strip()
        if agent_body:
            resolution["composed_with_agent_body"] = True
            updates["system_prompt"] = "\n\n".join(
                (
                    resolved.text.strip(),
                    "Agent-specific instructions from this definition:",
                    agent_body,
                )
            )
        else:
            updates["system_prompt"] = resolved.text
    if resolved.provider:
        updates["default_provider"] = resolved.provider
    if resolved.model:
        updates["default_model"] = resolved.model
    return agent_def.model_copy(update=updates)


def _prompt_render_context(app: "FastAPI") -> dict[str, str]:
    """Build the CLIO-owned dynamic context exposed to prompt templates."""

    from clio_agent.gact.agents import resolution as _resolution  # noqa: PLC0415

    try:
        agents = validate_expert_hierarchy(
            _resolution._merge_agent_def_rows(
                _builtin_agents()
                + [AgentDef(**row.to_wire()) for row in app.state.user_agents.list()]
                + load_expert_packs()
            )
        )
    except Exception as exc:  # noqa: BLE001 - disk/registry read; fall back to builtins
        trace.event(
            "PROMPT-CTX",
            "agent-tree build failed (%s); falling back to builtin agents",
            exc,
        )
        agents = _builtin_agents()
    enabled_agents = [agent for agent in agents if getattr(agent, "enabled", True)]
    by_parent: dict[str, list["AgentDef"]] = {}
    for agent in enabled_agents:
        by_parent.setdefault(agent.parent_id or "", []).append(agent)

    def render_tree(parent_id: str = "", depth: int = 0) -> list[str]:
        lines: list[str] = []
        for agent in sorted(by_parent.get(parent_id, []), key=lambda row: (row.tier, row.id)):
            indent = "  " * depth
            detail = f" - {agent.description}" if agent.description else ""
            lines.append(f"{indent}- {agent.id}: {agent.title}{detail}")
            lines.extend(render_tree(agent.id, depth + 1))
        return lines

    flat_agents = [
        f"- {agent.id}: {agent.title}" for agent in sorted(enabled_agents, key=lambda row: row.id)
    ]
    tools = [f"- {tool.id}: {tool.description}" for tool in _builtin_tools()]
    commands: list[str] = []
    try:
        for row in _load_command_files_from_disk():
            if row.get("enabled") and row.get("agent_invocable"):
                commands.append(f"- {row.get('id')}: {row.get('description') or row.get('title')}")
    except Exception as exc:  # noqa: BLE001 - disk read; render no commands on failure
        trace.event("PROMPT-CTX", "command-file scan failed (%s); rendering no commands", exc)
        commands = []
    provider = getattr(app.state, "lm_config", None)
    provider_summary = "{}"
    if provider is not None:
        try:
            provider_summary = json.dumps(asdict(provider), sort_keys=True)
        except Exception as exc:  # noqa: BLE001 - non-dataclass provider; stringify
            trace.event("PROMPT-CTX", "provider summary serialize failed (%s); using repr", exc)
            provider_summary = str(provider)
    return {
        "agents.available_tree": "\n".join(render_tree()) or "(no enabled experts)",
        "agents.available_flat": "\n".join(flat_agents) or "(no enabled experts)",
        "tools.available": "\n".join(tools) or "(no declared tools)",
        "commands.agent_invocable": "\n".join(commands) or "(no agent-invocable commands)",
        "memory.policy_summary": "Session-local by default; same-workspace/global reads require explicit policy or user intent.",
        "permissions.policy_summary": "Permission-controlled actions must use CLIO policy gates and visible provenance.",
        "provider.current": provider_summary,
        "session.active_pack": "(no active expert pack)",
    }


def _runtime_dynamic_agent_children_context(
    app: "FastAPI | None",
    agent_def: "AgentDef",
    *,
    session_id: str = "",
) -> str:
    """Render the orchestrator-identity briefing for an expert that has children.

    General across blueprints: any expert with declared children IS, by construction,
    an orchestrator -- it routes work to children (who hold the tools and produce the
    grounded evidence) and assembles their results. This briefing tells the model that
    it is an orchestrator, what each child produces, and how to route work to them by
    CALLING the spawn-runtime tools (``spawn_agent_task`` / ``wait_agent_tasks`` /
    ``spawn_agents_parallel``). This is *grounding* -- telling the model what it is and
    how routing works -- not a behavioral handcuff, and it is what makes a model
    delegate instead of answering (and fabricating) from its own prior knowledge.
    """

    from clio_agent.gact.agents import resolution as _resolution  # noqa: PLC0415

    _aid = getattr(agent_def, "id", "")
    rows: list[AgentDef] = []
    if agent_def.source == "expert_pack" and session_id and app is not None:
        rows = _resolution._runtime_child_agent_rows(app, _aid, session_id=session_id)
    if not rows:
        # Session-less rebuild (the build the model often actually runs): reuse the
        # briefing rendered on a context-bearing build of THIS app so the grounding
        # never drops. Keyed on the passed app's ``app.state`` (per_app_dict) — an
        # app-less rebuild resolves the live turn's app, else a structured empty; it
        # NEVER inherits a sibling app's cached briefing.
        return per_app_dict("orchestrator_briefing", app=app).get(_aid, "")
    lines = [
        "## You are an ORCHESTRATOR — route work to your children; do not do it yourself",
        "",
        "You have child experts who hold the tools and produce the GROUNDED evidence for "
        "this task. You have NO tools of your own and NO grounded knowledge of your own: "
        "any specific fact — a place's coordinates, a station/dataset/resource id, a file "
        "path, a measured value — that you state from prior knowledge instead of from a "
        "child's returned evidence is a FABRICATION and makes the answer invalid. You "
        "literally cannot know these things; only your children's tools can find them. "
        "Your ONLY job is to delegate to the right child, read the typed evidence it "
        "returns, and decide the next step.",
        "",
        "Your child experts (delegate to these — you may route to no one else):",
    ]
    for row in sorted(rows, key=lambda item: (item.tier, item.id)):
        detail = (row.description or row.title or "").strip()
        cap_bits: list[str] = []
        if row.tools:
            cap_bits.append("tools: " + ", ".join(row.tools))
        cap_text = f" [{'; '.join(cap_bits)}]" if cap_bits else ""
        lines.append(f"- `{row.id}`: {detail}{cap_text}")
    lines.append("")
    lines.append(
        "Routing: delegate by CALLING your spawn tools. `spawn_agent_task(agent, task)` "
        "spawns ONE declared child as a real child turn and returns its `task_id`; "
        "`wait_agent_tasks([task_id])` blocks until it finishes and returns its typed "
        "evidence. Spawn one child, wait for its evidence, and let that evidence (in the "
        "returned typed workflow_state) decide the next hop; use `spawn_agents_parallel` "
        "to fan out independent children at once. Read the returned evidence, then write "
        "your final `answer` — every claim in it must be backed by a child's returned "
        "evidence; NEVER answer from your own knowledge. If you have spawned no children "
        "yet, you have no evidence yet — do not answer."
    )
    briefing = "\n".join(lines)
    if _aid:
        per_app_dict("orchestrator_briefing", app=app)[_aid] = briefing
    return briefing


def _runtime_active_workspace_context(app: "FastAPI", *, session_id: str = "") -> str:
    """Surface the active workspace root and write-allowed roots to an expert.

    Data only: it tells the expert where the current session's workspace lives
    and which roots writes are permitted under, so the expert can naturally
    place generated artifacts inside the workspace. It does NOT reroute, rename,
    or force any path — file_policy still validates every write, and out-of-root
    writes still surface as errors / permission prompts.
    """

    if app is None:
        return ""
    from clio_agent.gact.agents import resolution as _resolution  # noqa: PLC0415

    root = _resolution._runtime_workspace_catalog_cwd(app, session_id=session_id)
    if root is None:
        return ""
    try:
        from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415

        allowed_roots = [str(item) for item in FileAccessPolicy.from_env().allowed_roots]
    except Exception as exc:  # noqa: BLE001 - policy env read; degrade to no roots line
        trace.event("WORKSPACE-CTX", "allowed-roots read failed (%s); omitting roots line", exc)
        allowed_roots = []
    lines = [
        f"Active workspace root: {root}",
        (
            "Write generated artifacts (files, charts, exports) inside the "
            "active workspace root using absolute paths so they stay with the "
            "session. Read inputs from wherever the user points you."
        ),
    ]
    if allowed_roots:
        lines.append("Writes are only permitted under: " + ", ".join(allowed_roots) + ".")
    return "\n".join(lines)
