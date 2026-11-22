"""A view's slow call answers 202 with a location, so it never holds a browser connection.

Held view requests (mount, reopen, a view's tools/call) used every one of the browser's
six connections per origin, and the page's own reads queued behind them: the UI looked
hung while the agent answered (take 1t). With ``Prefer: respond-async`` the call keeps
running and the client polls its location instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact import view_call_cancel, view_pending

PREFER = {"Prefer": "respond-async, wait=0.1"}


def _app(gate: asyncio.Event, seen: list[Any]) -> FastAPI:
    app = FastAPI()
    view_pending.register_view_pending_routes(app)

    async def slow(cancel: Any) -> dict[str, Any]:
        seen.append(cancel)
        while not gate.is_set():
            if await cancel.is_disconnected():
                raise HTTPException(status_code=409, detail=view_call_cancel.VIEW_CALL_CANCELLED)
            await asyncio.sleep(0.01)
        return {"ok": True}

    async def fails(_cancel: Any) -> Any:
        await gate.wait()
        raise HTTPException(status_code=502, detail="server unreachable")

    @app.post("/v1/sessions/{sid}/slow")
    async def slow_route(sid: str, request: Request) -> Any:
        return await view_pending.respond(app, request, sid, "reopen", slow)

    @app.post("/v1/sessions/{sid}/fails")
    async def fails_route(sid: str, request: Request) -> Any:
        return await view_pending.respond(app, request, sid, "tools/call", fails)

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_the_preference_is_parsed_and_bounded() -> None:
    def wait(value: str) -> float | None:
        return view_pending.preferred_wait(httpx.Request("GET", "http://t", headers=value))

    assert view_pending.preferred_wait(httpx.Request("GET", "http://t")) is None
    assert wait({"Prefer": "return=minimal"}) is None
    assert wait({"Prefer": "respond-async"}) == view_pending.DEFAULT_WAIT_S
    assert wait({"Prefer": "respond-async, wait=3"}) == 3.0
    assert wait({"Prefer": "respond-async, wait=999"}) == view_pending.MAX_WAIT_S


@pytest.mark.asyncio
async def test_without_the_preference_the_request_is_held_as_before() -> None:
    gate, seen = asyncio.Event(), []
    app = _app(gate, seen)
    async with _client(app) as client:
        asyncio.get_running_loop().call_later(0.2, gate.set)
        answer = await client.post("/v1/sessions/s1/slow")
    assert answer.status_code == 200 and answer.json() == {"ok": True}
    assert not isinstance(seen[0], view_pending._Lease)  # bound to the request itself


@pytest.mark.asyncio
async def test_a_fast_call_answers_inline() -> None:
    gate, seen = asyncio.Event(), []
    gate.set()
    async with _client(_app(gate, seen)) as client:
        answer = await client.post("/v1/sessions/s1/slow", headers=PREFER)
    assert answer.status_code == 200 and answer.json() == {"ok": True}


@pytest.mark.asyncio
async def test_a_slow_call_answers_202_then_its_result_at_the_location() -> None:
    """SABOTAGE: await the operation inline in ``respond`` -> the POST holds -> red."""
    gate, seen = asyncio.Event(), []
    app = _app(gate, seen)
    async with _client(app) as client:
        answer = await asyncio.wait_for(client.post("/v1/sessions/s1/slow", headers=PREFER), 5)
        assert answer.status_code == 202
        pending = answer.json()[view_pending.PENDING_KEY]
        assert pending["route"] == "reopen" and answer.headers["Location"] == pending["location"]
        assert pending["location"] == f"/v1/sessions/s1/mcp-apps/pending/{pending['id']}"
        again = await client.get(pending["location"])
        assert again.status_code == 202
        gate.set()
        await asyncio.sleep(0.05)
        done = await client.get(pending["location"])
        assert done.status_code == 200 and done.json() == {"ok": True}
        gone = await client.get(pending["location"])
        assert gone.status_code == 404
        assert gone.json()["detail"]["error"] == view_pending.PENDING_UNKNOWN
        other = await client.get(f"/v1/sessions/s2/mcp-apps/pending/{pending['id']}")
        assert other.status_code == 404


@pytest.mark.asyncio
async def test_an_error_is_answered_at_the_location_as_the_original_would_have() -> None:
    gate, seen = asyncio.Event(), []
    async with _client(_app(gate, seen)) as client:
        answer = await client.post("/v1/sessions/s1/fails", headers=PREFER)
        assert answer.status_code == 202
        gate.set()
        await asyncio.sleep(0.05)
        failed = await client.get(answer.json()[view_pending.PENDING_KEY]["location"])
    assert failed.status_code == 502 and failed.json()["detail"] == "server unreachable"


@pytest.mark.asyncio
async def test_delete_cancels_the_call() -> None:
    gate, seen = asyncio.Event(), []
    async with _client(_app(gate, seen)) as client:
        answer = await client.post("/v1/sessions/s1/slow", headers=PREFER)
        location = answer.json()[view_pending.PENDING_KEY]["location"]
        cancelled = await client.delete(location)
        assert cancelled.status_code == 200 and cancelled.json()["cancelled"] is True
        await asyncio.sleep(0.1)
        settled = await client.get(location)
    assert settled.status_code == 409


@pytest.mark.asyncio
async def test_a_call_nobody_polls_is_cancelled_when_its_lease_lapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page went away: the call ends as a dropped request's call does."""
    monkeypatch.setattr(view_pending, "LEASE_S", 0.2)
    gate, seen = asyncio.Event(), []
    async with _client(_app(gate, seen)) as client:
        answer = await client.post("/v1/sessions/s1/slow", headers=PREFER)
        assert answer.status_code == 202
        await asyncio.sleep(0.5)
        assert seen[0].cancelled is True


@pytest.mark.asyncio
async def test_shutdown_cancels_every_pending_call() -> None:
    gate, seen = asyncio.Event(), []
    app = _app(gate, seen)
    async with _client(app) as client:
        await client.post("/v1/sessions/s1/slow", headers=PREFER)
        await client.post("/v1/sessions/s1/slow", headers=PREFER)
        assert view_pending.cancel_all(app) == 2
        assert all(lease.cancelled for lease in seen)
