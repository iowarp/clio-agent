"""Explicit skill-seeded child-spawn effect owner."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.skills import read_skill_body
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.skills import SkillRef


def _spawned_skill_run_label(ref: SkillRef, assignment: str) -> str:
    """Derive a concise label for a skill-created child conversation."""
    first_line = " ".join(assignment.split()).strip() or str(ref.title or ref.id).strip()
    first_sentence = first_line.split(". ", maxsplit=1)[0].rstrip(".")
    if len(first_sentence) <= 72:
        return first_sentence
    return f"{first_sentence[:69].rstrip()}..."


def apply_spawn_skill_effect(ref: SkillRef, *, agent_id: str, task: str) -> tuple[str, Any, str]:
    """Spawn one skill-seeded child through the explicit spawn tool contract."""
    from clio_agent.gact.agents.skill_effects import (  # noqa: PLC0415
        EFFECT_SPAWN_SUBAGENT,
        SkillEffectError,
        SkillEffectOutcome,
        _emit_skill_effect,
        parse_skill_effect,
    )
    from clio_agent.gact.spawn_context import bind_task_spec_to_parent  # noqa: PLC0415
    from clio_agent.gact.turn_spawn import (  # noqa: PLC0415
        SpawnError,
        TaskSpec,
        spawn_child_turn_threadsafe,
    )

    effect = parse_skill_effect(ref.meta)
    if effect is None or effect.kind != EFFECT_SPAWN_SUBAGENT:
        raise SkillEffectError(
            f"skill {ref.id!r} does not declare spawn_subagent_with_skill",
            reason="skill_not_spawnable",
        )
    app = _ctx.active_app()
    session_id = _ctx.active_session_id()
    if app is None or not session_id:
        raise SkillEffectError(
            f"skill {ref.id!r} cannot spawn without an active session context",
            reason="no_active_session",
        )

    assignment = task.strip()
    if not assignment:
        raise SkillEffectError(
            "spawn_skill_task requires a specific child assignment",
            reason="spawn_task_missing",
        )
    child_expert = effect.agent or agent_id
    body = read_skill_body(ref)
    spec = TaskSpec(
        child_expert_id=child_expert,
        task_text=assignment,
        parent_session_id=session_id,
        requesting_expert_id=agent_id,
        seed_context=f"# Skill: {ref.id}\n\n{body}",
        run_label=_spawned_skill_run_label(ref, assignment),
        skip_declared_check=not effect.agent,
        mode="async",
    )
    try:
        spawned = spawn_child_turn_threadsafe(app, bind_task_spec_to_parent(app, spec))
    except SpawnError as exc:
        raise SkillEffectError(
            f"spawn_subagent_with_skill refused for skill {ref.id!r}: {exc}",
            reason=exc.reason,
        ) from exc

    outcome = SkillEffectOutcome(
        kind=EFFECT_SPAWN_SUBAGENT,
        detail=f"spawned subagent {child_expert!r} seeded with skill {ref.id!r}",
        replaces_body=True,
        task_id=spawned.task_id,
        child_session_id=spawned.child_session_id,
    )
    _emit_skill_effect(app, session_id, ref, outcome, agent_id)
    trace.event(
        "SKILLS", "agent %s skill %s effect %s (%s)", agent_id, ref.id, outcome.kind, outcome.detail
    )
    output = json.dumps(
        {
            "skill_effect": outcome.kind,
            "status": "spawned",
            "task_id": outcome.task_id,
            "child_session_id": outcome.child_session_id,
            "detail": outcome.detail,
        },
        sort_keys=True,
    )
    return output, spawned, child_expert
