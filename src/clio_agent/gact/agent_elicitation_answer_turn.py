"""The child-turn answer mechanism for agent-driven elicitation (#1309, C1-S7).

Split out of :mod:`clio_agent.gact.agent_elicitation` (the cleanup program's
no-accretion rule, #775: that module is a ratchet-baselined file — new logic
goes in an owner module of its own, not appended past its recorded line
count) as its own small, focused owner for exactly one concern: spawning and
waiting on the bounded, self-directed, TOOL-LESS answer child turn
(``answer_mode="turn"``). See the parent module's docstring for why the
DEFAULT answerer is the inline completion instead (this mechanism deadlocks
while the parent tool call holds the session's own turn slot). Moved
verbatim, no behavior change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from clio_agent.gact.agent_elicitation_context import _bounded_transcript_excerpt
from clio_agent.gact.agent_elicitation_reply import answer_field_text as _answer_field_text


def _spawn_agent_answer_turn(
    app: Any,
    *,
    answer_session_id: str,
    prompt: str,
    depth: int,
) -> Any:
    """Spawn (never waits for) the bounded, self-directed, TOOL-LESS answer child.

    Split out from :func:`_run_agent_answer_turn` so the spawn step -- the
    part F1's authority test drives for real -- is independently callable
    without paying for a full answer-turn LM round trip. Reuses the SAME
    invocation machinery every declared child spawn uses
    (:class:`clio_agent.gact.agents.invoker.InProcessExpertInvoker` over
    :func:`clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe`) -- no new
    turn-state machine. ``skip_declared_check=True`` marks this a
    SELF-directed spawn (the answering expert is the session's own, exactly
    like ``spawn_subagent_with_skill``'s self-directed skill-subagent), never
    a routing decision to a different declared capability. ``tool_allowlist=()``
    forces the child to ZERO tools (see
    :func:`clio_agent.gact.agents.resolution._apply_session_tool_allowlist`) --
    stamped onto the child's OWN metadata at MINT time by ``spawn_child_turn``
    itself, so it is true before the child's first turn build, never patched
    in after the fact.

    Returns:
        The :class:`~clio_agent.gact.agents.invoker.TaskHandle` for the caller
        to ``wait``/``cancel``.

    Raises:
        SpawnError: the bounded spawn was refused (global depth cap, cancelled
            parent, or no live expert bound to the answering session).
    """

    from clio_agent.gact.agents.invoker import InProcessExpertInvoker  # noqa: PLC0415
    from clio_agent.gact.agents.spawn_runtime import _current_session_depth  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import _session_agent_id  # noqa: PLC0415
    from clio_agent.gact.spawn_context import bind_task_spec_to_parent  # noqa: PLC0415
    from clio_agent.gact.turn_spawn import SpawnError, TaskSpec  # noqa: PLC0415

    session = app.state.sessions.get(answer_session_id)
    expert_id = _session_agent_id(session) if session is not None else ""
    if session is None or not expert_id:
        raise SpawnError(
            "no live session/expert bound to answer from",
            reason="agent_elicitation_no_expert",
        )

    seed_context = _bounded_transcript_excerpt(app, answer_session_id)
    spawn_depth = _current_session_depth(app, answer_session_id) + 1
    spec = bind_task_spec_to_parent(
        app,
        TaskSpec(
            child_expert_id=expert_id,
            task_text=prompt,
            parent_session_id=answer_session_id,
            requesting_expert_id="agent_elicitation",
            depth=spawn_depth,
            mode="sync",
            skip_declared_check=True,
            seed_context=seed_context,
            run_label="agent-elicitation answer",
            # This is an implementation turn used to answer the paused tool's
            # question from model context, not delegated user-facing work. The
            # authoritative interaction remains attached to the invocation.
            project_to_parent=False,
            # F1/F3 (owner gate review): both stamped onto the child's OWN
            # metadata at MINT time by spawn_child_turn itself -- true before
            # the child's first turn build, never patched in afterward.
            tool_allowlist=(),
            agent_elicitation_depth=depth,
        ),
    )
    return InProcessExpertInvoker(app).invoke(spec)  # SpawnError propagates, typed


def _run_agent_answer_turn(
    app: Any,
    *,
    answer_session_id: str,
    prompt: str,
    depth: int,
    timeout_s: float,
    on_spawn: Callable[[Any], None] | None = None,
) -> str:
    """Spawn + wait for the bounded answer child turn; return its raw reply
    text (the bounded ``answer_excerpt``).

    Runs entirely on a worker thread (every call here blocks) -- callers
    dispatch it via ``asyncio.to_thread``, exactly like
    ``gact.runtime.ai_review``'s one-shot reviewer runs its own bounded LM
    call off the event loop.

    Raises:
        SpawnError: see :func:`_spawn_agent_answer_turn`.
        TimeoutError: the answer turn did not reach a terminal state in time
            (the in-flight child is cancelled before this raises).
        RuntimeError: the answer turn reached a non-``completed`` terminal
            status (failed/cancelled).
    """

    from clio_agent.gact.agents.invoker import InProcessExpertInvoker  # noqa: PLC0415

    handle = _spawn_agent_answer_turn(
        app, answer_session_id=answer_session_id, prompt=prompt, depth=depth
    )
    if on_spawn is not None:
        on_spawn(handle)
    invoker = InProcessExpertInvoker(app)
    result = invoker.wait(handle, timeout_s=timeout_s)
    if not result.is_terminal:
        invoker.cancel(handle)
        raise TimeoutError(f"agent-elicitation answer turn {handle.task_id} timed out")
    if result.status != "completed":
        raise RuntimeError(
            f"agent-elicitation answer turn {handle.task_id} ended {result.status!r}: "
            f"{result.error_reason or 'no reason recorded'}"
        )
    payload = result.result or {}
    answer_text = _answer_field_text(
        app, result.child_session_id, str(payload.get("message_ref", ""))
    )
    if answer_text:
        return answer_text
    # STRUCTURAL fallback (never a silent regression): no ``answer``-field part
    # was found on the child's final message (an unexpected module shape --
    # e.g. a future answer-turn kind that never streams a text `answer` part
    # at all). Fall back to the generic, bounded excerpt exactly as before
    # this fix, so SOME text still reaches _parse_agent_reply's typed
    # unparseable fallback rather than an empty string masquerading as "the
    # model said nothing".
    return str(payload.get("answer_excerpt", ""))
