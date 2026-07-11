"""Single explicit runtime context for a GACT turn / expert / react-step.

Replaces 11 scattered module-level ``ContextVar``s (formerly in ``gact/app.py``)
with ONE ``ContextVar`` carrying a layered, mostly-immutable context object.

This module is a strict stdlib-only LEAF: it imports nothing from ``app``,
``config``, or ``runtime.*`` so those modules can import the accessors here
without creating an import cycle (``app`` imports this; this imports nothing
from the package).

The migration is behavior-preserving. Every transition that used to be a
``ContextVar.set()`` / ``.reset(token)`` is reproduced here as a functional
``dataclasses.replace`` of the immutable :class:`RuntimeContext` plus the same
single-var ``.set()`` / ``.reset(token)`` -- so nested sets compose and a
``reset`` restores the precise prior layer, identical to independent vars whose
``.reset`` restores their prior value. The two intentionally-leaking turn vars
(turn_id / trace_id) and the reassigned-without-reset react trajectory are
reproduced via tokenless bare sets and a mutable :class:`TrajectoryCell`.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass
class TrajectoryCell:
    """Mutable holder for the in-flight react trajectory.

    ``_RetainingReAct.forward`` reassigns the retained trajectory twice within
    one call (clear -> publish-before-extract) WITHOUT a token reset. A mutable
    cell reproduces that exactly: ``forward`` mutates ``.value``; readers read
    ``.value``. Each ``forward`` MUST get a FRESH cell (see
    :func:`install_trajectory_cell`) so a delegated child never publishes into
    its parent's cell.
    """

    value: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnContext:
    """Per-turn identity + the FastAPI app handle.

    Set once per turn; the turn_id / trace_id fields leak for the turn (mirrors
    the old bare ``TURN_ID``/``TRACE_ID`` ``.set`` with no reset, so they stay
    live for every later ``copy_context()`` snapshot taken during the turn).
    """

    app: Any | None = None
    session_id: str = ""  # _ACTIVE_GACT_SESSION_ID
    turn_id: str = ""  # _ACTIVE_GACT_TURN_ID
    trace_id: str = ""  # _ACTIVE_GACT_TRACE_ID
    tool_session_id: str = ""  # _ACTIVE_TOOL_SESSION_ID


@dataclass(frozen=True)
class RuntimeContext:
    """The single object carried by the contextvar.

    Layers per-turn identity (:class:`TurnContext`) with the currently-active
    expert / react-step fields. Immutable: every transition is a
    ``replace(...)`` + ``ContextVar.set(token)`` / ``reset(token)``, preserving
    the exact set/reset structure of the 11 original vars.
    """

    turn: TurnContext = field(default_factory=TurnContext)
    # ---- expert / react-loop layer (formerly the REACT_* + BLUEPRINT vars) ----
    react_scope: str = ""  # _ACTIVE_REACT_SCOPE
    # The declared ``module.kind`` (predict|chain_of_thought|react) of the expert
    # owning ``react_scope``. Resolved ONCE per expert forward and threaded through
    # here so the live-stream emit gate can suppress a `kind: react` expert's
    # redundant EXTRACT fields (reasoning/answer) STRUCTURALLY — never by matching a
    # field name, which would also delete a chain_of_thought expert's visible
    # reasoning (#878). Empty off-scope (main planner / CLI / optimizer).
    react_kind: str = ""  # module.kind of the active expert
    # The current ReAct step's reasoning, set by the react loop BEFORE it invokes
    # the step's tool so the tool observer (which runs synchronously on the react
    # thread) can stamp it onto the ``tool_call`` part — one LLM turn = thought +
    # the tool call, as a single ordered event (#732). Reset at the step boundary.
    step_thought: str = ""  # the model's next_thought for this step
    step_reasoning: str = ""  # the raw reasoning channel for this step
    react_session: str = ""  # _ACTIVE_REACT_SESSION
    react_context_window: int = 0  # _ACTIVE_REACT_CONTEXT_WINDOW
    blueprint_tool_rows: list[dict[str, Any]] | None = None  # _ACTIVE_BLUEPRINT_TOOL_ROWS
    visible_answer_stream: bool = True
    parent_span_id: str = ""  # _ACTIVE_PARENT_SPAN_ID
    trajectory_cell: TrajectoryCell | None = None  # _ACTIVE_REACT_TRAJECTORY (via cell)


# The single live channel. The default is an immutable FROZEN singleton; safe to
# share across contexts because nothing mutates a RuntimeContext in place (only
# the optional TrajectoryCell is mutable, and the default carries none). B039
# (mutable ContextVar default) is a false positive here: the default is frozen.
_DEFAULT_RUNTIME = RuntimeContext()
_RUNTIME: contextvars.ContextVar[RuntimeContext] = contextvars.ContextVar(
    "clio_gact_runtime_context",
    default=_DEFAULT_RUNTIME,  # noqa: B039 - frozen immutable singleton, safe to share
)


# ---------------------------------------------------------------------------
# Reads -- replace every ``_ACTIVE_*.get()``.
# ---------------------------------------------------------------------------
def current() -> RuntimeContext:
    """Return the live :class:`RuntimeContext`."""
    return _RUNTIME.get()


def current_turn() -> TurnContext:
    """Return the live :class:`TurnContext` layer."""
    return _RUNTIME.get().turn


def active_app() -> Any | None:
    """``_ACTIVE_GACT_APP.get()``."""
    return _RUNTIME.get().turn.app


def active_session_id() -> str:
    """``_ACTIVE_GACT_SESSION_ID.get()``."""
    return _RUNTIME.get().turn.session_id


def active_turn_id() -> str:
    """``_ACTIVE_GACT_TURN_ID.get()``."""
    return _RUNTIME.get().turn.turn_id


def active_trace_id() -> str:
    """``_ACTIVE_GACT_TRACE_ID.get()``."""
    return _RUNTIME.get().turn.trace_id


def active_tool_session_id() -> str:
    """``_ACTIVE_TOOL_SESSION_ID.get()``."""
    return _RUNTIME.get().turn.tool_session_id


def active_react_scope() -> str:
    """``_ACTIVE_REACT_SCOPE.get()``."""
    return _RUNTIME.get().react_scope


def active_react_kind() -> str:
    """The declared ``module.kind`` of the expert owning the active react scope.

    One of ``predict`` / ``chain_of_thought`` / ``react`` while an expert forward
    is on the stack; ``""`` off-scope (the main planner, CLI, optimizer). Read at
    the live-stream emit gate to decide, structurally, whether a contract field is
    a react expert's redundant EXTRACT field (#878).
    """
    return _RUNTIME.get().react_kind


def react_extract_field_suppressed(kind: str, field: str, *, answer_is_deliverable: bool) -> bool:
    """Whether a ``kind: react`` expert's contract ``field`` is a redundant EXTRACT
    field that must NOT become a visible transcript part (#878).

    Shared by BOTH visible-emit seams (the io_logging live tap
    ``lm_activity.note_lm_answer_delta`` for nested/synchronous experts, and the
    ``streamify`` pump ``streaming._emit_visible_chunk`` for a top-level program) so
    the kind-gate logic lives in exactly one place.

    A ``kind: react`` expert's visible conversation is its per-step ``next_thought``
    (plus its tool calls). Its final ``ChainOfThought`` EXTRACT emits ``reasoning``
    and ``answer``:

    * ``reasoning`` — the extract's own chain-of-thought, never parent-bound, no
      conversational standing. Always suppressed.
    * ``answer`` — redundant with the delegation return contract (``row["output"]``
      carries the VALUE, rendered behind *show more*), EXCEPT when this LM call is
      the TOP-LEVEL deliverable stream (``answer_is_deliverable``), where the answer
      is the user-facing turn output and must stay visible.

    Only ``react`` is gated. ``chain_of_thought``/``predict`` (and off-scope, empty
    ``kind``) return ``False`` so their ``reasoning`` stays fully visible — the
    reverted attempt deleted exactly those transcripts by suppressing on field name.
    """
    if kind != "react":
        return False
    if field == "reasoning":
        return True
    if field == "answer":
        return not answer_is_deliverable
    return False


def active_step_thought() -> str:
    """The current ReAct step's ``next_thought`` (for the ``tool_call`` part)."""
    return _RUNTIME.get().step_thought


def active_step_reasoning() -> str:
    """The current ReAct step's raw reasoning channel."""
    return _RUNTIME.get().step_reasoning


def active_react_session() -> str:
    """``_ACTIVE_REACT_SESSION.get()``."""
    return _RUNTIME.get().react_session


def active_react_context_window() -> int:
    """``_ACTIVE_REACT_CONTEXT_WINDOW.get()``."""
    return _RUNTIME.get().react_context_window


def active_blueprint_tool_rows() -> list[dict[str, Any]] | None:
    """``_ACTIVE_BLUEPRINT_TOOL_ROWS.get()``."""
    return _RUNTIME.get().blueprint_tool_rows


def active_visible_answer_stream() -> bool:
    """Return whether the active LM ``answer`` field is transcript-visible prose."""

    return _RUNTIME.get().visible_answer_stream


def active_parent_span_id() -> str:
    """``_ACTIVE_PARENT_SPAN_ID.get()``."""
    return _RUNTIME.get().parent_span_id


def active_trajectory() -> dict[str, Any] | None:
    """``_ACTIVE_REACT_TRAJECTORY.get()`` (via the active cell)."""
    cell = _RUNTIME.get().trajectory_cell
    return cell.value if cell is not None else None


# ---------------------------------------------------------------------------
# Set/reset transitions -- replace ``_ACTIVE_*.set()`` / ``.reset()``.
#
# Each setter returns a ``contextvars.Token`` for symmetric reset, preserving
# the EXACT set/reset bracketing of the originals. Because ``replace`` snapshots
# the *current* RuntimeContext, nested sets compose correctly and ``reset(token)``
# restores the precise prior layer.
# ---------------------------------------------------------------------------
def reset(token: contextvars.Token[RuntimeContext]) -> None:
    """Restore the layer captured by ``token`` (``ContextVar.reset``)."""
    _RUNTIME.reset(token)


def set_app(app: Any) -> contextvars.Token[RuntimeContext]:
    """Set ``turn.app`` (the ``_gact_app_context`` body)."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, turn=replace(cur.turn, app=app)))


def set_session_id(sid: str) -> contextvars.Token[RuntimeContext]:
    """Set ``turn.session_id`` (executor entry + copy seed)."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, turn=replace(cur.turn, session_id=sid)))


def set_tool_session_id(sid: str) -> contextvars.Token[RuntimeContext]:
    """Set ``turn.tool_session_id`` (the ``_tool_session_context`` body)."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, turn=replace(cur.turn, tool_session_id=sid)))


def set_turn_identity(
    *,
    app: Any,
    session_id: str,
    turn_id: str,
    trace_id: str,
) -> None:
    """Establish the whole turn layer at once. BARE set, NO token.

    Mirrors the turn-scoped leak at the top of ``_run_turn_in_background`` where
    ``TURN_ID``/``TRACE_ID`` are bare-set with no reset so they stay live for
    every later ``copy_context()`` snapshot of the turn. ``tool_session_id`` is
    carried forward from the current turn (it is set independently via
    :func:`set_tool_session_id`).
    """
    cur = _RUNTIME.get()
    _RUNTIME.set(
        replace(
            cur,
            turn=TurnContext(
                app=app,
                session_id=session_id,
                turn_id=turn_id,
                trace_id=trace_id,
                tool_session_id=cur.turn.tool_session_id,
            ),
        )
    )


def set_turn_id(turn_id: str) -> None:
    """Bare set of ``turn.turn_id`` only (``_ACTIVE_GACT_TURN_ID.set``, no reset)."""
    cur = _RUNTIME.get()
    _RUNTIME.set(replace(cur, turn=replace(cur.turn, turn_id=turn_id)))


def set_trace_id(trace_id: str) -> None:
    """Bare set of ``turn.trace_id`` only (``_ACTIVE_GACT_TRACE_ID.set``, no reset)."""
    cur = _RUNTIME.get()
    _RUNTIME.set(replace(cur, turn=replace(cur.turn, trace_id=trace_id)))


def set_turn_id_token(turn_id: str) -> contextvars.Token[RuntimeContext]:
    """Token-returning set of ``turn.turn_id`` (for the back-compat ``_CompatVar``).

    Core uses the tokenless :func:`set_turn_id`; the legacy ``_ACTIVE_GACT_TURN_ID``
    proxy callers (tests that ``.set()`` then ``.reset()``) need a token.
    """
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, turn=replace(cur.turn, turn_id=turn_id)))


def set_trace_id_token(trace_id: str) -> contextvars.Token[RuntimeContext]:
    """Token-returning set of ``turn.trace_id`` (for the back-compat ``_CompatVar``).

    Core uses the tokenless :func:`set_trace_id`; the legacy ``_ACTIVE_GACT_TRACE_ID``
    proxy callers (tests that ``.set()`` then ``.reset()``) need a token.
    """
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, turn=replace(cur.turn, trace_id=trace_id)))


def set_blueprint_tool_rows(
    rows: list[dict[str, Any]] | None,
) -> contextvars.Token[RuntimeContext]:
    """Set ``blueprint_tool_rows`` (``_ACTIVE_BLUEPRINT_TOOL_ROWS.set``).

    Stores the SAME list object (no copy) so a wrapped tool's
    ``active_blueprint_tool_rows().append(...)`` mutates the caller's list --
    preserving the read-and-mutate recording behavior.
    """
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, blueprint_tool_rows=rows))


def set_visible_answer_stream(visible: bool) -> contextvars.Token[RuntimeContext]:
    """Set whether this LM call's ``answer`` field may stream to the transcript."""

    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, visible_answer_stream=bool(visible)))


def set_react_scope(scope: str, kind: str = "") -> contextvars.Token[RuntimeContext]:
    """Set ``react_scope`` and the expert's ``react_kind`` in ONE transition.

    ``react_kind`` rides the same token as ``react_scope`` (they enter and leave
    scope together at each expert forward site), so the single-var layer restores
    both on ``reset`` with no separate LIFO ordering to get wrong (#878)."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, react_scope=scope, react_kind=kind))


def set_react_kind(kind: str) -> contextvars.Token[RuntimeContext]:
    """Set ``react_kind`` alone (its own token). Used where the kind is threaded
    independently of the scope, e.g. in tests; production folds it into
    :func:`set_react_scope` (#878)."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, react_kind=kind))


def set_step_thought(thought: str, reasoning: str = "") -> contextvars.Token[RuntimeContext]:
    """Set this step's ``step_thought``/``step_reasoning`` so the tool observer can
    stamp the model's reasoning onto the ``tool_call`` part it emits (#732)."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, step_thought=thought, step_reasoning=reasoning))


def set_react_session(session: str) -> contextvars.Token[RuntimeContext]:
    """Set ``react_session`` (``_ACTIVE_REACT_SESSION.set``)."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, react_session=session))


def set_react_window(window: int) -> contextvars.Token[RuntimeContext]:
    """Set ``react_context_window`` (``_ACTIVE_REACT_CONTEXT_WINDOW.set``)."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, react_context_window=window))


def set_react_context_window(window: int) -> None:
    """Bare functional update of ``react_context_window`` (NO token).

    Reproduces the one mid-body window mutation in a test (and any caller that
    re-sets the window without a reset). In src the window is only set via
    :func:`set_react_window`; this exists so a tokenless re-set works too.
    """
    cur = _RUNTIME.get()
    _RUNTIME.set(replace(cur, react_context_window=window))


def set_parent_span(span_id: str) -> contextvars.Token[RuntimeContext]:
    """Set ``parent_span_id`` (``_ACTIVE_PARENT_SPAN_ID.set``)."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, parent_span_id=span_id))


def install_trajectory_cell() -> TrajectoryCell:
    """Install a FRESH ``TrajectoryCell`` (value=None) as a BARE set, NO token.

    Reproduces ``_ACTIVE_REACT_TRAJECTORY.set(None)`` at the top of
    ``_RetainingReAct.forward`` (a clear with no reset). Each ``forward`` calls
    this first so a delegated child -- running in its own copied context -- gets
    its OWN cell and cannot publish into the parent's retained trajectory.
    Returns the new cell so the caller may keep a direct handle if it wants.
    """
    cell = TrajectoryCell(value=None)
    cur = _RUNTIME.get()
    _RUNTIME.set(replace(cur, trajectory_cell=cell))
    return cell


def publish_trajectory(value: dict[str, Any] | None) -> None:
    """Mutate the active cell IN PLACE.

    Reproduces ``_ACTIVE_REACT_TRAJECTORY.set({...})`` (reassignment without a
    token reset) at the publish-before-extract point. No-op when no cell is
    installed (mirrors a forward that never installed one).
    """
    cell = _RUNTIME.get().trajectory_cell
    if cell is not None:
        cell.value = value


def install_trajectory(value: dict[str, Any] | None) -> TrajectoryCell:
    """Install a fresh cell pre-seeded with ``value`` (bare set, NO token).

    Convenience for callers (notably tests of the re-extract path) that need a
    populated retained trajectory in scope without driving a full ``forward``.
    """
    cell = TrajectoryCell(value=value)
    cur = _RUNTIME.get()
    _RUNTIME.set(replace(cur, trajectory_cell=cell))
    return cell
