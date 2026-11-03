"""The prompt basis: what the model's cached prompt is built from.

A session pins its agent's resolution (``agent_resolution``). The system prompt,
the skills it lists and the tool list are the front of every request, and so of
the provider's prompt cache. They are built from the *prompt basis*: the pinned
resolution itself, unless a plugin change was applied as a note.

A change can reach the model three ways (``PATCH /v1/sessions/{sid}/agent``
``apply``):

* ``now``: the next request carries the new prompt and tools; the cache is rebuilt.
* ``compact``: the session is compacted now, and the change goes in with it.
* ``note``: the basis stays on the resolution the model already has
  (``agent_prompt_basis``) until the next compaction, which rebuilds the cache
  anyway. Until then each turn carries a ``plugin_change`` injection describing
  the change: removed plugins stay in the prompt and the model is asked not to use
  them; an added plugin's instructions travel in the note, its skills load now, and
  its tools are introduced there (name, arguments, description) and run if called,
  though the request's tool list keeps only what the cached prompt had. The note's
  wording is the reminder ``clio.reminder.plugin_change`` and its ``plugin_change_*``
  pieces.

Hooks always follow the pinned resolution. The tools that run, the skills
``load_skill`` accepts and the MCP servers a session mounts are the union of both,
so nothing the model can still see, or was told about, fails to load.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from clio_agent.gact.types import AgentDef
from clio_agent.plugins.resolve import Resolution

PROMPT_BASIS_KEY = "agent_prompt_basis"
APPLY_MODES = ("now", "compact", "note")
#: The injection source of the note that tells the model about a change.
PLUGIN_CHANGE_SOURCE = "plugin_change"
#: Tools a deferred MCP server added mid-session (``tools/list_changed``), named by the
#: plugin-change note until the prompt is rebuilt and its deferred entry names them (TD11).
DEFERRED_ADDED_KEY = "deferred_tools_added"


def _say(piece: str, app: Any = None, session_id: str = "", **variables: Any) -> str | None:
    """One piece of the change note (``None``: the person switched that piece off)."""
    from clio_agent import injections  # noqa: PLC0415

    return injections.render(piece, variables, app=app, session_id=session_id)


def _metadata(app: Any, session_id: str) -> Mapping[str, Any]:
    session = app.state.sessions.get(session_id)
    metadata = getattr(session, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def basis_resolution(app: Any, session_id: str, current: Resolution) -> Resolution:
    """The resolution the session's prompt and tools are built from."""

    from clio_agent.gact.agents.resolution import pinned_resolution  # noqa: PLC0415

    digest = str(_metadata(app, session_id).get(PROMPT_BASIS_KEY) or "")
    if not digest or digest == current.digest:
        return current
    return pinned_resolution(app, digest, str(current.agent.get("ref") or ""))


def hold_basis(app: Any, session_id: str, previous_digest: str) -> None:
    """Keep the model's prompt on ``previous_digest`` (a note-mode change).

    A second note-mode change keeps the first basis: the model's cache still
    holds the prompt from before both.
    """

    if previous_digest and not _metadata(app, session_id).get(PROMPT_BASIS_KEY):
        app.state.sessions.update(session_id, metadata_patch={PROMPT_BASIS_KEY: previous_digest})


def settle_basis(app: Any, session_id: str) -> None:
    """The prompt catches up with the pinned resolution (apply now, or a compaction).

    Tools loaded with ``search_tools`` since the last rebuild join the tool list too.
    """

    from clio_agent.gact.search_tools import promote_loaded_tools  # noqa: PLC0415

    promote_loaded_tools(app, session_id)
    if _metadata(app, session_id).get(PROMPT_BASIS_KEY):
        app.state.sessions.update(session_id, metadata_patch={PROMPT_BASIS_KEY: ""})
    if _metadata(app, session_id).get(DEFERRED_ADDED_KEY):
        app.state.sessions.update(session_id, metadata_patch={DEFERRED_ADDED_KEY: []})


def note_deferred_added_in_turn(session_id: str, names: Iterable[str]) -> None:
    """:func:`note_deferred_tools_added` for the turn's app (a live refresh mid-step)."""
    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    if app is not None and session_id:
        note_deferred_tools_added(app, session_id, names)


def note_deferred_tools_added(app: Any, session_id: str, names: Iterable[str]) -> None:
    """Record tools a deferred server added, for the next plugin-change note."""
    known = [str(n) for n in _metadata(app, session_id).get(DEFERRED_ADDED_KEY) or ()]
    merged = sorted({*known, *names})
    if merged != sorted(known):
        app.state.sessions.update(session_id, metadata_patch={DEFERRED_ADDED_KEY: merged})


def pending_change(basis: Resolution, current: Resolution) -> dict[str, Any]:
    """What the pinned resolution has that the prompt does not, and the reverse."""

    before, after = set(basis.plugin_ids), set(current.plugin_ids)
    basis_tools = set(basis.tool_names)
    basis_skills = {row["id"] for row in basis.skills}
    added = [pid for pid in current.plugin_ids if pid not in before]
    removed = [pid for pid in basis.plugin_ids if pid not in after]
    return {
        "pending": bool(added or removed),
        "added": added,
        "removed": removed,
        "skills_now": [row["id"] for row in current.skills if row["id"] not in basis_skills],
        "tools_unlisted": [row["name"] for row in current.tools if row["name"] not in basis_tools],
        "tools_still_listed": sorted(
            row["name"] for row in basis.tools if row["plugin"] in set(removed)
        ),
    }


def basis_view(app: Any, session_id: str, current: Resolution) -> dict[str, Any]:
    """The prompt basis as the API shows it next to the pinned resolution."""

    basis = basis_resolution(app, session_id, current)
    return {"digest": basis.digest, **pending_change(basis, current)}


def turn_agent_def(app: Any, session_id: str, current: Resolution) -> tuple[AgentDef, Resolution]:
    """The turn's agent and the resolution its module builds tools from.

    Under a held basis the prompt is the basis's; the module builds every tool of
    both resolutions, and ``unlisted_tools`` / ``unlisted_servers`` name the added
    ones the request's tool list leaves out (the note introduces them).
    """

    from clio_agent.gact.agents.resolution import resolution_agent_def  # noqa: PLC0415

    basis = basis_resolution(app, session_id, current)
    if basis.digest == current.digest:
        return resolution_agent_def(current), current
    listed = set(basis.tool_names)
    added_tools = [row for row in current.tools if row["name"] not in listed]
    basis_skills = {row["id"] for row in basis.skills}
    merged = dataclasses.replace(
        current,
        tools=[*basis.tools, *added_tools],
        mcp_servers={**basis.mcp_servers, **current.mcp_servers},
        skills=[*basis.skills, *(r for r in current.skills if r["id"] not in basis_skills)],
    )
    agent_def = resolution_agent_def(basis)
    folders = {(row["plugin"], str(Path(row["path"]).parent.parent)) for row in merged.skills}
    metadata = {
        **agent_def.metadata,
        "mcp_namespaces": sorted(merged.mcp_servers),
        "skill_folders": sorted(folders),
        # The system prompt lists the basis skills only; load_skill takes them all.
        "prompt_skills": list(agent_def.skills),
        "prompt_basis": basis.digest,
        "unlisted_tools": [row["name"] for row in added_tools],
        "unlisted_servers": sorted(set(current.mcp_servers) - set(basis.mcp_servers)),
    }
    update = {
        "tools": [row["name"] for row in merged.tools],
        "skills": [row["id"] for row in merged.skills],
        "metadata": metadata,
    }
    return agent_def.model_copy(update=update), merged


def unlisted_tool_names(metadata: Mapping[str, Any], names: Iterable[str]) -> frozenset[str]:
    """Of ``names``, the tools the request's tool list leaves out (added under a note)."""

    tools = set(metadata.get("unlisted_tools") or ())
    prefixes = tuple(f"{server}_" for server in metadata.get("unlisted_servers") or ())
    return frozenset(n for n in names if n in tools or (prefixes and n.startswith(prefixes)))


def with_tool_introductions(
    injections: list[tuple[str, str]], tools: Iterable[Any]
) -> list[tuple[str, str]]:
    """Add the unlisted tools (name, arguments, description) to the change note."""

    lines = []
    for tool in tools:
        args = ", ".join(
            f"{name}: {spec.get('type', 'any')}" if isinstance(spec, Mapping) else name
            for name, spec in (getattr(tool, "args", None) or {}).items()
        )
        lines.append(f"- {tool.name}({args}): {str(getattr(tool, 'desc', '') or '').strip()}")
    section = _say("plugin_change_tool_introductions", tools="\n".join(lines)) if lines else None
    if not section:
        return injections
    return [
        (source, f"{text}\n{section}" if source == PLUGIN_CHANGE_SOURCE else text)
        for source, text in injections
    ]


def mounted_servers(app: Any, session_id: str, current: Resolution) -> dict[str, Any]:
    """The plugin MCP servers a session mounts: the prompt's and the pinned ones."""

    basis = basis_resolution(app, session_id, current)
    return {**basis.mcp_servers, **current.mcp_servers}


def plugin_change_note(app: Any, session_id: str) -> str:
    """The note that tells the model about plugin changes its prompt lacks ("" if none)."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    added_tools = [str(n) for n in _metadata(app, session_id).get(DEFERRED_ADDED_KEY) or ()]
    where = {"app": app, "session_id": session_id}
    tools_line = (
        _say("plugin_change_deferred_tools", **where, tools=", ".join(added_tools)) or ""
        if added_tools
        else ""
    )
    if not _metadata(app, session_id).get(PROMPT_BASIS_KEY):
        return tools_line
    try:
        current = session_resolution(app, session_id)
        basis = basis_resolution(app, session_id, current)
    except SessionAgentUnavailable:
        return tools_line
    change = pending_change(basis, current)
    if not change["pending"]:
        return tools_line
    lines: list[str | None] = [_say(PLUGIN_CHANGE_SOURCE, **where)]
    if change["removed"]:
        lines.append(_say("plugin_change_removed", **where, plugins=", ".join(change["removed"])))
    for plugin_id in change["added"]:
        lines.append(_say("plugin_change_added", **where, plugin=plugin_id))
        lines += [
            fragment["text"].strip()
            for section in current.prompt.get("sections") or ()
            for fragment in section.get("fragments") or ()
            if fragment.get("plugin") == plugin_id and str(fragment.get("text") or "").strip()
        ]
    skills = [row for row in current.skills if row["id"] in set(change["skills_now"])]
    if skills:
        lines.append(_say("plugin_change_skills", **where))
        lines += [f"- {row['id']}: {row.get('description', '')}".rstrip(": ") for row in skills]
    lines += [tools_line, _say("plugin_change_footer", **where)]
    return "\n".join(line for line in lines if line)


__all__ = [
    "APPLY_MODES",
    "DEFERRED_ADDED_KEY",
    "note_deferred_added_in_turn",
    "note_deferred_tools_added",
    "PLUGIN_CHANGE_SOURCE",
    "PROMPT_BASIS_KEY",
    "basis_resolution",
    "basis_view",
    "hold_basis",
    "mounted_servers",
    "pending_change",
    "plugin_change_note",
    "settle_basis",
    "turn_agent_def",
    "unlisted_tool_names",
    "with_tool_introductions",
]
