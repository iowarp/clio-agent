"""The one CLIO hook system (P2.2).

A declarative, stable-id, tighten-only hook dispatcher over an internal adapter
interface. The subprocess adapter implements the industry exit-0/exit-2 wire; the
four live consumers of the deleted ``clio_agent.runtime.hooks`` registry fire
through the thin module-level ``dispatch_*`` helpers.

See ``docs/design/governance-surfaces-2026-07.md`` (Pillar 2) and
``docs/design/hooks-research-2026-07.md`` (§5.3 wire contract).
"""

from __future__ import annotations

from clio_agent.gact.hooks.adapters import HookAdapter, SubprocessAdapter, default_adapters
from clio_agent.gact.hooks.config import (
    HookConfigError,
    HookEntry,
    HookMatch,
    HookRun,
    discover_hook_entries,
    parse_hook_entries,
)
from clio_agent.gact.hooks.dispatcher import (
    HookDispatcher,
    build_hook_dispatcher,
    dispatch_post_tool,
    dispatch_post_tool_batch,
    dispatch_pre_compact,
    dispatch_pre_tool,
    dispatch_semantic_event,
    dispatch_session_end,
    dispatch_session_start,
    dispatch_stop,
    dispatch_subagent_start,
    dispatch_subagent_stop,
    dispatch_user_prompt_submit,
    get_global_dispatcher,
    install_global_dispatcher,
)
from clio_agent.gact.hooks.events import (
    DENY_CAPABLE_EVENTS,
    KNOWN_EVENTS,
    POST_TOOL_BATCH,
    POST_TOOL_USE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    SEMANTIC_EVENT,
    SESSION_END,
    SESSION_START,
    STOP,
    SUBAGENT_START,
    SUBAGENT_STOP,
    USER_PROMPT_SUBMIT,
    is_deny_capable,
)
from clio_agent.gact.hooks.intercept import (
    fire_post_tool_batch,
    intercept_from_outcome,
    make_post_tool_hook,
    pre_tool_interceptor,
    run_post_tool,
    stash_pre_tool_intercept,
    take_pre_tool_intercept,
)
from clio_agent.gact.hooks.wire import (
    HookDecision,
    HookEnvelope,
    HookInfraError,
    HookOutcome,
    hook_reasons,
    wire_annotations,
)

__all__ = [
    "DENY_CAPABLE_EVENTS",
    "KNOWN_EVENTS",
    "POST_TOOL_BATCH",
    "POST_TOOL_USE",
    "PRE_COMPACT",
    "PRE_TOOL_USE",
    "SEMANTIC_EVENT",
    "SESSION_END",
    "SESSION_START",
    "STOP",
    "SUBAGENT_START",
    "SUBAGENT_STOP",
    "USER_PROMPT_SUBMIT",
    "HookAdapter",
    "HookConfigError",
    "HookDecision",
    "HookDispatcher",
    "HookEntry",
    "HookEnvelope",
    "HookInfraError",
    "HookMatch",
    "HookOutcome",
    "HookRun",
    "SubprocessAdapter",
    "build_hook_dispatcher",
    "default_adapters",
    "discover_hook_entries",
    "dispatch_post_tool",
    "dispatch_post_tool_batch",
    "dispatch_pre_compact",
    "dispatch_pre_tool",
    "dispatch_semantic_event",
    "dispatch_session_end",
    "dispatch_session_start",
    "dispatch_stop",
    "dispatch_subagent_start",
    "dispatch_subagent_stop",
    "dispatch_user_prompt_submit",
    "fire_post_tool_batch",
    "get_global_dispatcher",
    "hook_reasons",
    "install_global_dispatcher",
    "intercept_from_outcome",
    "is_deny_capable",
    "make_post_tool_hook",
    "parse_hook_entries",
    "pre_tool_interceptor",
    "run_post_tool",
    "stash_pre_tool_intercept",
    "take_pre_tool_intercept",
    "wire_annotations",
]
