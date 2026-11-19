"""A plugin added during a turn joins the running turn, the way the person chose.

When the person accepts a plugin or connector offer, the agent is in the middle of the
request that needed it. The plugin's tools used to wait for the next message (only its
MCP servers joined), so a one-request conversation never had them: an agent that
enabled the NDP connector worked through the connector's MCP tools without CLIO's own
NDP source tools. Here the running turn takes the plugin at once. The offer card says
how (``apply``, the plugin picker's modes, :mod:`.prompt_basis`):

- ``now``: the prompt basis settles on the new resolution and the turn's tool list and
  system prompt are rebuilt; the provider's cached prefix is written again once.
- ``note``: the basis stays on what the model already has; the plugin's tools join the
  turn unlisted but callable, introduced (name, arguments, description) in the offer's
  result, so the cached prefix is kept.
- ``compact``: at the next step boundary the running scope is compacted, the basis
  settles and the turn is rebuilt as for ``now``; the cache restarts from the summary.

The rebuild builds the agent exactly as the next turn would (``turn_agent_def``, the
session's withheld tools and allowlist, ``build_agent_module``) and the running loop
takes the new program's tools and tool settings (listed, deferred, code-only, the
run_code servers, the live MCP refresh), so a joined tool is instrumented, gated and
disclosed like any other. Each join is traced (:data:`PLUGIN_JOINED_TURN`); one that
cannot run is traced with its typed reason and the offer's result says so.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime import trace

#: How a plugin added during a turn reaches it (the plugin picker's ``apply`` modes).
APPLY_MODES = ("now", "compact", "note")
DEFAULT_APPLY = "note"
#: Traced when an added plugin joined the running turn.
PLUGIN_JOINED_TURN = "plugin_joined_running_turn"
#: Traced (with a reason) when an added plugin could not join the running turn.
PLUGIN_JOIN_FAILED = "plugin_join_running_turn_failed"
_PENDING_COMPACT = "plugin_join_pending_compact"


def apply_mode(metadata: Any) -> str:
    """The card's ``apply`` choice, or the default; an unknown value is refused."""
    value = str((metadata or {}).get("apply") or DEFAULT_APPLY)
    if value not in APPLY_MODES:
        raise ValueError(f"apply must be one of {', '.join(APPLY_MODES)}, not {value!r}")
    return value


def _rebuilt(app: Any, session_id: str) -> Any:
    """The session's agent module, built as the next turn would build it."""
    from clio_agent.gact.agents.builders import build_agent_module  # noqa: PLC0415
    from clio_agent.gact.agents.prompt_basis import turn_agent_def  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        apply_session_tool_allowlist,
        session_resolution,
        withhold_session_tools,
    )

    resolution = session_resolution(app, session_id)
    turn_def, build_resolution = turn_agent_def(app, session_id, resolution)
    turn_def = withhold_session_tools(app, session_id, turn_def)
    dynamic = apply_session_tool_allowlist(app, turn_def, session_id=session_id)
    return build_agent_module(app.state.agent, dynamic, build_resolution), dynamic


def _inner_program(program: Any) -> Any:
    """The ReAct program under a BestOfN / Refine wrapper, if any."""
    return getattr(program, "program", None) if not hasattr(program, "tools") else program


def _adopt(loop: Any, module: Any, *, prompt: bool) -> list[str]:
    """The running loop takes the rebuilt program's tools and tool settings."""
    from clio_agent.gact.agents.clio_react import _system  # noqa: PLC0415
    from clio_agent.gact.compaction import note_system_prompt  # noqa: PLC0415

    new = _inner_program(module.program)
    if new is None:
        raise RuntimeError("the rebuilt agent has no tool program")
    before = set(loop.agent.tools)
    loop.agent.tools.clear()
    loop.agent.tools.update(new.tools)
    for name, value in vars(new).items():
        if name.startswith("_clio_") and name != "_clio_expert_id":
            object.__setattr__(loop.agent, name, value)
    if prompt and not loop.bare:
        loop.inputs["system_prompt"] = module.system_prompt
        loop.system = _system(loop.agent.signature, loop.inputs, loop.agent.tools)
        note_system_prompt(loop.session, loop.scope, loop.system)
    loop.tools = loop._listed_tools()
    return sorted(set(loop.agent.tools) - before)


def _introductions(loop: Any, names: list[str]) -> str:
    """The note-mode introduction of tools that joined unlisted (``""`` if none)."""
    from clio_agent.gact.agents.prompt_basis import (  # noqa: PLC0415
        PLUGIN_CHANGE_SOURCE,
        with_tool_introductions,
    )

    listed = {tool.name for tool in loop.tools}
    tools = [loop.agent.tools[n] for n in names if n in loop.agent.tools and n not in listed]
    if not tools:
        return ""
    return with_tool_introductions([(PLUGIN_CHANGE_SOURCE, "")], tools)[0][1].strip()


def join_running_turn(app: Any, session_id: str, mode: str) -> dict[str, Any]:
    """Bring a plugin the person just added into the running turn, as ``mode`` says.

    Called by the offer tool once its card is answered, inside the turn. Returns what
    the offer's result tells the agent: the tools that joined, whether they are listed,
    the introductions of unlisted ones, or the typed reason the join did not run.
    """
    from clio_agent.gact.agents.clio_react import active_loop  # noqa: PLC0415
    from clio_agent.gact.agents.prompt_basis import settle_basis  # noqa: PLC0415
    from clio_agent.gact.runtime.app_state import per_app_dict  # noqa: PLC0415

    loop = active_loop()
    if loop is None or loop.session != session_id:
        trace.event("TOOLS", "%s reason=no_running_turn", PLUGIN_JOIN_FAILED)
        return {"apply": mode, "joined": [], "join_reason": "no_running_turn"}
    if mode == "compact":
        per_app_dict(_PENDING_COMPACT, app=app)[session_id] = True
        trace.event("TOOLS", "%s mode=compact pending session=%s", PLUGIN_JOINED_TURN, session_id)
        return {"apply": mode, "joined": [], "pending": "compact_before_next_step"}
    try:
        if mode == "now":
            settle_basis(app, session_id)
        module, _dynamic = _rebuilt(app, session_id)
        joined = _adopt(loop, module, prompt=mode == "now")
    except Exception as exc:  # noqa: BLE001 - forwards: typed join_reason in the offer result
        trace.event("TOOLS", "%s mode=%s error=%r", PLUGIN_JOIN_FAILED, mode, exc)
        return {"apply": mode, "joined": [], "join_reason": f"rebuild_failed: {exc}"}
    listed = {tool.name for tool in loop.tools}
    trace.event(
        "TOOLS",
        "%s mode=%s session=%s joined=%s",
        PLUGIN_JOINED_TURN,
        mode,
        session_id,
        ",".join(joined),
    )
    return {
        "apply": mode,
        "joined": joined,
        "listed": [n for n in joined if n in listed],
        "introductions": _introductions(loop, joined) if mode == "note" else "",
    }


def compact_pending_join(loop: Any) -> bool:
    """At a step boundary: run a ``compact`` join the person chose (True when it ran)."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact import working_state  # noqa: PLC0415
    from clio_agent.gact.agents.prompt_basis import settle_basis  # noqa: PLC0415
    from clio_agent.gact.compaction import CompactionError, compact_session_context  # noqa: PLC0415
    from clio_agent.gact.runtime.app_state import per_app_dict  # noqa: PLC0415

    app = _ctx.active_app()
    if app is None or not per_app_dict(_PENDING_COMPACT, app=app).pop(loop.session, False):
        return False
    try:
        result = compact_session_context(
            app,
            loop.session,
            trigger="auto",
            scope=loop.scope,
            summarize=loop.autocompact.summarize,
        )
    except CompactionError as exc:
        trace.event("TOOLS", "%s mode=compact error=%s", PLUGIN_JOIN_FAILED, exc.error)
        result = {"compacted": False}
    if result.get("compacted"):
        loop.autocompact.compaction_id = str(result["compactions"][-1]["compaction_id"])
        loop.autocompact.awaiting_measurement = True
    settle_basis(app, loop.session, promote_loaded=False)
    module, _dynamic = _rebuilt(app, loop.session)
    joined = _adopt(loop, module, prompt=True)
    if result.get("compacted"):
        working_state.reinject_after_compaction(loop.recorder)
    trace.event(
        "TOOLS",
        "%s mode=compact session=%s compacted=%s joined=%s",
        PLUGIN_JOINED_TURN,
        loop.session,
        bool(result.get("compacted")),
        ",".join(joined),
    )
    return True


def refresh_running_tools(loop: Any) -> None:
    """The loop's per-step tool refresh: a pending ``compact`` join, then any MCP server's
    ``tools/list_changed`` (:mod:`clio_agent.gact.agents.live_tool_refresh`)."""
    from clio_agent.gact.agents.deferred_tools import sync_deferred_catalog  # noqa: PLC0415
    from clio_agent.gact.agents.prompt_basis import note_deferred_added_in_turn  # noqa: PLC0415
    from clio_agent.gact.agents.tool_settings import reapply_tool_settings  # noqa: PLC0415

    compact_pending_join(loop)
    refresh = getattr(loop.agent, "_clio_tool_refresh", None)
    if refresh is not None and refresh(loop.agent.tools):
        reapply_tool_settings(loop.agent)
        if added := sync_deferred_catalog(loop.agent):  # named by the next note (TD11)
            note_deferred_added_in_turn(loop.session, added)
        loop.tools = loop._listed_tools()


def joined_message(turn: dict[str, Any]) -> str:
    """The sentence the offer's result adds about the join (``""`` when it did not run)."""
    mode = turn.get("apply")
    if turn.get("pending") == "compact_before_next_step":
        return (
            "The conversation is compacted before your next step; then its tools are in "
            "your tool list."
        )
    if turn.get("join_reason"):
        return f"Its tools could not join this turn ({turn['join_reason']})."
    joined = turn.get("joined") or []
    if not joined:
        return ""
    if mode == "now":
        return f"Its tools are in your tool list from your next step: {', '.join(joined)}."
    text = (
        "Its tools joined this turn without changing your tool list; call them by name "
        f"with these arguments: {', '.join(joined)}."
    )
    intro = turn.get("introductions") or ""
    return f"{text}\n{intro}" if intro else text
