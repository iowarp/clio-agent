"""Retained attention hydration must not block delivery on the ASGI loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from clio_agent.gact.routes import blueprint_catalog, catalog, interactions


@pytest.mark.asyncio
async def test_interaction_hydration_does_not_block_other_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hold the synchronous projection while a request on the same loop finishes."""
    app = FastAPI()
    app.state.sessions = SimpleNamespace(get=lambda sid: SimpleNamespace(id=sid))
    entered = threading.Event()
    release = threading.Event()

    def project(*args: Any, **kwargs: Any) -> interactions.InteractionProjection:
        entered.set()
        assert release.wait(5), "test did not release retained-ledger projection"
        return interactions.InteractionProjection(rows=[], degradations=[])

    monkeypatch.setattr(interactions, "project_pending_interactions", project)
    interactions.register_interaction_routes(app, None)  # type: ignore[arg-type]

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"responsive": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(client.get("/v1/sessions/s/interactions"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            response = await asyncio.wait_for(client.get("/probe"), 1)
            assert response.json() == {"responsive": True}
            assert not pending.done()
        finally:
            release.set()
            completed = await pending
        assert completed.status_code == 200
        assert completed.json()["interactions"] == []


@pytest.mark.asyncio
async def test_catalog_discovery_does_not_block_other_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold on-disk catalog must not pause unrelated stream delivery."""
    app = FastAPI()
    entered = threading.Event()
    release = threading.Event()

    def discover(*args: Any, **kwargs: Any) -> list[Any]:
        entered.set()
        assert release.wait(5), "test did not release catalog discovery"
        return [SimpleNamespace(to_wire=lambda: {"id": "test-blueprint"})]

    monkeypatch.setattr(blueprint_catalog, "discover_agent_blueprints", discover)
    monkeypatch.setattr(blueprint_catalog, "_runtime_workspace_catalog_cwd", lambda *a, **k: None)
    blueprint_catalog.register_blueprint_catalog_route(app)

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"responsive": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(client.get("/v1/agent-blueprints"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            response = await asyncio.wait_for(client.get("/probe"), 1)
            assert response.json() == {"responsive": True}
            assert not pending.done()
        finally:
            release.set()
            completed = await pending
        assert completed.status_code == 200
        assert completed.json()["agent_blueprints"] == [{"id": "test-blueprint"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("planner", [False, True])
async def test_command_discovery_does_not_block_other_requests(
    monkeypatch: pytest.MonkeyPatch, planner: bool
) -> None:
    """Neither composer nor planner skill discovery may block stream delivery."""
    app = FastAPI()
    entered = threading.Event()
    release = threading.Event()

    def discover(*args: Any, **kwargs: Any) -> list[dict[str, str]]:
        entered.set()
        assert release.wait(5), "test did not release command discovery"
        return [{"id": "test-command"}]

    monkeypatch.setattr(catalog, "all_command_rows", discover)
    monkeypatch.setattr(catalog, "planner_command_rows", discover)
    monkeypatch.setattr(catalog, "command_context_for_request", lambda *a, **k: (None, []))
    catalog.register_catalog_routes(app, SimpleNamespace(resolve_runtime_dynamic_agent=None))

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"responsive": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(client.get("/v1/commands", params={"planner": planner}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            response = await asyncio.wait_for(client.get("/probe"), 1)
            assert response.json() == {"responsive": True}
            assert not pending.done()
        finally:
            release.set()
            completed = await pending
        assert completed.status_code == 200
        assert completed.json()["commands"] == [{"id": "test-command"}]
