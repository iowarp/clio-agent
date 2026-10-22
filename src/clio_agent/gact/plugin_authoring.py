"""``make_plugin``: Clio writes a plugin, and the person reviews it in the conversation.

The tool writes the plugin's files (:mod:`clio_agent.plugins.authored`), checks it
as ``clio-agent plugin check`` does, and installs it untrusted. Under bypass it
then trusts it and adds it to this conversation. Otherwise it starts the
``adopt-plugin`` flow, which asks the person in the conversation, with what the
plugin does, its shape and what its code reaches, and does both on Yes.

Only a plugin whose installed files are exactly what Clio wrote takes this path.
Any other plugin, a third party's or one edited since, keeps the trust step in
Settings, in every approval mode.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from clio_agent.gact import context, session_warmup
from clio_agent.gact.agents import prompt_basis
from clio_agent.gact.agents.resolution import (
    SessionAgentUnavailable,
    pin_session_agent,
    plugin_service,
    session_delta,
)
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.plugins import authored
from clio_agent.plugins.check import check
from clio_agent.plugins.resolve import Delta
from clio_agent.plugins.store import StoreError

#: The flow that asks the person to trust and add a plugin Clio wrote.
ADOPT_FLOW = "adopt-plugin"


def adopt(app: Any, name: str, session_id: str, *, reason: str) -> dict[str, Any]:
    """Trust the plugin Clio wrote as ``name`` and add it to ``session_id``'s agent.

    Refused (``AuthoredError``) unless the installed files are exactly the ones
    Clio wrote: checked again here, at the moment of trust.
    """

    store = plugin_service(app).store
    record = authored.authorship(store.root, name)
    if record is None:
        raise authored.AuthoredError(
            "not_authored", f"{name!r} is not a plugin Clio wrote, or its files changed since"
        )
    entry = store.marketplaces().get(name)
    if entry is None or authored.folder_digest(Path(entry["snapshot"])) != record["digest"]:
        raise authored.AuthoredError(
            "changed_since_written", f"the installed {name!r} is not the version Clio wrote"
        )
    store.trust(name, reason=reason)
    refs = sorted(
        f"{row.marketplace}/{row.id}" for row in store.records() if row.marketplace == name
    )
    delta = session_delta(app, session_id)
    added = [ref for ref in refs if ref not in delta.add]
    try:
        pin_session_agent(
            app,
            session_id,
            delta=Delta((*delta.add, *added), tuple(r for r in delta.remove if r not in refs)),
        )
    except SessionAgentUnavailable as exc:
        return {"trusted": True, "added": [], "not_added": exc.reason}
    prompt_basis.settle_basis(app, session_id)
    session_warmup.start_session_warmup(app, session_id, trigger="agent_changed")
    return {"trusted": True, "added": refs}


def request_review(name: str) -> dict[str, Any]:
    """The ``adopt-plugin`` flow's first step: what the person reviews, or why not."""

    app = context.active_app()
    root = plugin_service(app).store.root
    if authored.authorship(root, name) is None:
        return {"authored": False}
    return {"authored": True, **authored.review(authored.authored_root(root) / name)}


def adopt_here(name: str) -> dict[str, Any]:
    """The ``adopt-plugin`` flow's last step: trust ``name`` and add it to the conversation
    that started the flow."""

    app = context.active_app()
    flow_session = app.state.sessions.get(context.active_session_id())
    parent = str(getattr(flow_session, "parent_session_id", "") or "")
    try:
        return adopt(app, name, parent, reason="approved_in_conversation")
    except authored.AuthoredError as exc:
        return {"trusted": False, "reason": exc.reason, "message": str(exc)}


async def make_plugin(name: str, files: dict[str, str]) -> dict[str, Any]:
    from clio_agent.gact.flow_tools import _runtime, start_flow  # noqa: PLC0415

    app, caller = _runtime()
    store = plugin_service(app).store
    session = app.state.sessions.get(caller.session_id)
    try:
        folder = await asyncio.to_thread(authored.write, store.root, name, files, caller.session_id)
    except authored.AuthoredError as exc:
        return {"installed": False, "reason": exc.reason, "error": str(exc)}
    report = await asyncio.to_thread(check, folder, service=plugin_service(app))
    if not report.ok:
        return {"installed": False, "reason": "check_failed", "check": report.to_dict()}
    try:
        await asyncio.to_thread(store.install, str(folder), name=name)
    except StoreError as exc:
        return {"installed": False, "reason": exc.code, "error": str(exc)}
    if getattr(session, "approval_mode", "") == "bypass":
        adopted = await asyncio.to_thread(
            adopt, app, name, caller.session_id, reason="authored_bypass"
        )
        return {"installed": True, "check": "passed", **adopted}
    accepted = await start_flow(ADOPT_FLOW, {"name": name}, None)
    return {
        "installed": True,
        "check": "passed",
        "trusted": False,
        "request": accepted,
        "message": (
            "The person is asked in this conversation whether to use it; the answer "
            "arrives as the request's task result."
        ),
    }


def build_make_plugin_tool() -> Any:
    return native_tool(
        make_plugin,
        name="make_plugin",
        domain="tasks",
        title="Make plugin",
        desc=(
            "Write a plugin from its files (plugin.json and its code, as the author-a-flow "
            "skill describes), check it and install it. Under bypass it is trusted and added "
            "to this conversation at once; otherwise the person is asked here, and the "
            "answer arrives as a task result. Calling it again with changed files makes a "
            "new version, which is asked about again."
        ),
        args={
            "name": {"type": "string", "description": "The plugin's name, as in plugin.json."},
            "files": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Each file's path inside the plugin, mapped to its text.",
            },
        },
        presentation="tasks",
    )
