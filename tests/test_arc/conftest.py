"""Shared fixtures/helpers for the ARC live-context-plane acceptance tests.

The acceptance contract is observed at the LM boundary: a ``PromptRecorder``
captures the exact ``messages`` dspy sends, and the live plane is exercised
through the REAL ``_RetainingReAct`` machinery (not a stub) driven by a scripted
``DummyLM`` so the loop is deterministic.
"""

from __future__ import annotations

import contextlib
import types
from typing import Any, Iterator

import dspy
import pytest

import clio_agent.gact.app as app
from clio_agent.arc.memory import ARCMemory


@pytest.fixture
def arc(tmp_path) -> ARCMemory:
    """A fresh ARCMemory backed by the test's tmp dir."""
    return ARCMemory(data_dir=str(tmp_path / "arc"))


@contextlib.contextmanager
def live_plane_context(
    arc_memory: ARCMemory,
    *,
    session: str = "s1",
    scope: str = "agentA",
    window: int = 0,
) -> Iterator[None]:
    """Set the gact contextvars the live plane reads (app handle, scope, session,
    and the context window that drives auto-compaction)."""
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc_memory))
    tokens = [
        app._ACTIVE_GACT_APP.set(fake_app),
        app._ACTIVE_REACT_SCOPE.set(scope),
        app._ACTIVE_REACT_SESSION.set(session),
        app._ACTIVE_REACT_CONTEXT_WINDOW.set(window),
    ]
    try:
        yield
    finally:
        app._ACTIVE_GACT_APP.reset(tokens[0])
        app._ACTIVE_REACT_SCOPE.reset(tokens[1])
        app._ACTIVE_REACT_SESSION.reset(tokens[2])
        app._ACTIVE_REACT_CONTEXT_WINDOW.reset(tokens[3])


def make_react_agent(tools: list[Any] | None = None) -> Any:
    """Build a real _RetainingReAct instance over a trivial signature."""

    def search(q: str) -> str:
        """A search tool."""
        return "SEARCH_RESULT"

    react_cls = app._retaining_react_cls()
    return react_cls("question -> answer", tools=tools or [dspy.Tool(search)])


def stock_format_trajectory(agent: Any, keys: dict[str, Any]) -> str:
    """Format a trajectory dict via dspy's *stock* formatter (the byte-equality
    reference). Must be called inside a ``dspy.context`` with an adapter set."""
    react_cls = app._retaining_react_cls()
    return super(react_cls, agent)._format_trajectory(keys)


def expected_trajectory_dict(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconstruct the dict stock dspy would build for a fully-populated loop —
    the byte-equality reference for an UNEDITED trajectory.

    ``steps`` is a list of ``{thought, tool_name, tool_args, observation}``.
    """
    out: dict[str, Any] = {}
    for i, s in enumerate(steps):
        out[f"thought_{i}"] = s["thought"]
        out[f"tool_name_{i}"] = s["tool_name"]
        out[f"tool_args_{i}"] = s["tool_args"]
        out[f"observation_{i}"] = s["observation"]
    return out
