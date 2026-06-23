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
    react_session: str = ""  # _ACTIVE_REACT_SESSION
    react_context_window: int = 0  # _ACTIVE_REACT_CONTEXT_WINDOW
    blueprint_tool_rows: list[dict[str, Any]] | None = None  # _ACTIVE_BLUEPRINT_TOOL_ROWS
    parent_span_id: str = ""  # _ACTIVE_PARENT_SPAN_ID
    run_span_id: str = ""  # the active ReAct-step (agent-run) span; the lm_io seam reads it
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


def active_react_session() -> str:
    """``_ACTIVE_REACT_SESSION.get()``."""
    return _RUNTIME.get().react_session


def active_react_context_window() -> int:
    """``_ACTIVE_REACT_CONTEXT_WINDOW.get()``."""
    return _RUNTIME.get().react_context_window


def active_blueprint_tool_rows() -> list[dict[str, Any]] | None:
    """``_ACTIVE_BLUEPRINT_TOOL_ROWS.get()``."""
    return _RUNTIME.get().blueprint_tool_rows


def active_parent_span_id() -> str:
    """``_ACTIVE_PARENT_SPAN_ID.get()``."""
    return _RUNTIME.get().parent_span_id


def active_run_span() -> str:
    """The active ReAct-step (agent-run) span id, or ``""``. Set per step in
    ``_RetainingReAct.forward``; read by the lm_io capture seam so a captured LM
    call lands on the right step."""
    return _RUNTIME.get().run_span_id


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


def set_react_scope(scope: str) -> contextvars.Token[RuntimeContext]:
    """Set ``react_scope`` (``_ACTIVE_REACT_SCOPE.set``)."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, react_scope=scope))


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


def set_run_span(span_id: str) -> contextvars.Token[RuntimeContext]:
    """Set ``run_span_id`` (the active ReAct-step / agent-run span). Set per step in
    ``_RetainingReAct.forward`` (mirrors :func:`set_parent_span`); the lm_io seam
    reads it via :func:`active_run_span` so a captured LM call is keyed to the step."""
    cur = _RUNTIME.get()
    return _RUNTIME.set(replace(cur, run_span_id=span_id))


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
