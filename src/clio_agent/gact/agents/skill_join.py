"""A skill added to the conversation while a turn runs, loadable in that turn (CF-D6).

A turn's ``load_skill`` resolves against the skills its agent had when the turn was
built. A plugin the person adds mid-turn (the plugin or connector offer, the plugin
picker in note mode) pins a new resolution whose skills the plugin-change note names
from the next message on; until then a ``load_skill`` of one of them would answer
"unknown skill" although the conversation has it. :func:`joined_skill` resolves such a
skill from the conversation's pinned resolution, on reality (the skill is in it), and
adds it to the turn's runtime, typed ``skill_joined_turn``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.runtime import trace


def joined_skill(runtime: Any, skill_id: str) -> Any | None:
    """The conversation's own resolution of ``skill_id``, added to ``runtime``; or None."""

    from clio_agent.gact.agents import resolution as _resolution  # noqa: PLC0415
    from clio_agent.gact.skills import SkillCatalog  # noqa: PLC0415

    app, session_id = _ctx.active_app(), _ctx.active_session_id()
    if app is None or not session_id or not skill_id:
        return None
    try:
        pinned = _resolution.session_resolution(app, session_id)
    except (_resolution.SessionAgentUnavailable, RuntimeError):
        return None
    row = next((r for r in pinned.skills if r.get("id") == skill_id), None)
    if row is None:
        return None
    folder = str(Path(row["path"]).parent.parent)
    catalog = SkillCatalog(
        cwd=_resolution._runtime_workspace_catalog_cwd(app, session_id=session_id),
        app=app,
        session_id=session_id,
        plugin_skills=[(str(row["plugin"]), folder)],
    )
    resolved = catalog.resolve(skill_id)
    if resolved.status != "resolved" or resolved.skill is None:
        trace.event(
            "SKILLS",
            "skill_join_unresolved skill=%s plugin=%s status=%s",
            skill_id,
            row.get("plugin"),
            resolved.status,
        )
        return None
    runtime.resolutions[skill_id] = resolved
    trace.event(
        "SKILLS",
        "skill_joined_turn skill=%s plugin=%s session=%s",
        skill_id,
        row.get("plugin"),
        session_id,
    )
    return resolved
