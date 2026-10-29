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
  though the request's tool list keeps only what the cached prompt had.

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
    """The prompt catches up with the pinned resolution (apply now, or a compaction)."""

    if _metadata(app, session_id).get(PROMPT_BASIS_KEY):
        app.state.sessions.update(session_id, metadata_patch={PROMPT_BASIS_KEY: ""})


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
    if not lines:
        return injections
    section = (
        "New tools you can call now, though they are not in your tool list until the next "
        "compaction:\n" + "\n".join(lines)
    )
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

    if not _metadata(app, session_id).get(PROMPT_BASIS_KEY):
        return ""
    try:
        current = session_resolution(app, session_id)
        basis = basis_resolution(app, session_id, current)
    except SessionAgentUnavailable:
        return ""
    change = pending_change(basis, current)
    if not change["pending"]:
        return ""
    lines = ["The person changed this conversation's plugins."]
    if change["removed"]:
        lines.append(
            f"Removed: {', '.join(change['removed'])}. Do not use their tools or follow "
            "their instructions, even though they still appear above."
        )
    for plugin_id in change["added"]:
        lines.append(f"Added: {plugin_id}.")
        lines += [
            fragment["text"].strip()
            for section in current.prompt.get("sections") or ()
            for fragment in section.get("fragments") or ()
            if fragment.get("plugin") == plugin_id and str(fragment.get("text") or "").strip()
        ]
    skills = [row for row in current.skills if row["id"] in set(change["skills_now"])]
    if skills:
        lines.append("Skills you can load now with load_skill:")
        lines += [f"- {row['id']}: {row.get('description', '')}".rstrip(": ") for row in skills]
    lines.append("Your system prompt will include these changes after the next compaction.")
    return "\n".join(lines)


__all__ = [
    "APPLY_MODES",
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
