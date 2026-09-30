"""Unit tests for the single runtime-context module (clio_agent.gact.context).

Covers the #714 migration's leaf module directly: dataclass construction +
defaults, the set/replace/reset round-trip restoring the prior layer, and the
executor-crossing round-trip (parent unaffected by a child copy's mutation).
"""

from __future__ import annotations

import contextvars

from clio_agent.gact import context as ctx


def test_turn_context_defaults():
    tc = ctx.TurnContext()
    assert tc.app is None
    assert tc.session_id == ""
    assert tc.turn_id == ""
    assert tc.trace_id == ""
    assert tc.tool_session_id == ""
    assert tc.execution_blueprint_id == ""


def test_runtime_context_defaults():
    rc = ctx.RuntimeContext()
    assert isinstance(rc.turn, ctx.TurnContext)
    assert rc.react_scope == ""
    assert rc.react_session == ""
    assert rc.react_context_window == 0
    assert rc.blueprint_tool_rows is None
    assert rc.parent_span_id == ""


def test_runtime_context_construction():
    turn = ctx.TurnContext(app=object(), session_id="s", turn_id="t", trace_id="tr")
    rc = ctx.RuntimeContext(
        turn=turn,
        react_scope="scope",
        react_session="sess",
        react_context_window=4096,
        blueprint_tool_rows=[{"name": "x"}],
        parent_span_id="span",
    )
    assert rc.turn is turn
    assert rc.react_scope == "scope"
    assert rc.react_context_window == 4096
    assert rc.blueprint_tool_rows == [{"name": "x"}]
    assert rc.parent_span_id == "span"


def test_set_app_session_round_trip():
    base = ctx.current()
    app_obj = object()
    app_token = ctx.set_app(app_obj)
    sess_token = ctx.set_session_id("sid-1")
    try:
        assert ctx.active_app() is app_obj
        assert ctx.active_session_id() == "sid-1"
        # The turn layer carries both fields on the SAME object.
        assert ctx.current_turn().app is app_obj
        assert ctx.current_turn().session_id == "sid-1"
    finally:
        ctx.reset(sess_token)
        ctx.reset(app_token)
    # Reset restores the prior layer exactly.
    assert ctx.current() is base
    assert ctx.active_app() is None
    assert ctx.active_session_id() == ""


def test_replace_reset_restores_prior_layer():
    scope_token = ctx.set_react_scope("outer")
    try:
        assert ctx.active_react_scope() == "outer"
        inner_token = ctx.set_react_scope("inner")
        try:
            assert ctx.active_react_scope() == "inner"
        finally:
            ctx.reset(inner_token)
        # Back to the outer layer (the snapshot captured before the inner set).
        assert ctx.active_react_scope() == "outer"
    finally:
        ctx.reset(scope_token)
    assert ctx.active_react_scope() == ""


def test_nested_react_layer_lifo_reset():
    """Reverse-LIFO reset of scope/session/window unwinds the single-var stack."""
    scope_token = ctx.set_react_scope("agentA")
    session_token = ctx.set_react_session("sessA")
    window_token = ctx.set_react_window(8192)
    try:
        assert ctx.active_react_scope() == "agentA"
        assert ctx.active_react_session() == "sessA"
        assert ctx.active_react_context_window() == 8192
    finally:
        ctx.reset(window_token)
        ctx.reset(session_token)
        ctx.reset(scope_token)
    assert ctx.active_react_scope() == ""
    assert ctx.active_react_session() == ""
    assert ctx.active_react_context_window() == 0


def test_parent_span_nested_dance():
    """Mirror the ClioReAct expert/step parent-span set/reset dance."""
    expert_token = ctx.set_parent_span("EXPERT")
    try:
        assert ctx.active_parent_span_id() == "EXPERT"
        step_token = ctx.set_parent_span("STEP")
        try:
            assert ctx.active_parent_span_id() == "STEP"
        finally:
            ctx.reset(step_token)
        # Back to the expert span at the step boundary.
        assert ctx.active_parent_span_id() == "EXPERT"
    finally:
        ctx.reset(expert_token)
    assert ctx.active_parent_span_id() == ""


def test_blueprint_tool_rows_stores_identity():
    rows: list[dict] = []
    token = ctx.set_blueprint_tool_rows(rows)
    try:
        # The field holds the SAME list object so a wrapped tool's append is visible.
        active = ctx.active_blueprint_tool_rows()
        assert active is rows
        active.append({"name": "tool"})
        assert rows == [{"name": "tool"}]
    finally:
        ctx.reset(token)
    assert ctx.active_blueprint_tool_rows() is None


def test_set_turn_identity_bare_and_preserves_tool_session():
    base_token = ctx._RUNTIME.set(ctx.RuntimeContext())
    try:
        ts_token = ctx.set_tool_session_id("tool-sess")
        try:
            app_obj = object()
            ctx.set_turn_identity(
                app=app_obj,
                session_id="sid",
                turn_id="tid",
                trace_id="trid",
                execution_blueprint_id="deep-researcher",
            )
            assert ctx.active_app() is app_obj
            assert ctx.active_session_id() == "sid"
            assert ctx.active_turn_id() == "tid"
            assert ctx.active_trace_id() == "trid"
            # tool_session_id is carried forward across the turn-identity set.
            assert ctx.active_tool_session_id() == "tool-sess"
            assert ctx.active_execution_blueprint_id() == "deep-researcher"
        finally:
            # ts_token was set BEFORE the tokenless set_turn_identity; resetting it
            # restores the pre-tool-session layer (the tokenless set is discarded).
            ctx.reset(ts_token)
    finally:
        ctx._RUNTIME.reset(base_token)


def test_executor_crossing_child_set_does_not_leak_to_parent():
    """A copy_context() thunk that SETS the var (new RuntimeContext) cannot leak
    that back to the parent: contextvar .set in a copied context is local to it."""
    app_token = ctx.set_app("parent-app")
    try:
        parent_session = ctx.active_session_id()
        snapshot = contextvars.copy_context()

        def _child() -> str:
            tok = ctx.set_session_id("child-sid")
            try:
                assert ctx.active_session_id() == "child-sid"
                # The app set in the parent is visible in the copy.
                assert ctx.active_app() == "parent-app"
                return ctx.active_session_id()
            finally:
                ctx.reset(tok)

        assert snapshot.run(_child) == "child-sid"
        # Parent is unaffected by the child copy's set.
        assert ctx.active_session_id() == parent_session
        assert ctx.active_app() == "parent-app"
    finally:
        ctx.reset(app_token)
