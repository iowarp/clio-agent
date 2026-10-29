"""Windows bootstrap overlaps independent checks without relaxing readiness barriers."""

from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi import FastAPI

from clio_agent.gact import server_boot
from clio_agent.runtime import sandbox


@pytest.mark.asyncio
async def test_windows_census_overlaps_detection_but_fence_selection_is_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked native detector must not prevent census progress or publish a fence early."""
    app = FastAPI()
    loop_thread = threading.get_ident()
    census_started = threading.Event()
    detector_started = threading.Event()
    release_detector = threading.Event()
    release_census = asyncio.Event()
    verdict = object()

    async def census(_app: FastAPI) -> None:
        census_started.set()
        await release_census.wait()

    def detect() -> object:
        assert threading.get_ident() != loop_thread
        assert census_started.wait(2)
        detector_started.set()
        assert release_detector.wait(2)
        return verdict

    monkeypatch.setattr(server_boot, "reap_orphans", census)
    monkeypatch.setattr(sandbox, "install_sandbox", detect)
    preparation = asyncio.create_task(server_boot.prepare_protection(app, windows=True))
    try:
        assert await asyncio.to_thread(detector_started.wait, 2)
        assert not preparation.done()
        assert not hasattr(app.state, "sandbox")
        release_detector.set()
        await preparation
        assert app.state.sandbox is verdict
        assert not app.state.orphan_reap_task.done()
    finally:
        release_detector.set()
        release_census.set()
        await preparation
        await app.state.orphan_reap_task


@pytest.mark.asyncio
async def test_other_platforms_preserve_fence_then_census_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Linux/macOS startup path retains its synchronous detector and ordering."""
    app = FastAPI()
    calls: list[str] = []
    loop_thread = threading.get_ident()

    def detect() -> object:
        assert threading.get_ident() == loop_thread
        calls.append("fence")
        return object()

    async def census(_app: FastAPI) -> None:
        calls.append("census")

    monkeypatch.setattr(server_boot, "reap_orphans", census)
    monkeypatch.setattr(sandbox, "install_sandbox", detect)
    await server_boot.prepare_protection(app, windows=False)
    assert calls == ["fence"]
    await app.state.orphan_reap_task
    assert calls == ["fence", "census"]


@pytest.mark.asyncio
async def test_failed_detector_cancels_owned_census_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detector error cannot leave a live bootstrap task or become a ready fence."""
    app = FastAPI()
    census_started = threading.Event()
    census_stopped = asyncio.Event()

    async def census(_app: FastAPI) -> None:
        census_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            census_stopped.set()

    def detect() -> object:
        assert census_started.wait(2)
        raise ValueError("invalid sandbox policy")

    monkeypatch.setattr(server_boot, "reap_orphans", census)
    monkeypatch.setattr(sandbox, "install_sandbox", detect)
    with pytest.raises(ValueError, match="invalid sandbox policy"):
        await server_boot.prepare_protection(app, windows=True)
    assert census_stopped.is_set()
    assert app.state.orphan_reap_task.cancelled()
    assert not hasattr(app.state, "sandbox")


@pytest.mark.asyncio
async def test_cancelled_preparation_does_not_publish_a_late_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation retires the async census and cannot stamp a late detector result."""
    app = FastAPI()
    detector_started = threading.Event()
    release_detector = threading.Event()
    detector_stopped = threading.Event()
    census_stopped = asyncio.Event()

    async def census(_app: FastAPI) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            census_stopped.set()

    def detect() -> object:
        detector_started.set()
        assert release_detector.wait(2)
        detector_stopped.set()
        return object()

    monkeypatch.setattr(server_boot, "reap_orphans", census)
    monkeypatch.setattr(sandbox, "install_sandbox", detect)
    preparation = asyncio.create_task(server_boot.prepare_protection(app, windows=True))
    try:
        assert await asyncio.to_thread(detector_started.wait, 2)
        preparation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await preparation
        assert census_stopped.is_set()
        assert not hasattr(app.state, "sandbox")
    finally:
        release_detector.set()
        assert await asyncio.to_thread(detector_stopped.wait, 2)
