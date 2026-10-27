"""The private SearXNG that CLIO's boot started stops with CLIO; one the person started stays."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clio_agent.gact import search_bootstrap
from clio_agent.gact.infrastructure.models import InfrastructureOperation


def _op(action: str, initiator: str, at: str, state: str = "succeeded") -> InfrastructureOperation:
    return InfrastructureOperation(
        service_id="searxng",
        target_id="local",
        action=action,
        initiator=initiator,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        created_at=at,
    )


def _runtime(*ops: InfrastructureOperation) -> SimpleNamespace:
    return SimpleNamespace(store=SimpleNamespace(operations=lambda: list(ops)))


def test_clio_actions_never_stand_for_the_persons_decision() -> None:
    runtime = _runtime(_op("start", "clio", "1"), _op("stop", "clio", "2"))
    assert search_bootstrap._last_action(runtime) == ""
    runtime = _runtime(_op("start", "clio", "1"), _op("stop", "person", "2"))
    assert search_bootstrap._last_action(runtime) == "stop"


def test_only_a_start_by_clio_is_stopped_even_while_the_persons_start_settles() -> None:
    assert search_bootstrap._started_by_clio(_runtime(_op("start", "clio", "1")))
    settling = _runtime(_op("start", "clio", "1"), _op("start", "person", "2", state="running"))
    assert not search_bootstrap._started_by_clio(settling)
    assert not search_bootstrap._started_by_clio(_runtime())


@pytest.mark.asyncio
async def test_shutdown_leaves_a_person_started_searxng_running() -> None:
    runtime = _runtime(_op("start", "person", "1"))
    store = SimpleNamespace(service=lambda *_: SimpleNamespace(state="running"))
    app = SimpleNamespace(
        state=SimpleNamespace(infrastructure_runtime=runtime, infrastructure_store=store)
    )
    assert await search_bootstrap.stop_started(app) == "not_started_by_clio"


@pytest.mark.asyncio
async def test_shutdown_stops_the_searxng_clio_started(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(_op("start", "clio", "1"))
    store = SimpleNamespace(service=lambda *_: SimpleNamespace(state="running"))
    app = SimpleNamespace(
        state=SimpleNamespace(infrastructure_runtime=runtime, infrastructure_store=store)
    )
    stopped: list[str] = []

    async def run(_runtime: object, action: str, _poll: float) -> str:
        stopped.append(action)
        return "succeeded"

    monkeypatch.setattr(search_bootstrap, "_run", run)
    assert await search_bootstrap.stop_started(app) == "stopped"
    assert stopped == ["stop"]
