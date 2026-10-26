"""Startup steps are timed inside the server and served by ``/v1/health`` (B0)."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import boot_timing
from clio_agent.gact.app import build_app
from clio_agent.gact.boot_timing import BootTimeline


def test_step_records_name_thread_times_and_ok_outcome() -> None:
    timeline = BootTimeline(started_at=0.0)
    with timeline.step("restore"):
        pass
    (step,) = timeline.snapshot()["steps"]
    assert step["name"] == "restore"
    assert step["thread"] == threading.current_thread().name
    assert step["outcome"] == "ok"
    assert step["end_s"] >= step["start_s"] > 0
    assert step["seconds"] == pytest.approx(step["end_s"] - step["start_s"], abs=0.002)


def test_failed_step_records_the_exception_type_and_reraises() -> None:
    timeline = BootTimeline(started_at=0.0)
    with pytest.raises(KeyError), timeline.step("migrate"):
        raise KeyError("missing")
    assert timeline.snapshot()["steps"][0]["outcome"] == "KeyError"


def test_running_steps_are_listed_until_they_finish() -> None:
    timeline = BootTimeline(started_at=0.0)
    with timeline.step("attach"):
        running = timeline.snapshot()["running"]
        assert [row["name"] for row in running] == ["attach"]
        assert timeline.snapshot()["steps"] == []
    assert timeline.snapshot()["running"] == []


def test_timed_awaits_and_returns_the_result() -> None:
    timeline = BootTimeline(started_at=0.0)

    async def work() -> int:
        await asyncio.sleep(0)
        return 7

    assert asyncio.run(timeline.timed("agent_construct", work())) == 7
    assert timeline.snapshot()["steps"][0]["name"] == "agent_construct"


def test_a_mark_keeps_its_first_time() -> None:
    timeline = BootTimeline(started_at=0.0)
    timeline.mark("serving")
    first = timeline.snapshot()["marks"]["serving"]
    timeline.mark("serving")
    assert timeline.snapshot()["marks"]["serving"] == first


def test_times_are_measured_from_process_start() -> None:
    timeline = BootTimeline()
    assert timeline.now() > 0


def test_the_app_timeline_is_created_once_on_first_use() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    first = boot_timing.timeline(app)
    assert boot_timing.timeline(app) is first
    boot_timing.mark(app, "agent_ready")
    assert "agent_ready" in boot_timing.snapshot(app)["marks"]


def test_health_serves_the_boot_block() -> None:
    with TestClient(build_app()) as client:
        body = client.get("/v1/health").json()
    boot = body["boot"]
    assert set(boot) == {"steps", "running", "marks"}
    assert "lifespan_started" in boot["marks"]
    assert "serving" in boot["marks"]
