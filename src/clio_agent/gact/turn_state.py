"""Turn-scoped mutable state for the GACT turn engine (#767 Phase B).

``_run_turn_in_background`` in :mod:`clio_agent.gact.turn` used to carry its whole
working set as ~40 function-scope locals threaded through a stack of nested
closures. Phase B decomposes that god-function into free-function seam modules
(``turn_stream``/``turn_forward``/``turn_finalize``/``turn_spawn``/...); the
:class:`TurnState` dataclass is the shared carrier those seams read and mutate.

The refactor is behavior-preserving because ``turn.py`` has *zero* ``nonlocal``:
every closure only READ function-scope scalars (late-bound) and mutated captured
mutable OBJECTS in place, while every scalar REASSIGNMENT lived in the linear
body. Threading a single mutable ``TurnState`` reproduces that exactly — a closure
reading ``x`` becomes ``state.x``; a body reassignment ``x = …`` becomes
``state.x = …`` — with no aliasing hazard, since nothing a closure touches is
written back by that closure.

Slice 0 (this file's introduction) only stands the dataclass up and threads
``state`` through the existing closures/body; no code leaves ``turn.py`` yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.runtime.globals import _semantic_trace_id

if TYPE_CHECKING:
    import threading

    from fastapi import FastAPI

    from clio_agent.gact.events import EventBus
    from clio_agent.gact.transcript import TurnTranscript
    from clio_agent.gact.types import AgentDef, ErrorInfo, Message, Session
    from clio_agent.gact.workflow_state.schema import WorkflowStateSchema


@dataclass(kw_only=True)
class TurnState:
    """The full working set of one ``_run_turn_in_background`` invocation.

    Fields mirror the former function-scope locals one-for-one, grouped by
    lifecycle: turn identity (set once at construction), turn-scoped infra (set
    once, early, in the linear body), and mutable accumulators (reassigned as the
    turn progresses). ``transcript`` and ``turn_cancel_event`` use
    ``field(init=False)``: they are concrete (non-``Optional``) but only assigned
    once the turn's ledger/cancel wiring is opened in the body, so reads elsewhere
    see the real type without ``Optional`` narrowing noise.
    """

    # --- Identity / frozen (set once at construction) ---
    app: "FastAPI"
    sid: str
    user_text: str
    user_msg: "Message"
    turn_agent_id: str
    sess: "Session"
    bus: "EventBus"
    turn_id: str
    trace_id: str
    retry_attempt_id: str
    native_images: list[Any]

    # --- Turn-scoped infra (set once, early, in the linear body) ---
    transcript: "TurnTranscript" = field(init=False)
    turn_cancel_event: "threading.Event" = field(init=False)
    # #767 Phase C: the turn's active pack workflow_state schema, resolved once
    # (the single resolver seam) in the linear body just before the transcript
    # opens; every delegation/grounding/scrub seam reads it off ``state``.
    workflow_schema: "WorkflowStateSchema" = field(init=False)
    # #767 Phase B: the no-progress watchdog reads these off ``state`` — the
    # progress-timeout window + poll cadence are derived by
    # :func:`~clio_agent.gact.turn_watchdog.make_turn_cancel_event`, and
    # ``cancel_requested`` / ``await_turn_work`` are free functions in
    # ``turn_watchdog.py`` (no longer state-carried closures).
    turn_progress_timeout_s: float = 0.0
    _watchdog_poll_s: float = 0.0
    history_start: dict[int, int] = field(default_factory=dict)
    context_frame: Any = None
    context_file_provenance: Any = None
    enriched_text: str = ""
    memory_search_metadata: dict[str, Any] = field(default_factory=dict)
    # #948 S6 [1]/[4]: observe-later task ids composed into this turn's enriched
    # input during enrichment but NOT yet consumed. Consumed + their delegation
    # terminals emitted only at the commit-to-run seam (immediately before forward),
    # so a turn aborted after enrichment leaves them pending for the next turn.
    pending_notification_task_ids: list[str] = field(default_factory=list)

    # --- Mutable accumulators (reassigned as the turn progresses) ---
    error_info: "Optional[ErrorInfo]" = None
    answer_text: str = ""
    selected_agent: str = ""
    rationale: str = ""
    route_source: str = ""
    route_reason: str = ""
    execution_path: str = ""
    invocation_agent_id: str = ""
    active_agent_id: str = ""
    agent_runtime: dict[str, Any] = field(default_factory=dict)
    dynamic_agent_used: "AgentDef | None" = None
    prompt_resolution: dict[str, Any] = field(default_factory=dict)
    tools_called: list[dict[str, Any]] = field(default_factory=list)
    expert_handoffs: list[dict[str, Any]] = field(default_factory=list)
    proposed_diffs: list[Any] = field(default_factory=list)
    nanoagents: list[Any] = field(default_factory=list)
    thinking_text: str = ""
    turn_tokens: dict[str, int] = field(
        default_factory=lambda: {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
        }
    )
    turn_cost: float = 0.0
    pred: Any = None
    cancelled_turn: bool = False
    assistant_metadata: dict[str, Any] = field(default_factory=dict)


def new_turn_state(
    app: "FastAPI",
    sid: str,
    user_text: str,
    user_msg: "Message",
    turn_agent_id: str,
    *,
    sess: "Session",
    bus: "EventBus",
) -> TurnState:
    """Construct the turn's :class:`TurnState` and bind its turn identity.

    Reproduces the former inline init block of ``_run_turn_in_background``: derive
    the retry-attempt id from the user message, mint the turn/trace ids, pin the
    turn identity contextvar (so ``active_app()``/``active_session_id()`` stay
    reliable on the executor rail for every forward path), and pre-extract native
    images from the user parts.

    ``sess``/``bus`` are resolved and None-guarded by the caller (the session may
    evaporate between POST and background start) and passed in already narrowed,
    so :attr:`TurnState.sess` stays non-``Optional``.
    """

    retry_attempt_id = ""
    if isinstance(user_msg.metadata, dict):
        retry_attempt_id = str(user_msg.metadata.get("retry_attempt_id") or "")
    turn_id = user_msg.id
    trace_id = _semantic_trace_id(turn_id)
    # Bare set, no reset: the whole turn identity (app + session + turn_id +
    # trace_id) must stay live for every later copy_context() snapshot taken
    # during this turn (mirrors the original turn-scoped leak). Establishing
    # app/session here -- not only inside the narrow dynamic-agent forward
    # wrappers -- makes active_app()/active_session_id() reliable on the executor
    # rail for ALL turn paths, incl. the CLIO orchestrator forward (#735 3).
    _ctx.set_turn_identity(app=app, session_id=sid, turn_id=turn_id, trace_id=trace_id)
    from clio_agent.gact.app import _dspy_images_from_parts  # noqa: PLC0415

    native_images = _dspy_images_from_parts(user_msg.parts)
    return TurnState(
        app=app,
        sid=sid,
        user_text=user_text,
        user_msg=user_msg,
        turn_agent_id=turn_agent_id,
        sess=sess,
        bus=bus,
        turn_id=turn_id,
        trace_id=trace_id,
        retry_attempt_id=retry_attempt_id,
        native_images=native_images,
    )
