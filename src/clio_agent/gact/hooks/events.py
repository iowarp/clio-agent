"""Hook event names + their capabilities (P2.2).

This slice ships the events needed to port the four live consumers of the deleted
``runtime/hooks.py`` — at least the coverage the old system had. The broader
canonical set (``PostToolUse``/``PostToolBatch``/``SessionStart``/``SessionEnd``/
``SubagentStart``/``SubagentStop``/``PreCompact``) lands in P2.3, and
``BeforeModel`` in P2.4.

Names follow the industry contract where the mapping is honest (hooks-research
§5.2): ``PreToolUse`` and ``UserPromptSubmit`` are the load-bearing deny-capable
events; ``Stop`` is the post-turn observation point; ``SemanticEvent`` is
clio-specific observability (the old ``semantic_event`` hook), deliberately
observation-only.
"""

from __future__ import annotations

#: The tool gate. Deny-capable — the workhorse (allow/deny; ask/modify/synthesize
#: grow in later slices).
PRE_TOOL_USE = "PreToolUse"

#: The turn's user prompt boundary. Deny-capable — validate/reject the prompt.
USER_PROMPT_SUBMIT = "UserPromptSubmit"

#: End-of-turn observation (the old ``post_message``). Deny/self-loop is P2.5, so
#: this slice treats it as observation-only.
STOP = "Stop"

#: Every emitted semantic event (the old ``semantic_event``). Observation-only.
SEMANTIC_EVENT = "SemanticEvent"

#: All events this slice knows about.
KNOWN_EVENTS: frozenset[str] = frozenset(
    {PRE_TOOL_USE, USER_PROMPT_SUBMIT, STOP, SEMANTIC_EVENT}
)

#: The subset whose hooks may block (return a ``deny`` that stops the operation).
#: Every other event is a pure observation: a hook cannot gate it, and a hook
#: infrastructure failure there is swallowed, never fatal to a turn.
DENY_CAPABLE_EVENTS: frozenset[str] = frozenset({PRE_TOOL_USE, USER_PROMPT_SUBMIT})


def is_deny_capable(event: str) -> bool:
    """Return whether a hook on ``event`` may block the operation."""

    return event in DENY_CAPABLE_EVENTS
