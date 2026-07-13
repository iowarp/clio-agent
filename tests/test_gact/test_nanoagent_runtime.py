"""iowarp/clio-agent#9: spawn_one + spawn_many real runtime."""

from __future__ import annotations

import contextvars
import threading

from clio_agent.runtime.nanoagent import spawn_many, spawn_one

# Module-level probe: mimics active_app()/workspace-root ContextVars that the
# nanoagent rail (#735/#813) must carry from the spawning thread into
# dspy.Parallel workers. dspy's ParallelExecutor copies only
# thread_local_overrides, not contextvars.Context, so without the rail fix a
# worker thread reads the default, not the spawning thread's value.
_PROBE_VAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nanoagent_probe", default="DEFAULT"
)


class _StubAgent:
    """Stand-in for a DSPy Module without DSPy machinery."""

    calls: list[str] = []

    def __init__(self, label: str = "ok"):
        self.label = label

    def __call__(self, *, question: str):
        type(self).calls.append(question)
        return type("Pred", (), {"answer": f"{self.label}: {question}"})()


def test_spawn_one_returns_named_result() -> None:
    _StubAgent.calls = []
    out = spawn_one(
        _StubAgent,
        agent_id="validator",
        input={"file": "/tmp/x.h5"},
    )
    assert out.agent_id == "validator"
    assert out.input == {"file": "/tmp/x.h5"}
    assert "ok:" in out.answer
    assert out.duration_ms >= 0
    assert out.error == ""
    # Wire shape includes the right keys.
    wire = out.to_wire()
    assert wire["agent_id"] == "validator"
    assert "answer" in wire


def test_spawn_one_captures_exceptions() -> None:
    class _BoomAgent:
        def __call__(self, *, question: str):
            raise RuntimeError("kaboom")

    out = spawn_one(_BoomAgent, agent_id="x", input={"k": 1})
    assert out.error
    assert "kaboom" in out.error


def test_spawn_many_runs_each_item() -> None:
    _StubAgent.calls = []
    items = [
        {"agent_id": "v1", "input": {"file": "a.h5"}},
        {"agent_id": "v2", "input": {"file": "b.h5"}},
        {"agent_id": "v3", "input": {"file": "c.h5"}},
    ]
    results = spawn_many(_StubAgent, items=items)
    assert len(results) == 3
    assert {r.agent_id for r in results} == {"v1", "v2", "v3"}
    # Each result picked up the right input.
    by_agent = {r.agent_id: r for r in results}
    assert "a.h5" in by_agent["v1"].answer
    assert "b.h5" in by_agent["v2"].answer
    # The stub recorded every call.
    assert len(_StubAgent.calls) == 3


def test_spawn_many_empty_returns_empty() -> None:
    assert spawn_many(_StubAgent, items=[]) == []


def test_render_input_uses_question_field_when_present() -> None:
    from clio_agent.runtime.nanoagent import _render_input

    assert _render_input({"question": "hello"}) == "hello"
    assert _render_input({"file": "/tmp/x", "mode": "read"}).startswith("file=")


class _ContextReadingAgent:
    """Reads the spawning thread's ContextVar inside the worker thread."""

    def __call__(self, *, question: str):
        return type("Pred", (), {"answer": _PROBE_VAR.get()})()


def test_spawn_many_workers_inherit_spawning_context() -> None:
    """dspy.Parallel workers must observe the spawning thread's contextvars.

    Fails before the rail fix (worker thread starts with a fresh context, so
    _PROBE_VAR.get() returns the "DEFAULT"), passes after.
    """

    token = _PROBE_VAR.set("SPAWNER")
    try:
        results = spawn_many(
            _ContextReadingAgent,
            items=[{"agent_id": "a", "input": {"q": "x"}}],
        )
    finally:
        _PROBE_VAR.reset(token)
    assert len(results) == 1
    assert results[0].error == ""
    assert results[0].answer == "SPAWNER"


def test_spawn_many_concurrent_workers_no_context_reentry() -> None:
    """Two concurrent workers must not raise the "already entered" RuntimeError.

    A barrier forces both workers to sit inside their context.run() at the same
    time. A single shared Context would raise ``RuntimeError: cannot enter
    context: already entered`` on the second concurrent entry; per-pair context
    copies (plus disabled straggler resubmit) keep each execution isolated.
    """

    barrier = threading.Barrier(2, timeout=10)

    class _SlowAgent:
        def __call__(self, *, question: str):
            # Guarantee temporal overlap of both workers inside their contexts.
            barrier.wait()
            return type("Pred", (), {"answer": _PROBE_VAR.get()})()

    token = _PROBE_VAR.set("SPAWNER")
    try:
        results = spawn_many(
            _SlowAgent,
            items=[
                {"agent_id": "a", "input": {"q": "1"}},
                {"agent_id": "b", "input": {"q": "2"}},
            ],
            num_threads=2,
        )
    finally:
        _PROBE_VAR.reset(token)
    assert len(results) == 2
    assert all(r.error == "" for r in results)
    assert all(r.answer == "SPAWNER" for r in results)
