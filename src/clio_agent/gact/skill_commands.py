"""Every skill a session's agent has is a ``/<skill>`` command (owner module).

The rows come from the session agent's own :class:`SkillRuntime`, the set its
``load_skill`` tool reads, so plugin skills are included and a skill marked
``user-invocable: false`` is left out. Running ``/<skill> <text>`` loads the skill
through that same ``load_skill`` tool, attributed to the person, and the turn's
model text is the person's text followed by the skill exactly as ``load_skill``
returned it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as gact_context
from clio_agent.gact.agents.resolution import SessionAgentUnavailable, session_agent_def

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Generated catalog skills describe A2UI components for the agent; they are not commands.
_NOT_COMMANDS = frozenset({"catalog"})


def _user_invocable(meta: dict[str, Any]) -> bool:
    value = meta.get("user-invocable", meta.get("user_invocable", True))
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "no", "0", "off"}
    return value is not False


def _runtime(app: "FastAPI", sid: str) -> Any:
    from clio_agent.gact.agents.skill_runtime import skill_runtime_for_agent  # noqa: PLC0415

    agent_def = session_agent_def(app, sid)
    return agent_def, skill_runtime_for_agent(app, agent_def, session_id=sid)


def skill_rows(app: "FastAPI", sid: str) -> list[dict[str, Any]]:
    """One ``skill`` row per skill the session's agent resolves, except ``user-invocable: false``."""

    try:
        _agent_def, runtime = _runtime(app, sid)
    except SessionAgentUnavailable:
        return []
    rows = []
    for skill_id, resolution in sorted(runtime.resolved.items()):
        ref = resolution.skill
        if ref is None or ref.scope in _NOT_COMMANDS or not _user_invocable(ref.meta):
            continue
        plugin = ref.source if ref.scope == "plugin" else ""
        qualifier = plugin or ("user" if ref.scope in {"global", "user"} else "project")
        hint = ref.meta.get("argument-hint") or ref.meta.get("argument_hint") or ""
        rows.append(
            {
                "kind": "skill",
                "id": f"/{skill_id}",
                "name": f"/{skill_id}",
                "qualified_id": f"/{qualifier}:{skill_id}",
                "plugin": plugin,
                "source": f"skill_{ref.scope}",
                "label": "",
                "title": ref.title or skill_id,
                "description": ref.description,
                "argument_hint": str(hint),
                "arguments": [],
                "aliases": [],
                "status": "available",
                "enabled": True,
                "disabled_reason": "",
                "disabled_detail": "",
                "unsupported_fields": [],
                "agent": "",
                "handler": "",
                "path": ref.path,
                "skill_id": skill_id,
                "prompt_template": "",
                "root": "",
            }
        )
    return rows


def load_skill_for_person(app: "FastAPI", sid: str, skill_id: str) -> str:
    """``load_skill(skill_id)`` through the agent's own tool, recorded as the person's load."""

    from clio_agent.gact.agents.skill_runtime import (  # noqa: PLC0415
        LOADED_BY_PERSON,
        build_load_skill_tool,
    )

    agent_def, runtime = _runtime(app, sid)
    tool = build_load_skill_tool(agent_def, runtime)
    tokens = (gact_context.set_app(app), gact_context.set_session_id(sid))
    person = LOADED_BY_PERSON.set(True)
    try:
        return str(tool.func(skill_id=skill_id))
    finally:
        LOADED_BY_PERSON.reset(person)
        for token in reversed(tokens):
            gact_context.reset(token)


__all__ = ["load_skill_for_person", "skill_rows"]
