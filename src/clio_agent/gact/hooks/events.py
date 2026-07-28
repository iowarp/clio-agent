"""Hook event names + their capabilities (P2.2 + P2.3).

P2.2 shipped the events needed to port the four live consumers of the deleted
``runtime/hooks.py`` (``PreToolUse``/``UserPromptSubmit``/``Stop``/
``SemanticEvent``). P2.3 adds the rest of the canonical tool + lifecycle set:
``PostToolUse``/``PostToolBatch`` (the tool-result + parallel-batch boundaries)
and the session/subagent/compaction lifecycle points
(``SessionStart``/``SessionEnd``/``SubagentStart``/``SubagentStop``/
``PreCompact``). P2.4 adds the two PER-REQUEST model events fired by the
``dspy.LM`` wrapper (:mod:`clio_agent.lm.hooked_lm`): ``BeforeModel`` (the outgoing
model request, deny/synthesize/route/modify-capable) and ``AfterModel`` (the model
response, observe/rewrite before it enters context). Unlike the turn events these
fire ONCE PER LM CALL — many per turn — which is the whole point of doing them at
the LM boundary rather than a turn-level seam.

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

#: Before ONE model request leaves the ``dspy.LM`` wrapper (P2.4). Fires per LM
#: call (many per turn). Deny/synthesize/route/modify-capable: a ``synthesize``
#: carries a canned ``llm_response`` and the real LM is NEVER called (offline
#: replay + caching); a ``modify`` may carry a ``model_override`` (route this call
#: to a different LM) and/or a ``request_patch`` (rewrite the outgoing
#: messages/params — redact); a ``deny`` blocks the call with a typed reason.
BEFORE_MODEL = "BeforeModel"

#: After ONE model response returns, before it enters context (P2.4). NOT a
#: blocking gate — the call already ran. A hook may OBSERVE or REWRITE the response
#: the model sees (``llm_response`` — changes only what enters context). Fires on a
#: synthesized response too (``synthetic: true``).
AFTER_MODEL = "AfterModel"

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
        BEFORE_MODEL,
        AFTER_MODEL,
    }
)

#: The model events, fired per-request by the ``dspy.LM`` wrapper (P2.4). Kept as a
#: named set so the wrapper's cheap "is any model hook configured?" pre-check (the
#: pass-through fast path) never has to hard-code the two names.
MODEL_EVENTS: frozenset[str] = frozenset({BEFORE_MODEL, AFTER_MODEL})

#: The subset whose hooks may BLOCK an operation (return a ``deny`` that stops it
#: before an effect runs). ``PostToolUse`` is deliberately EXCLUDED: its effect has
#: already run, so its ``deny`` is feedback and an infra failure there can never be
#: fatal — it must not fail-closed a completed call. Every event not listed here is
#: a pure observation: a hook cannot gate it, and a hook infrastructure failure
#: there is swallowed, never fatal to a turn.
#:
#: ``BeforeModel`` is deny-capable: a fail-closed BeforeModel hook that fails on
#: infrastructure blocks the model call (the paused request never leaves) rather
#: than silently letting an un-vetted request through. ``AfterModel`` is
#: deliberately EXCLUDED — its call already ran, so its outcome can only rewrite the
#: observed response, never un-run the request (mirrors ``PostToolUse``).
DENY_CAPABLE_EVENTS: frozenset[str] = frozenset(
    {PRE_TOOL_USE, USER_PROMPT_SUBMIT, BEFORE_MODEL}
)


def is_deny_capable(event: str) -> bool:
    """Return whether a hook on ``event`` may block the operation before its effect."""

    return event in DENY_CAPABLE_EVENTS
