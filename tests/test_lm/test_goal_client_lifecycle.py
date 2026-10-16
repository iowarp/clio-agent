"""Real HTTP sockets across sequential goal work/check/next-work cycles.

The endpoint is local test data. Count active requests and idle sockets separately;
an idle socket is not evidence of an active inference request at a remote gateway.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from dspy.lm15 import Message, Request

from clio_agent.config import LMProviderConfig
from clio_agent.gact import goal
from clio_agent.lm.engines.lm_loop import run_on_lm_loop
from clio_agent.lm.factory import create_lm
from clio_agent.lm.resources import model_resources
from tests.test_gact.test_goal import _app, _session


def _client(base: str) -> dspy.LM:
    return create_lm(
        LMProviderConfig(
            provider="openai",
            provider_id="amsc_mag",
            model="test-goal",
            api_base=base,
            api_key="local-test-only",
        )
    )


async def _closed(state: dict[str, Any]) -> None:
    deadline = time.monotonic() + 2
    while state["open"] and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert state["open"] == 0
    assert state["active"] == 0


def test_repeated_goal_checks_close_owned_clients_without_waiting_for_gc(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, state = endpoint
    retained: list[dspy.LM] = []

    def route(provider_id: str, model_id: str) -> tuple[dspy.LM, Any]:
        lm = _client(base)
        retained.append(lm)  # prove deterministic cleanup, independent of GC timing
        return lm, dspy.ChatAdapter()

    monkeypatch.setattr("clio_agent.gact.fleet_settings.model_route", route)
    app = _app(tmp_path)
    sid = _session(app)
    for _ in range(6):
        _session(app)  # six idle sessions issue no requests

    async def run() -> None:
        try:
            for _ in range(21):
                verdict = await goal.run_llm_judge(
                    app,
                    sid,
                    {
                        "condition": "test",
                        "_caller_model": {"provider_id": "amsc_mag", "model_id": "test-goal"},
                    },
                )
                assert verdict.available and not verdict.met, verdict
            assert state["max_active"] == 1
            await _closed(state)
            assert all(not lm._engine_store for lm in retained)
        finally:
            for lm in retained:
                await lm.aclose()

    asyncio.run(run())
    assert state["calls"] == 21
    assert state["max_open"] <= 2


def test_inherited_client_is_borrowed_and_idle_socket_is_not_an_active_request(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
) -> None:
    base, state = endpoint
    lm = _client(base)
    app = _app(tmp_path)
    app.state.agent = SimpleNamespace(_main_lm=lm, _dspy_adapter=dspy.ChatAdapter())
    sid = _session(app)

    async def run() -> None:
        try:
            for _ in range(3):
                verdict = await goal.run_llm_judge(app, sid, {"condition": "test"})
                assert verdict.available
            assert state["open"] == 1
            assert state["active"] == 0
            assert lm._engine_store  # borrowed pool remains reusable
        finally:
            await lm.aclose()
        await _closed(state)

    asyncio.run(run())
    assert state["calls"] == 3 and state["max_open"] == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_pending_goal_timeout_or_cancel_closes_owned_resources(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    base, state = endpoint
    state["hold"] = True
    retained: list[dspy.LM] = []

    def route(provider_id: str, model_id: str) -> tuple[dspy.LM, Any]:
        lm = _client(base)
        retained.append(lm)
        return lm, dspy.ChatAdapter()

    monkeypatch.setattr("clio_agent.gact.fleet_settings.model_route", route)
    monkeypatch.setattr(goal, "DEFAULT_JUDGE_TIMEOUT_S", 5 if cancel else 0.1)
    app = _app(tmp_path)
    sid = _session(app)

    async def run() -> None:
        task = asyncio.create_task(
            goal.run_llm_judge(
                app,
                sid,
                {
                    "condition": "test",
                    "_caller_model": {"provider_id": "amsc_mag", "model_id": "test-goal"},
                },
            )
        )
        assert await asyncio.to_thread(state["started"].wait, 5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            verdict = await task
            assert not verdict.available
        await _closed(state)
        assert retained and all(not lm._engine_store for lm in retained)

    asyncio.run(run())
    assert state["calls"] == 1


def test_work_check_next_work_uses_one_active_request_and_releases_each_phase(
    endpoint: tuple[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, state = endpoint
    retained: list[dspy.LM] = []

    def route(provider_id: str, model_id: str) -> tuple[dspy.LM, Any]:
        lm = _client(base)
        retained.append(lm)
        return lm, dspy.ChatAdapter()

    def work() -> None:
        with model_resources():
            lm = _client(base)
            retained.append(lm)
            request = Request(model=lm.model, messages=(Message.user("work"),))
            run_on_lm_loop(lambda: lm.acall(request))
            run_on_lm_loop(lambda: lm.acall(request))  # reuse within an iteration

    monkeypatch.setattr("clio_agent.gact.fleet_settings.model_route", route)
    app = _app(tmp_path)
    sid = _session(app)

    async def run() -> None:
        for _ in range(21):
            await asyncio.to_thread(work)
            await _closed(state)
            verdict = await goal.run_llm_judge(
                app,
                sid,
                {
                    "condition": "test",
                    "_caller_model": {"provider_id": "amsc_mag", "model_id": "test-goal"},
                },
            )
            assert verdict.available
            await _closed(state)
        assert all(not lm._engine_store for lm in retained)

    asyncio.run(run())
    assert state["calls"] == 63 and state["max_active"] == 1
    assert state["max_open"] == 1
