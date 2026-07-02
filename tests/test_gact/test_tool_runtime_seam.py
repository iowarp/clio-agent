"""Step 3 (#735 unified-concurrency §2 + §4 Site 1): the tools-layer runtime seam.

Two failing-first regressions for the ``ToolRuntimeHooks`` seam:

1. **Two-app hooks isolation on the ORCHESTRATOR rail.** Two ``build_app()``
   instances A/B in one process, each with a DISTINCT ``pending_tool_observer``.
   App B is built last and owns the single retained app-less fallback bundle. A
   real MCP tool is driven via ``SyncMCPToolExecutor.call_tool`` on a
   ``contextvars.copy_context()`` snapshot taken inside app A's *orchestrator*
   turn identity (the keystone-bound ``set_turn_identity(app=A)`` layer — the
   rail that binds NO per-turn ``_tool_session_context`` on the sync forward
   path). A's observer MUST see the call; B's must not.

   ``call_tool`` resolves ``current_tool_runtime()`` → the installed resolver
   dispatches on ``active_app()`` == A → A's ``pending_tool_observer``; B's
   fallback bundle is never consulted because an app resolved.

2. **App-less resolve emits a structured reason.** With no app resolvable and a
   retained fallback bundle carrying a gate/observer, ``current_tool_runtime()``
   returns that bundle AND records a ``tool_runtime_appless_fallback`` reason
   (no silent gate drop). With an empty fallback it records
   ``tool_runtime_unresolved``.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
from pathlib import Path
from typing import Any

from fastmcp import Client, FastMCP

from clio_agent.gact import context as _ctx
from clio_agent.gact.app import build_app
from clio_agent.tools.execution import (
    SyncMCPToolExecutor,
    ToolRuntimeHooks,
    set_tool_runtime_fallback,
)


def _echo_server() -> FastMCP:
    server = FastMCP("seam-demo")

    @server.tool()
    def echo(text: str) -> str:
        """Echo the text back."""
        return text

    return server


def _override_hooks(app: Any, observer: Any) -> None:
    """Pin ONLY the observer hook on this app; drop gate/interceptor/cancel so the
    test isolates observer routing (a real permission gate could deny)."""

    app.state.pending_tool_observer = observer
    app.state.pending_permission_gate = None
    app.state.pending_cancellation_checker = None
    app.state.pending_tool_interceptor = None


def test_two_app_hooks_isolation_on_orchestrator_rail(tmp_path: Path) -> None:
    app_a = build_app(sessions_path=tmp_path / "a.json")
    app_b = build_app(sessions_path=tmp_path / "b.json")

    a_observed: list[tuple[str, str | None]] = []
    b_observed: list[tuple[str, str | None]] = []

    _override_hooks(
        app_a,
        lambda name, args, phase, error=None, result=None: a_observed.append((name, phase)),
    )
    _override_hooks(
        app_b,
        lambda name, args, phase, error=None, result=None: b_observed.append((name, phase)),
    )

    # App B, built last, owns the single retained app-less fallback bundle (the
    # last-installed net). The resolver dispatching on active_app()==A must win
    # over it, so A's call never touches B's fallback observer.
    set_tool_runtime_fallback(
        ToolRuntimeHooks(
            tool_observer=lambda name, args, phase, error=None, result=None: b_observed.append(
                (name, phase)
            )
        )
    )

    executor = SyncMCPToolExecutor(_echo_server(), timeout=5.0, client_factory=Client)
    try:

        def turn_body() -> str:
            # The keystone binds the WHOLE turn identity to app A on the
            # orchestrator rail (bare set, no _tool_session_context).
            _ctx.set_turn_identity(app=app_a, session_id="sid-a", turn_id="t1", trace_id="tr1")
            snapshot = contextvars.copy_context()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(snapshot.run, executor.call_tool, "echo", {"text": "hi"})
                return fut.result(timeout=10)

        # Run the turn body in its OWN copy_context so the bare set_turn_identity
        # cannot leak app A into the test thread's context.
        result = contextvars.copy_context().run(turn_body)
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
        executor.close()

    assert "hi" in result
    # A's turn telemetry landed on A's observer (via the resolver dispatch)...
    assert a_observed == [("echo", "started"), ("echo", "completed")], a_observed
    # ...and B's fallback observer saw nothing — no cross-app contamination.
    assert b_observed == [], b_observed


def test_appless_resolve_emits_structured_reason() -> None:
    """current_tool_runtime() app-less MUST record a typed reason, never silently
    drop a gate. Fully controls the module's hook state (resolver + fallback) so the
    outcome is deterministic regardless of suite ordering. Reads the reason via a
    before/after slice + membership so a concurrent app-less emit cannot flake it.
    """

    import clio_agent.tools.execution as _ex  # noqa: PLC0415

    def _gate(_name: object, _args: object) -> str:
        return "allow"

    def _observer(*_a: object, **_k: object) -> None:
        return None

    saved = (
        _ex._TOOL_RUNTIME_RESOLVER,
        _ex._FALLBACK_TOOL_RUNTIME,
    )
    try:
        # Deterministic app-less state: no resolver, so the ONLY hook source is the
        # retained fallback bundle set per case below.
        _ex.set_tool_runtime_resolver(None)

        # (a) fallback carrying a gate/observer -> appless_fallback.
        _ex.set_tool_runtime_fallback(
            _ex.ToolRuntimeHooks(permission_gate=_gate, tool_observer=_observer)
        )
        before = len(_ex.recorded_tool_runtime_reasons())
        hooks = _ex.current_tool_runtime()
        assert hooks.permission_gate is _gate
        assert hooks.tool_observer is _observer
        emitted = [r["reason"] for r in _ex.recorded_tool_runtime_reasons()[before:]]
        assert "tool_runtime_appless_fallback" in emitted, emitted

        # (b) empty fallback -> unresolved (still loud, still no silent drop).
        _ex.set_tool_runtime_fallback(_ex.ToolRuntimeHooks())
        before2 = len(_ex.recorded_tool_runtime_reasons())
        empty = _ex.current_tool_runtime()
        assert empty.permission_gate is None and empty.tool_observer is None
        emitted2 = [r["reason"] for r in _ex.recorded_tool_runtime_reasons()[before2:]]
        assert "tool_runtime_unresolved" in emitted2, emitted2
    finally:
        (
            _ex._TOOL_RUNTIME_RESOLVER,
            _ex._FALLBACK_TOOL_RUNTIME,
        ) = saved
