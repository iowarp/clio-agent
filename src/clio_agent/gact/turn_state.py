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

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.runtime.constants import _CTX_MAX_BYTES
from clio_agent.gact.runtime.globals import _semantic_trace_id

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.events import EventBus
    from clio_agent.gact.transcript import TurnTranscript
    from clio_agent.gact.types import AgentDef, ErrorInfo, Message, Session
    from clio_agent.gact.workflow_state.schema import WorkflowStateSchema

#: The turn's off-loop prologue (:func:`~clio_agent.gact.turn_start_offloop.
#: prepare_turn_off_loop`) lifecycle, replacing the old single completion boolean
#: (L1 slice, #1339 follow-on). Four REAL states instead of one bit:
#:
#: * ``not_started`` -- the default. The prologue callable never began running
#:   (a cancel landed while it was still queued on the executor -- see
#:   ``turn_start_offloop.DEFERRED_JOB_ORPHANED`` for the matching proof on the
#:   deferred transcript job).
#: * ``running`` -- set as the FIRST statement inside ``prepare_turn_off_loop``.
#:   Stays ``running`` (never reaches ``completed``/``failed``) ONLY when a
#:   ``TurnCancelledDuringPrologue`` (or a hook's ``HookCancelled``) escapes the
#:   function -- i.e. the turn's cancel token tripped mid-prologue.
#: * ``completed`` -- the prologue ran to its own end (whatever outcome string
#:   it returned: proceed/blocked/deferred). Every prologue-derived field
#:   (context_frame/context_file_provenance/enriched_text/memory_search_metadata)
#:   is assigned by this point.
#: * ``failed`` -- an exception OTHER than a cancel signal escaped the
#:   prologue (e.g. the deferred transcript job raising ``TranscriptIngestError``).
#:   The real exception is captured on :attr:`TurnState.prologue_error` so the
#:   guard can settle with its REAL cause instead of a fabricated one.
ProloguePhase = Literal["not_started", "running", "completed", "failed"]

PROLOGUE_NOT_STARTED: ProloguePhase = "not_started"
PROLOGUE_RUNNING: ProloguePhase = "running"
PROLOGUE_COMPLETED: ProloguePhase = "completed"
PROLOGUE_FAILED: ProloguePhase = "failed"


class TurnCancelledDuringPrologue(RuntimeError):
    """The turn's cancel token tripped while its off-loop prologue was running.

    Raised directly by :func:`~clio_agent.gact.turn_start_offloop.prepare_turn_off_loop`'s
    cooperative-cancel checkpoints (one before every prologue step) and treated
    identically to a :class:`~clio_agent.gact.hooks.wire.HookCancelled` escaping a
    killed ``UserPromptSubmit`` hook subprocess: BOTH leave
    :attr:`TurnState.prologue_phase` at ``"running"`` (never ``"failed"``), which is
    the ONE signal :func:`~clio_agent.gact.turn_prologue_guard.run_finalize_or_settle_prologue_gap`
    needs to settle the turn with this typed reason instead of a fabricated
    ``turn_prologue_never_ran`` or a generic ``agent_error``.

    Carries the typed settle reason through ``settle_failed_finalize`` unmodified,
    mirroring ``turn_prologue_guard._TurnPrologueNeverRan``'s own contract.
    """

    settle_reason = "turn_cancelled_during_prologue"
    settle_error_code = "turn_cancelled_during_prologue"

    def __init__(self, turn_id: str) -> None:
        super().__init__(
            f"turn {turn_id} was cancelled while its off-loop prologue "
            "(prepare_turn_off_loop) was still running"
        )
        self.turn_id = turn_id


class DeferredTranscriptJob:
    """The user message's ARC persist, handed to the turn to run FIRST — exactly once.

    #1334 moved this write out of ``POST /messages`` so the accept path never waits on a
    store RPC: the turn's off-loop prologue runs it before anything else, which keeps the
    user message's atoms ahead of everything the turn appends. A turn task that never
    reaches its prologue, though — cancelled before its first step (a stop landing during
    turn start, the shutdown drain) — used to drop the job on the floor. The message then
    lives in the in-memory ledger and the local store with NO atoms, and because
    ``transcript_projection.materialize_ledger`` takes its ``has_atoms`` branch on any
    session that has ever completed a turn, the message VANISHES from the transcript on
    the next rehydrate.

    The holder makes the hand-off single-consumer (:meth:`take` returns the job once, to
    whoever asks first) so :func:`spawn_user_turn` can arm a done-callback that flushes an
    unconsumed job off the loop without ever risking a double persist.
    """

    def __init__(self, job: Optional[Callable[[], None]]) -> None:
        self._job = job
        self._lock = threading.Lock()

    def take(self) -> Optional[Callable[[], None]]:
        """Claim the job. Returns it to the FIRST caller only; ``None`` thereafter."""

        with self._lock:
            job, self._job = self._job, None
            return job


def _unset_context_file_provenance() -> dict[str, Any]:
    """The typed empty record ``context_file_provenance`` carries before the
    prologue assigns a real one (:func:`clio_agent.gact.enrichment._context_file_
    turn_provenance`'s own shape -- ``status``/``count``/``max_inline_bytes``/
    ``files``).

    A turn whose prologue path skips BOTH of ``turn_start_offloop.py``'s own
    assignments (the unconditional ``status="prepared"`` line and the
    ``except _ContextFileAccessError`` overwrite -- e.g. a turn that never runs
    :func:`~clio_agent.gact.turn_start_offloop.prepare_turn_off_loop` at all)
    still reaches :mod:`clio_agent.gact.turn_finalize` with a dict it can safely
    subscript (``state.context_file_provenance["files"]``) instead of ``None``.
    ``status="unset"`` keeps this distinguishable from a real empty-attachments
    turn (``status="prepared"``, also ``files: []``) if ever inspected.
    """

    return {"status": "unset", "count": 0, "max_inline_bytes": _CTX_MAX_BYTES, "files": []}


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
    native_files: list[Any] = field(default_factory=list)

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
    # L1 slice (#1339 follow-on): replaces the old single completion boolean
    # with the four-state :data:`ProloguePhase` (see its docstring above) --
    # ``turn_prologue_guard.run_finalize_or_settle_prologue_gap`` is the ONE reader,
    # branching on the phase instead of a single None-check-shaped bit.
    prologue_phase: ProloguePhase = field(default=PROLOGUE_NOT_STARTED, init=False)
    # The REAL exception ``prepare_turn_off_loop`` caught when the phase is
    # ``"failed"`` -- carried so the guard can settle with its actual cause
    # instead of a fabricated ``turn_prologue_never_ran``.
    prologue_error: "Optional[BaseException]" = field(default=None, init=False, repr=False)
    history_start: dict[int, int] = field(default_factory=dict)
    context_frame: Any = None
    context_file_provenance: dict[str, Any] = field(default_factory=_unset_context_file_provenance)
    enriched_text: str = ""
    memory_search_metadata: dict[str, Any] = field(default_factory=dict)
    # #948 S6 [1]/[4]: observe-later task ids composed into this turn's enriched
    # input during enrichment but NOT yet consumed. Consumed + their delegation
    # terminals emitted only at the commit-to-run seam (immediately before forward),
    # so a turn aborted after enrichment leaves them pending for the next turn.
    pending_notification_task_ids: list[str] = field(default_factory=list)
    # #1334: the user message's deferred ARC transcript persist (staged on the accept
    # path with ``atoms_minted=True``); the turn's off-loop setup runs it FIRST, must-
    # succeed. ``None`` when the accept path minted inline (tests / legacy callers).
    transcript_job: "Optional[DeferredTranscriptJob]" = None
    # #1334: the attached-context failure the off-loop setup observed, raised as
    # ``_ContextFileAccessError`` at the commit-to-run seam (formerly a body local).
    context_file_error: "Optional[ErrorInfo]" = None

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
    last_prompt_usage: dict[str, Any] = field(default_factory=dict)
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
    images and PDFs from the user parts.

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
    behavior = user_msg.metadata.get("behavior") if isinstance(user_msg.metadata, dict) else None
    execution_mode = str(behavior.get("execution_mode") or "") if isinstance(behavior, dict) else ""
    execution_blueprint_id = ""
    if execution_mode == "deep_research":
        from clio_agent.gact.message_contract import (  # noqa: PLC0415
            DEEP_RESEARCH_BLUEPRINT_ID,
        )

        execution_blueprint_id = DEEP_RESEARCH_BLUEPRINT_ID
    _ctx.set_turn_identity(
        app=app,
        session_id=sid,
        turn_id=turn_id,
        trace_id=trace_id,
        execution_blueprint_id=execution_blueprint_id,
    )
    from clio_agent.gact.app import _dspy_images_from_parts  # noqa: PLC0415
    from clio_agent.gact.messaging import _dspy_files_from_parts  # noqa: PLC0415
    from clio_agent.gact.native_delivery_outcome import (  # noqa: PLC0415
        settle_native_deliveries,
    )

    workspace_id = str(getattr(sess, "workspace_id", "") or "")
    native_images = _dspy_images_from_parts(
        user_msg.parts,
        app=app,
        workspace_id=workspace_id,
    )
    native_files = _dspy_files_from_parts(
        user_msg.parts,
        app=app,
        workspace_id=workspace_id,
    )
    # The attach pass is done, so the ledger can stop reporting the PLAN as the
    # outcome: every native-planned row is stamped delivered, or not-delivered
    # with the typed reason the attach step recorded.
    settle_native_deliveries(
        app, workspace_id=workspace_id, message_id=user_msg.id, parts=list(user_msg.parts)
    )
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
        native_files=native_files,
    )
