"""Hook event names + their capabilities (P2.2 + P2.3).

P2.2 shipped the events needed to port the four live consumers of the deleted
``runtime/hooks.py`` (``PreToolUse``/``UserPromptSubmit``/``Stop``/
``SemanticEvent``). P2.3 adds the rest of the canonical tool + lifecycle set:
``PostToolUse``/``PostToolBatch`` (the tool-result + parallel-batch boundaries)
and the session/subagent/compaction lifecycle points
(``SessionStart``/``SessionEnd``/``SubagentStart``/``SubagentStop``/
``PreCompact``). ``BeforeModel`` remains a P2.4 (dspy.LM wrapper) concern.

Names follow the industry contract where the mapping is honest (hooks-research
§5.2): ``PreToolUse`` and ``UserPromptSubmit`` are the load-bearing deny-capable
events; ``PostToolUse`` observes/rewrites the tool result and its ``deny`` is
FEEDBACK (the effect already ran — it can never un-run it); the lifecycle events
are observation (``SubagentStop`` may validate in a later slice); ``SemanticEvent``
is clio-specific observability.
"""

from __future__ import annotations

#: The tool gate. Deny-capable — the workhorse. A ``modify`` mutates the tool
#: input; a ``synthesize`` skips the real call and fabricates the result (both
#: drive the already-wired ``tool_interceptor`` slot, P2.3).
PRE_TOOL_USE = "PreToolUse"

#: After one tool result. NOT a blocking gate — the effect already ran. A hook may
#: OBSERVE, REWRITE the observation the model sees (``updatedToolOutput`` — changes
#: only what enters context, never the already-run effect), or ``deny`` to feed the
#: reason back to the model as feedback. Fires on a synthesized result too
#: (``synthetic: true``).
POST_TOOL_USE = "PostToolUse"

#: After a full batch of a turn's tool calls resolves, before the next model step.
#: Observation-only (aggregate checks over the round). See the firing-point note in
#: ``turn_finalize`` for the DSPy-loop granularity caveat.
POST_TOOL_BATCH = "PostToolBatch"

#: The turn's user prompt boundary. Deny-capable — validate/reject the prompt.
USER_PROMPT_SUBMIT = "UserPromptSubmit"

#: End-of-turn observation (the old ``post_message``). Observation-only in this
#: slice (bounded self-loop is P2.5).
STOP = "Stop"

#: A session was created. Observation / inject-context.
SESSION_START = "SessionStart"

#: A session was closed (deleted). Observation.
SESSION_END = "SessionEnd"

#: A child (subagent) turn began (``turn_spawn`` queued→running). Observation.
SUBAGENT_START = "SubagentStart"

#: A child (subagent) turn reached a terminal state. Observation (validate in a
#: later slice).
SUBAGENT_STOP = "SubagentStop"

#: Before a session transcript is compacted into memory. Observation /
#: inject-context.
PRE_COMPACT = "PreCompact"

#: Every emitted semantic event (the old ``semantic_event``). Observation-only.
SEMANTIC_EVENT = "SemanticEvent"

#: All events this build knows about.
KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        PRE_TOOL_USE,
        POST_TOOL_USE,
        POST_TOOL_BATCH,
        USER_PROMPT_SUBMIT,
        STOP,
        SESSION_START,
        SESSION_END,
        SUBAGENT_START,
        SUBAGENT_STOP,
        PRE_COMPACT,
        SEMANTIC_EVENT,
    }
)

#: The subset whose hooks may BLOCK an operation (return a ``deny`` that stops it
#: before an effect runs). ``PostToolUse`` is deliberately EXCLUDED: its effect has
#: already run, so its ``deny`` is feedback and an infra failure there can never be
#: fatal — it must not fail-closed a completed call. Every event not listed here is
#: a pure observation: a hook cannot gate it, and a hook infrastructure failure
#: there is swallowed, never fatal to a turn.
DENY_CAPABLE_EVENTS: frozenset[str] = frozenset({PRE_TOOL_USE, USER_PROMPT_SUBMIT})


def is_deny_capable(event: str) -> bool:
    """Return whether a hook on ``event`` may block the operation before its effect."""

    return event in DENY_CAPABLE_EVENTS
