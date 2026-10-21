"""Real connection reuse, credential replacement and session-owner retirement."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from dspy.lm15 import Message, Request

from clio_agent.arc.memory import ARCMemory
from clio_agent.config import LMProviderConfig
from clio_agent.gact import context, goal
from clio_agent.lm.engines.lm_loop import run_on_lm_loop
from clio_agent.lm.factory import create_lm
from clio_agent.lm.session_clients import close_session_model_clients, session_model
from clio_agent.runtime import turn_lm_ledger
from tests.test_arc.conftest import core_arc as core_arc
from tests.test_gact.test_goal import _app, _session
from tests.test_lm.test_goal_client_lifecycle import _closed


@contextmanager
def bound(app: Any, sid: str) -> Iterator[None]:
    """Bind real session identity without retaining a test's context afterward."""
    app_token = context.set_app(app)
    sid_token = context.set_session_id(sid)
    try:
        yield
    finally:
        context.reset(sid_token)
        context.reset(app_token)


def config(base: str) -> LMProviderConfig:
    """Build the local OpenAI-compatible test configuration."""
    return LMProviderConfig(
        provider="openai",
        provider_id="amsc_mag",
        model="test-goal",
        api_base=base,
        api_key="test-token-1",
    )


def call(lm: Any) -> None:
    """Send a real request through DSPy's persistent async pool."""
    request = Request(model=lm.model, messages=(Message.user("work"),))
    run_on_lm_loop(lambda: lm.acall(request))


def test_21_goal_cycles_reuse_work_pool_while_owned_checkers_close(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arc: ARCMemory,
) -> None:
    base, state = endpoint
    app = _app(tmp_path, arc)
    sid = _session(app)
    for _ in range(6):
        _session(app)
    cfg = config(base)
    constructed: list[Any] = []
    views: list[Any] = []
    checkers: list[Any] = []

    def factory(cfg: LMProviderConfig) -> Any:
        lm = create_lm(cfg)
        constructed.append(lm)
        return lm

    def checker(provider_id: str, model_id: str) -> tuple[Any, Any]:
        lm = create_lm(cfg)
        checkers.append(lm)
        return lm, dspy.ChatAdapter()

    monkeypatch.setattr("clio_agent.config.create_lm", factory)
    monkeypatch.setattr("clio_agent.gact.fleet_settings.model_route", checker)

    def work() -> None:
        from clio_agent.gact.usage import _usage_from_history_slice

        token = turn_lm_ledger.open_ledger()
        try:
            with bound(app, sid), session_model(cfg, "registered-agent", sid) as lm:
                assert lm.history == []
                assert lm in turn_lm_ledger.ledger_lms()
                views.append(lm)
                call(lm)
                call(lm)
                assert len(lm.history) == 2
                with dspy.context(lm=lm):
                    usage = _usage_from_history_slice({}, app)
                assert usage["input"] == usage["output"] == 2
        finally:
            turn_lm_ledger.close_ledger(token)

    async def run() -> None:
        try:
            for _ in range(21):
                await asyncio.to_thread(work)
                verdict = await goal.run_llm_judge(
                    app,
                    sid,
                    {
                        "condition": "test",
                        "_caller_model": {"provider_id": "amsc_mag", "model_id": "test-goal"},
                    },
                )
                assert verdict.available
                deadline = time.monotonic() + 2
                while state["open"] > 1 and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert state["open"] == 1  # the checker socket closed; only work stays idle
            assert len(constructed) == 1
            assert all(view._engine_store is constructed[0]._engine_store for view in views)
            assert len(constructed[0]._engine_store) == 1
            assert all(len(view.history) == 2 for view in views)
            assert all(not checker._engine_store for checker in checkers)
            assert state["active"] == 0 and state["max_active"] == 1
        finally:
            await asyncio.to_thread(close_session_model_clients, app)
        await _closed(state)

    asyncio.run(run())
    assert state["calls"] == 63
    assert state["connections"] == 22  # one work pool, 21 ephemeral checker pools
    assert state["max_open"] <= 3
    if output := os.environ.get("CLIO_GOAL_REUSE_EVIDENCE"):
        Path(output).write_text(
            json.dumps(
                {
                    "source": "real local HTTP endpoint; no external inference",
                    "goal_cycles": 21,
                    "idle_sessions": 6,
                    "work_client_constructions": len(constructed),
                    "ephemeral_checker_clients": len(checkers),
                    "requests": state["calls"],
                    "connections_created": state["connections"],
                    "max_active_requests": state["max_active"],
                    "open_sockets_after_owner_close": state["open"],
                    "active_requests_after_owner_close": state["active"],
                    "isolated_turn_history_lengths": [len(view.history) for view in views],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def test_fresh_credentials_and_model_settings_replace_and_close_old_pools(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    arc: ARCMemory,
) -> None:
    base, state = endpoint
    app = _app(tmp_path, arc)
    sid = _session(app)
    cfg = config(base)
    views = []
    try:
        with bound(app, sid):
            for key, model in [
                ("test-token-1", "test-goal"),
                ("test-token-2", "test-goal"),
                ("test-token-2", "next-model"),
            ]:
                fresh = copy.copy(cfg)
                fresh.api_key, fresh.model = key, model
                with session_model(fresh, "agent", sid) as lm:
                    call(lm)
                    views.append(lm)
                assert all(not previous._engine_store for previous in views[:-1])
    finally:
        close_session_model_clients(app)
    asyncio.run(_closed(state))
    assert state["requests"] == [
        ("test-goal", "Bearer test-token-1"),
        ("test-goal", "Bearer test-token-2"),
        ("next-model", "Bearer test-token-2"),
    ]
    assert all(not view._engine_store for view in views)


def test_configuration_change_does_not_close_another_active_lease(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    arc: ARCMemory,
) -> None:
    base, state = endpoint
    app = _app(tmp_path, arc)
    sid = _session(app)
    cfg = config(base)
    changed = copy.copy(cfg)
    changed.api_key = "test-token-2"
    try:
        with bound(app, sid), session_model(cfg, "agent", sid) as first:
            call(first)
            with session_model(changed, "agent", sid) as second:
                call(second)
            assert first._engine_store  # retired, but still leased
            call(first)
        assert not first._engine_store
        assert second._engine_store
    finally:
        close_session_model_clients(app)
    asyncio.run(_closed(state))
    assert [auth for _, auth in state["requests"]] == [
        "Bearer test-token-1",
        "Bearer test-token-2",
        "Bearer test-token-1",
    ]


@pytest.mark.parametrize("whole_app", [False, True])
def test_session_delete_and_shutdown_defer_active_clients_then_reject_new_leases(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    whole_app: bool,
    arc: ARCMemory,
) -> None:
    base, state = endpoint
    app = _app(tmp_path, arc)
    sid, neighbor = _session(app), _session(app)
    cfg = config(base)
    try:
        with bound(app, neighbor), session_model(cfg, "agent", neighbor) as other:
            call(other)
        with bound(app, sid), session_model(cfg, "agent", sid) as lm:
            call(lm)
            assert lm._engine_store is not other._engine_store
            close_session_model_clients(app, None if whole_app else sid)
            assert lm._engine_store
        assert not lm._engine_store
        assert bool(other._engine_store) is not whole_app
        with bound(app, sid), pytest.raises(RuntimeError, match="owner has closed"):
            with session_model(cfg, "agent", sid):
                pytest.fail("closed sessions must not reacquire a client")
    finally:
        close_session_model_clients(app)
    asyncio.run(_closed(state))


def test_failed_work_and_cancelled_request_leave_owner_reusable(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    arc: ARCMemory,
) -> None:
    base, state = endpoint
    app = _app(tmp_path, arc)
    sid = _session(app)
    cfg = config(base)
    try:
        with bound(app, sid), pytest.raises(ValueError, match="work failed"):
            with session_model(cfg, "agent", sid) as failed:
                call(failed)
                raise ValueError("work failed")
        state["hold"] = True
        state["started"].clear()
        with bound(app, sid), pytest.raises(TimeoutError):
            with session_model(cfg, "agent", sid) as cancelled:

                async def timeout() -> None:
                    async with asyncio.timeout(0.1):
                        await cancelled.acall(
                            Request(model=cancelled.model, messages=(Message.user("work"),))
                        )

                run_on_lm_loop(timeout)
        state["hold"] = False
        with bound(app, sid), session_model(cfg, "agent", sid) as following:
            assert following._engine_store is failed._engine_store is cancelled._engine_store
            call(following)
    finally:
        close_session_model_clients(app)
    asyncio.run(_closed(state))
    assert state["calls"] == 3


def test_non_session_clients_keep_operation_lifetime(
    endpoint: tuple[str, dict[str, Any]],
) -> None:
    base, state = endpoint
    token = context.set_app(None)
    try:
        with session_model(config(base), "agent", "") as lm:
            call(lm)
        assert not lm._engine_store
    finally:
        context.reset(token)
    asyncio.run(_closed(state))


@pytest.mark.parametrize("kind", ["react", "predict"])
def test_rebuilt_registered_agents_reuse_clients_and_refresh_credentials(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    arc: ARCMemory,
) -> None:
    from clio_agent.gact.agents import builders, skill_runtime
    from clio_agent.gact.agents.clio_react import ClioReAct
    from clio_agent.gact.agents.resolution import resolution_agent_def
    from clio_agent.plugins.service import PluginService
    from clio_agent.providers.credentials import CredentialResolver
    from clio_agent.providers.lm_spec import LMSpec
    from clio_agent.providers.resolver import ResolvedLMSpec

    base, state = endpoint
    app = _app(tmp_path, arc)
    sid = _session(app)
    cfg = config(base)
    skeleton = copy.copy(cfg)
    skeleton.api_key = ""
    spec = ResolvedLMSpec(
        LMSpec(
            provider="openai",
            provider_id="amsc_mag",
            model="test-goal",
            credential_ref="amsc_mag:local",
        ),
        skeleton,
        _default_ref=False,
    )
    token = "test-token-1"
    resolutions: list[tuple[str, str]] = []
    constructed: list[Any] = []
    views: list[Any] = []

    def resolve(self: Any, provider: str, credential_ref: str = "") -> str:
        resolutions.append((provider, credential_ref))
        return token

    def factory(config: LMProviderConfig) -> Any:
        lm = create_lm(config)
        constructed.append(lm)
        return lm

    def program(self: Any, **kwargs: Any) -> Any:
        # Isolate the agent's task semantics, but send a real model request
        # through the LM that the production registered-agent builder binds.
        lm = dspy.settings.lm
        views.append(lm)
        call(lm)
        return dspy.Prediction(answer="local response")

    monkeypatch.setattr(CredentialResolver, "resolve", resolve)
    monkeypatch.setattr("clio_agent.config.create_lm", factory)
    monkeypatch.setattr(builders, "_dynamic_agent_lm_config", lambda *args: spec)
    monkeypatch.setattr(builders, "_agent_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(builders, "build_spawn_runtime_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(builders, "_runtime_active_workspace_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        skill_runtime,
        "skill_runtime_for_agent",
        lambda *args, **kwargs: skill_runtime.SkillRuntime(),
    )
    monkeypatch.setattr(ClioReAct, "forward", program)
    # A resolved plugin agent (the hermetic default marketplace's standard agent).
    app.state.plugin_service = PluginService()
    resolution = app.state.plugin_service.resolve("clio/standard", pin=False)
    agent = resolution_agent_def(resolution).model_copy(update={"module": {"kind": kind}})
    try:
        with bound(app, sid):
            for index in range(3):
                if index == 2:
                    token = "test-token-2"
                module = builders.build_agent_module(SimpleNamespace(), agent, resolution)
                result = module(question="work", session_id=sid)
                assert result.answer == "local response"
        assert len(resolutions) == 6  # every fresh module build AND every forward
        assert set(resolutions) == {("amsc_mag", "amsc_mag:local")}
        assert len(constructed) == 2  # one reused, one replacement after rotation
        assert views[0]._engine_store is views[1]._engine_store
        assert not constructed[0]._engine_store
        assert constructed[1]._engine_store
        assert all(len(view.history) == 1 for view in views)
    finally:
        close_session_model_clients(app)
    asyncio.run(_closed(state))
    assert [auth for _, auth in state["requests"]] == [
        "Bearer test-token-1",
        "Bearer test-token-1",
        "Bearer test-token-2",
    ]


def test_delete_session_api_retires_its_real_pool_without_closing_its_neighbor(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from clio_agent.gact.app import build_app

    base, state = endpoint
    app = build_app(sessions_path=tmp_path / "api-sessions.json", arc=None)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "work"}).json()["id"]
    neighbor = client.post("/v1/sessions", json={"title": "other"}).json()["id"]
    cfg = config(base)
    try:
        with bound(app, neighbor), session_model(cfg, "agent", neighbor) as other:
            call(other)
        with bound(app, sid), session_model(cfg, "agent", sid) as active:
            call(active)
            response = client.delete(f"/v1/sessions/{sid}")
            assert response.status_code == 204, response.text
            assert active._engine_store  # final lease, not an abruptly closed request
        assert not active._engine_store
        assert other._engine_store
        assert client.get(f"/v1/sessions/{sid}").status_code == 404
        with bound(app, sid), pytest.raises(RuntimeError, match="no longer exists"):
            with session_model(cfg, "agent", sid):
                pytest.fail("deleted sessions must not dispatch")
    finally:
        close_session_model_clients(app)
    asyncio.run(_closed(state))


def test_final_shutdown_closes_work_pools_before_exit(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arc: ARCMemory,
) -> None:
    from clio_agent.gact import desktop_lifecycle

    base, state = endpoint
    app = _app(tmp_path, arc)
    sid = _session(app)
    with bound(app, sid), session_model(config(base), "agent", sid) as lm:
        call(lm)
    assert lm._engine_store
    events = []

    def children(agent: Any) -> None:
        assert not lm._engine_store
        events.append("children")

    monkeypatch.setattr("clio_agent.runtime.process_tree.shutdown_child_processes", children)
    monkeypatch.setattr(
        desktop_lifecycle, "terminate_process_after_cleanup", lambda app: events.append("exit")
    )

    async def run() -> None:
        app.state.mcp_app_loop = asyncio.get_running_loop()
        await desktop_lifecycle.finalize_desktop_shutdown(app, None)
        await _closed(state)

    asyncio.run(run())
    assert events == ["children", "exit"]


def test_racing_first_forwards_share_one_owner_and_close_unused_construction(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arc: ARCMemory,
) -> None:
    base, state = endpoint
    app = _app(tmp_path, arc)
    sid = _session(app)
    cfg = config(base)
    ready = threading.Barrier(2)
    created: list[Any] = []
    closed: list[Any] = []

    def factory(config: LMProviderConfig) -> Any:
        lm = create_lm(config)
        close = lm.aclose

        async def record_close() -> None:
            await close()
            closed.append(lm)

        monkeypatch.setattr(lm, "aclose", record_close)
        created.append(lm)
        ready.wait(5)
        return lm

    def work() -> Any:
        with bound(app, sid), session_model(cfg, "agent", sid) as lm:
            call(lm)
            return lm

    monkeypatch.setattr("clio_agent.config.create_lm", factory)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(work) for _ in range(2)]
            views = [job.result(timeout=10) for job in jobs]
        assert views[0]._engine_store is views[1]._engine_store
        assert len(created) == 2 and len(closed) == 1
        assert not closed[0]._engine_store
    finally:
        close_session_model_clients(app)
    asyncio.run(_closed(state))
    assert len(closed) == 2
    assert state["calls"] == 2


def test_cleanup_failure_does_not_replace_a_retired_lease_work_error(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arc: ARCMemory,
) -> None:
    base, state = endpoint
    app = _app(tmp_path, arc)
    sid = _session(app)
    primary = ValueError("work error")

    def factory(config: LMProviderConfig) -> Any:
        lm = create_lm(config)
        close = lm.aclose

        async def fail_after_close() -> None:
            await close()
            raise RuntimeError("cleanup error")

        monkeypatch.setattr(lm, "aclose", fail_after_close)
        return lm

    monkeypatch.setattr("clio_agent.config.create_lm", factory)
    with bound(app, sid), pytest.raises(ValueError) as error:
        with session_model(config(base), "agent", sid) as lm:
            call(lm)
            close_session_model_clients(app, sid)
            raise primary
    assert error.value is primary
    assert "cleanup also failed" in error.value.__notes__[0]
    assert not lm._engine_store
    asyncio.run(_closed(state))


@pytest.mark.parametrize("factory_started", [False, True])
def test_shutdown_prevents_late_client_construction_from_dispatching(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory_started: bool,
    arc: ARCMemory,
) -> None:
    base, state = endpoint
    app = _app(tmp_path, arc)
    sid = _session(app)
    entered, resume = threading.Event(), threading.Event()
    created: list[Any] = []
    closed: list[Any] = []

    def factory(config: LMProviderConfig) -> Any:
        entered.set()
        assert resume.wait(5)
        lm = create_lm(config)
        created.append(lm)
        close = lm.aclose

        async def record_close() -> None:
            await close()
            closed.append(lm)

        monkeypatch.setattr(lm, "aclose", record_close)
        return lm

    def work() -> None:
        with bound(app, sid), session_model(config(base), "agent", sid) as lm:
            call(lm)

    monkeypatch.setattr("clio_agent.config.create_lm", factory)
    with ThreadPoolExecutor(max_workers=1) as executor:
        if factory_started:
            job = executor.submit(work)
            assert entered.wait(5)
        close_session_model_clients(app)
        resume.set()
        if not factory_started:
            job = executor.submit(work)
        with pytest.raises(RuntimeError, match="owner has closed"):
            job.result(timeout=10)
    assert len(created) == len(closed) == int(factory_started)
    assert state["calls"] == 0
    asyncio.run(_closed(state))
