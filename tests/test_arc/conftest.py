"""Shared fixtures/helpers for the ARC live-context-plane acceptance tests.

The acceptance contract is observed at the LM boundary: a ``PromptRecorder``
captures the exact ``messages`` dspy sends, and the live plane is exercised
through the REAL retaining react loop (``_RetainingReActV2`` — the only loop
since v0.8.0; not a stub) driven by a scripted ``DummyLM`` so the loop is
deterministic.
"""

from __future__ import annotations

import contextlib
import types
from typing import Any, Iterator

import dspy
import pytest

import clio_agent.gact.app as app
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx


@pytest.fixture(params=["local", "cte"])
def arc(request, tmp_path) -> Iterator[ARCMemory]:
    """A fresh ARCMemory, exercised on BOTH backends.

    The acceptance contract must hold identically whether ARC persists through the
    fast LocalFS store or the production clio-core runtime, so every test using
    this fixture runs once per backend. ``local`` is isolated by ``tmp_path``; ``cte``
    shares the process-global in-process runtime (the first param boots it, the rest
    connect), so it is cleared at setup + teardown for per-test isolation. The ``cte``
    leg skips when the binding is absent (binding-free CI keeps the ``local`` leg).
    """
    backend = request.param
    if backend == "cte":
        pytest.importorskip("clio_cte_core_ext")
        from clio_agent.arc.storage import make_arc_store

        memory = ARCMemory(store=make_arc_store(backend="cte"))
        memory.clear_all()  # fresh start on the shared runtime
        try:
            yield memory
        finally:
            memory.clear_all()
        return
    yield ARCMemory(data_dir=str(tmp_path / "arc"))


@contextlib.contextmanager
def live_plane_context(
    arc_memory: ARCMemory,
    *,
    session: str = "s1",
    scope: str = "agentA",
    window: int = 0,
) -> Iterator[None]:
    """Set the runtime context the live plane reads (app handle, scope, session,
    and the context window that drives auto-compaction)."""
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc_memory))
    # Layer the turn app, then scope/session/window on the single runtime var.
    # Reset in strict reverse-LIFO of the sets (window -> session -> scope -> app)
    # so the single-var stack unwinds cleanly. (#714)
    app_token = ctx.set_app(fake_app)
    scope_token = ctx.set_react_scope(scope)
    session_token = ctx.set_react_session(session)
    window_token = ctx.set_react_window(window)
    try:
        yield
    finally:
        ctx.reset(window_token)
        ctx.reset(session_token)
        ctx.reset(scope_token)
        ctx.reset(app_token)


def make_react_agent(tools: list[Any] | None = None) -> Any:
    """Build a real retaining-react (V2) instance over a trivial signature."""

    def search(q: str) -> str:
        """A search tool."""
        return "SEARCH_RESULT"

    react_cls = app._retaining_react_cls()
    return react_cls("question -> answer", tools=tools or [dspy.Tool(search)])


# (v0.8.0) The classic byte-equality helpers ``stock_format_trajectory`` /
# ``expected_trajectory_dict`` died with the classic loop; the V2 references live
# in tests/test_arc/test_reactv2_wire_byte_equality.py (expected_history_messages).
