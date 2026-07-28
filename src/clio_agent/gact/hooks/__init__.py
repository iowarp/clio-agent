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
    dispatch_pre_tool,
    dispatch_semantic_event,
    dispatch_stop,
    dispatch_user_prompt_submit,
    get_global_dispatcher,
    install_global_dispatcher,
)
from clio_agent.gact.hooks.events import (
    DENY_CAPABLE_EVENTS,
    KNOWN_EVENTS,
    PRE_TOOL_USE,
    SEMANTIC_EVENT,
    STOP,
    USER_PROMPT_SUBMIT,
    is_deny_capable,
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
    "PRE_TOOL_USE",
    "SEMANTIC_EVENT",
    "STOP",
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
    "dispatch_pre_tool",
    "dispatch_semantic_event",
    "dispatch_stop",
    "dispatch_user_prompt_submit",
    "get_global_dispatcher",
    "hook_reasons",
    "install_global_dispatcher",
    "is_deny_capable",
    "parse_hook_entries",
    "wire_annotations",
]
