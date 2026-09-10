"""Cold blueprint/tool preparation must not freeze the GACT event loop.

Also (#1334) the LAST thing the turn runs on the loop before the stream opens: resolving
whether the built agent's ``forward`` takes native inputs. The live goal-judge legs
attributed a 0.6-2.5 s whole-run loop stall to that ONE predicate -- ``dspy.Module``
intercepts the ``forward`` attribute name and runs ``inspect.stack()``, which reads and
``realpath``s every frame's source file, and uvicorn's loop stack is deep. Two locks
below: the predicate no longer walks the interpreter stack, and its resolve runs off the
loop regardless.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent.gact.app import build_app
from clio_agent.gact.messaging import _agent_accepts_images
from clio_agent.gact.turn_forward import _run_turn_setup_off_loop


def test_blocking_turn_setup_runs_off_the_event_loop() -> None:
    started = threading.Event()
    release = threading.Event()
    state = SimpleNamespace(
        sid="sess_parent",
        app=SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(get=lambda _sid: None))),
    )

    def blocking_setup() -> str:
        started.set()
        assert release.wait(timeout=2.0)
        return "ready"

    async def exercise() -> None:
        task = asyncio.create_task(_run_turn_setup_off_loop(state, blocking_setup))
        while not started.is_set():
            await asyncio.sleep(0)

        # If setup ran inline, this coroutine could not resume to make either
        # assertion until the blocking operation had already returned.
        assert not task.done()
        await asyncio.sleep(0)
        assert not task.done()

        release.set()
        assert await task == "ready"

    asyncio.run(exercise())


class _NativeInputAgent(dspy.Module):
    """A real ``dspy.Module`` -- the shape whose ``forward`` getattr walked the stack."""

    def forward(  # noqa: D102 - test double
        self,
        question: str,
        session_id: str,
        images: list[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        del question, session_id, images, kwargs
        return dspy.Prediction(answer="ok")


def test_the_native_input_predicate_never_walks_the_interpreter_stack() -> None:
    """#1334 root: reading ``forward`` off a dspy.Module must not call ``inspect.stack``.

    ``dspy.Module.__getattribute__`` runs ``inspect.stack()`` for exactly this attribute
    name (its "don't call forward directly" warning). That is an O(frames) walk with a
    source-file read + ``realpath`` per frame -- 0.6-2.5 s under uvicorn's loop stack on
    the live legs. Sabotage anchor: restore the plain ``getattr(agent, "forward", None)``
    in ``_agent_accepts_images`` and this goes red.
    """

    calls: list[int] = []
    real_stack = inspect.stack

    def _counting_stack(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real_stack(*args, **kwargs)

    agent = _NativeInputAgent()
    original = inspect.stack
    inspect.stack = _counting_stack  # type: ignore[assignment]
    try:
        assert _agent_accepts_images(agent) is True
    finally:
        inspect.stack = original  # type: ignore[assignment]
    assert calls == [], "resolving agent.forward walked the interpreter stack"


def test_a_non_dspy_agent_that_only_exposes_forward_dynamically_still_resolves() -> None:
    """The static lookup must not change the ANSWER for a ``__getattr__``-only agent."""

    class _Dynamic:
        def __getattr__(self, name: str) -> Any:
            if name != "forward":
                raise AttributeError(name)

            def forward(question: str, session_id: str, **kwargs: Any) -> None:
                del question, session_id, kwargs

            return forward

    assert _agent_accepts_images(_Dynamic()) is True

    class _NoForward:
        pass

    assert _agent_accepts_images(_NoForward()) is False


async def test_the_streamed_forwards_native_input_resolve_runs_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1334: ``_try_streamed_forward`` resolves the native-input kwargs on the executor.

    Defense in depth behind the root fix above: whatever the built module's introspection
    costs, it is not paid on the thread that serves REST/SSE. The spy records whether a
    loop was running on the thread that ran the resolve.
    """

    import importlib

    from clio_agent.gact import streaming as streaming_module

    streamify_module = importlib.import_module("dspy.streaming.streamify")

    def _passthrough(program: Any, **_options: Any) -> Any:
        async def _run(**kwargs: Any) -> Any:
            yield program.forward(**kwargs)

        return _run

    monkeypatch.setattr(streamify_module, "streamify", _passthrough)

    ran_on: list[tuple[str, bool]] = []
    real_resolve = streaming_module.native_input_kwargs

    def _spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        ran_on.append((threading.current_thread().name, on_loop))
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(streaming_module, "native_input_kwargs", _spy)

    agent = _NativeInputAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)

    async def emit_chunk(text: str) -> None:
        del text

    loop_thread = threading.current_thread().name
    await streaming_module._try_streamed_forward(app, "hello", "sid", emit_chunk)

    assert ran_on, "the native-input resolve never ran"
    assert all(not on_loop for _name, on_loop in ran_on), ran_on
    assert all(name != loop_thread for name, _on_loop in ran_on), ran_on
