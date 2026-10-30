"""Real asynchronous observers retain Core provenance without holding a model loop."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact import context, tool_observer
from clio_agent.gact.agents.tool_instrumentation import (
    _DECLARED_STRUCTURED_CONTENT,
    declare_structured_content,
    instrument_tools,
    native_tool,
    pop_declared_structured_content,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.model_identity import take_call_artifacts
from clio_agent.gact.artifacts.observer_bridge import observer_call_id
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.transforms import ARTIFACT_TRANSFORM_RECORDED_EVENT
from clio_agent.tools import execution
from tests.test_gact.task_records import active_turn_fixture, record_assignment_fixture

pytestmark = pytest.mark.usefixtures("clio_core_plane", "host_agent_executor")


@pytest.fixture
def observation_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FastAPI, str, str]]:
    """Use the real application, observer and Core writer, with no model execution."""
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        workspace = client.post(
            "/v1/workspaces", json={"name": "observations", "root_path": str(tmp_path)}
        )
        # One workspace per folder (#1771): reuse the workspace already pinning it.
        assert workspace.status_code in (201, 409), workspace.text
        body = workspace.json()
        wid = (
            body["error"]["details"]["workspace_id"] if workspace.status_code == 409 else body["id"]
        )
        response = client.post("/v1/sessions", json={"workspace_id": wid})
        assert response.status_code == 200
        sid = response.json()["id"]
        record_assignment_fixture(app, sid)
        hooks = execution.ToolRuntimeHooks(tool_observer=tool_observer._make_tool_observer(app))
        monkeypatch.setattr(execution, "current_tool_runtime", lambda: hooks)
        assert getattr(app.state, "artifact_registry", None) is None
        yield app, wid, sid


async def _entered(event: threading.Event) -> None:
    """Bound only the test's expectation that recording reaches the held point."""
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("output", [False, True])
def test_cold_async_observer_is_durable_responsive_and_returns_call_context(
    observation_world: tuple[FastAPI, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bool,
) -> None:
    app, wid, sid = observation_world
    entered, release = threading.Event(), threading.Event()
    real_record = app.state.arc.record_semantic_event
    owner_thread = threading.get_ident()

    def record(event: Any) -> Any:
        assert threading.get_ident() != owner_thread
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        if event.event_type == ARTIFACT_TRANSFORM_RECORDED_EVENT:
            entered.set()
            assert release.wait(5), "The event loop never released the held Core writer"
        return real_record(event)

    monkeypatch.setattr(app.state.arc, "record_semantic_event", record)
    destination = tmp_path / "observed.txt"

    async def probe(output_path: str = "") -> dict[str, Any]:
        """Create an optional real output and declare its structured observation."""
        call_id = observer_call_id()
        assert call_id
        if output_path:
            await asyncio.to_thread(Path(output_path).write_text, "retained", encoding="utf-8")
        declare_structured_content({"call_id": call_id, "structured": True})
        return {"call_id": call_id, "output": bool(output_path)}

    (tool,) = instrument_tools(
        [
            native_tool(
                probe,
                name="async_observed",
                args={"output_path": {"type": "string"}},
                desc="Inspect real output",
                presentation="text",
                domain="resources",
            )
        ]
    )

    async def invoke() -> tuple[dict[str, Any], list[dict[str, str]]]:
        declare_structured_content({"stale": True})
        result = await tool.acall(output_path=str(destination) if output else "")
        assert pop_declared_structured_content() is None
        call_id = tool_observer._OBSERVER_CALL_IDS.get()
        assert call_id == result["call_id"]
        assert tool_observer._OBSERVER_CALL_T0.get() is None
        artifacts = take_call_artifacts(call_id)
        assert take_call_artifacts(call_id) == []
        return result, artifacts

    async def exercise() -> tuple[dict[str, Any], list[dict[str, str]]]:
        pending = asyncio.create_task(invoke())
        try:
            await _entered(entered)
            for _ in range(10):
                await asyncio.sleep(0)
            assert not pending.done(), "The held durable write was acknowledged prematurely"
        finally:
            release.set()
        return await pending

    with active_turn_fixture(app, sid):
        scope = context.set_react_scope("observed-agent")
        try:
            result, artifacts = asyncio.run_coroutine_threadsafe(
                exercise(), app.state.mcp_app_loop
            ).result(timeout=10)
        finally:
            context.reset(scope)
    call_id = result["call_id"]
    transform = get_registry(app).get_transform(call_id)
    assert transform is not None and transform.workspace_id == wid
    assert transform.agent_id == "observed-agent"
    assert result["output"] is output
    if output:
        assert destination.read_text(encoding="utf-8") == "retained"
        assert any(item["path"] == str(destination) for item in artifacts)
    else:
        assert artifacts == []
    segments = list(app.state.arc._live.iter_session_event_segments(sid))
    transforms = [
        item
        for item in segments
        if item.content.get("event_type") == ARTIFACT_TRANSFORM_RECORDED_EVENT
    ]
    assert len(transforms) == 1
    assert transforms[0].content["subject"]["call_id"] == call_id
    assert transforms[0].turn_id == "fixture-turn"
    assert transforms[0].content["trace_id"] == "fixture-trace"
    completions = [
        item for item in app.state.live_assistant_parts[sid] if item.type == "tool_result"
    ]
    assert len(completions) == 1 and completions[0].call_id == call_id
    assert completions[0].structured_content == {"call_id": call_id, "structured": True}
    assert not getattr(app.state, "artifact_transform_failures", [])


@pytest.mark.parametrize("phase", ["started", "completed"])
def test_cancelled_async_observation_retires_its_core_worker_before_returning(
    observation_world: tuple[FastAPI, str, str],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    app, _wid, sid = observation_world
    entered, release, retired = threading.Event(), threading.Event(), threading.Event()
    real_record = app.state.arc.record_semantic_event
    body_ran = False

    def record(event: Any) -> Any:
        if event.event_type == "tool.call." + phase:
            entered.set()
            try:
                assert release.wait(5), "Recording was never released by the test"
                return real_record(event)
            finally:
                retired.set()
        return real_record(event)

    monkeypatch.setattr(app.state.arc, "record_semantic_event", record)

    async def probe() -> str:
        """Return only when the tool body was actually admitted."""
        nonlocal body_ran
        body_ran = True
        declare_structured_content({"body_ran": True})
        return "done"

    (tool,) = instrument_tools(
        [
            native_tool(
                probe,
                name="cancelled_observation",
                args={},
                desc="Observe cancellation",
                presentation="text",
                domain="resources",
            )
        ]
    )

    async def invoke() -> None:
        try:
            await tool.acall()
        finally:
            assert pop_declared_structured_content() is None

    async def exercise() -> None:
        parent_content = {"parent_only": True}
        parent_token = _DECLARED_STRUCTURED_CONTENT.set(parent_content)
        try:
            pending = asyncio.create_task(invoke())
            try:
                await _entered(entered)
                assert pending.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                assert not pending.done(), "A cancelled waiter concealed a live recording worker"
                assert not retired.is_set()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert retired.is_set()
            assert pop_declared_structured_content() == parent_content
        finally:
            _DECLARED_STRUCTURED_CONTENT.reset(parent_token)

    with active_turn_fixture(app, sid):
        asyncio.run_coroutine_threadsafe(exercise(), app.state.mcp_app_loop).result(timeout=10)
    assert body_ran is (phase == "completed")
    parts = app.state.live_assistant_parts[sid]
    starts = [item for item in parts if item.type == "tool_call"]
    ends = [item for item in parts if item.type == "tool_result"]
    assert len(starts) == len(ends) == 1
    assert starts[0].call_id == ends[0].call_id
    assert ends[0].is_error is (phase == "started")
    assert ends[0].structured_content == ({"body_ran": True} if phase == "completed" else None)


def test_interleaved_async_calls_keep_distinct_started_and_completed_identities(
    observation_world: tuple[FastAPI, str, str],
) -> None:
    app, _wid, sid = observation_world
    first_started, second_started, finish_second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    identities: dict[str, str] = {}

    async def probe(label: str) -> str:
        """Force A.started, B.started, A.completed, B.completed on the same loop."""
        identities[label] = observer_call_id()
        assert identities[label]
        if label == "A":
            first_started.set()
            await second_started.wait()
        else:
            second_started.set()
            await finish_second.wait()
        declare_structured_content({"label": label})
        return label

    (tool,) = instrument_tools(
        [
            native_tool(
                probe,
                name="interleaved_observation",
                args={"label": {"type": "string"}},
                desc="Interleave observations",
                presentation="text",
                domain="resources",
            )
        ]
    )

    async def exercise() -> None:
        first = asyncio.create_task(tool.acall(label="A"))
        await first_started.wait()
        second = asyncio.create_task(tool.acall(label="B"))
        await second_started.wait()
        try:
            assert await first == "A"
            assert not second.done()
        finally:
            finish_second.set()
        assert await second == "B"

    with active_turn_fixture(app, sid):
        asyncio.run_coroutine_threadsafe(exercise(), app.state.mcp_app_loop).result(timeout=10)
    assert identities["A"] != identities["B"]
    registry = get_registry(app)
    parts = app.state.live_assistant_parts[sid]
    for label, call_id in identities.items():
        assert registry.get_transform(call_id) is not None
        starts = [item for item in parts if item.type == "tool_call" and item.call_id == call_id]
        ends = [item for item in parts if item.type == "tool_result" and item.call_id == call_id]
        assert len(starts) == len(ends) == 1
        assert starts[0].input["label"] == label
        assert ends[0].structured_content == {"label": label}
        assert ends[0].duration_ms >= 0
    segments = list(app.state.arc._live.iter_session_event_segments(sid))
    retained = [
        item.content["subject"]["call_id"]
        for item in segments
        if item.content.get("event_type") == ARTIFACT_TRANSFORM_RECORDED_EVENT
    ]
    assert sorted(retained) == sorted(identities.values())
    assert not getattr(app.state, "artifact_transform_failures", [])
