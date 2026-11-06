"""One version per interval for a file a background script keeps registering (Part 4).

A view bound to ``artifact://<workspace>/<name>@latest`` follows a file through its
versions. A background script that registers its status file every second records one
version per ``artifacts.version_interval_s``: the registrations inside the interval are
answered at once and one version is recorded when it ends, with the file as it is then.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.agents.resolution import session_agent_def
from clio_agent.gact.agents.tool_instrumentation import pop_declared_structured_content
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts import version_pacing
from clio_agent.gact.artifacts.designation_tool import build_create_artifact_tool
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.code_task_context import _BACKGROUND_TASK, BackgroundTask
from tests.test_gact.test_document_artifacts import _workspace_session


@pytest.fixture(autouse=True)
def _consume_declared_structured_content() -> Iterator[None]:
    yield
    pop_declared_structured_content()


@pytest.fixture
def interval(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setenv("CLIO_ARTIFACTS_VERSION_INTERVAL_S", "0.4")
    return 0.4


def _in_background() -> Any:
    return _BACKGROUND_TASK.set(BackgroundTask("task_1", threading.Event()))


def test_registrations_inside_the_interval_become_one_version_with_the_latest_call(
    interval: float,
) -> None:
    """SABOTAGE: register every call at once (return ``None`` in ``pace``) and the
    three calls record three versions, not two."""

    recorded: list[str] = []
    token = _in_background()
    try:
        first = version_pacing.pace("s1", "ws", "status.csv", lambda: recorded.append("v1"))
        second = version_pacing.pace("s1", "ws", "status.csv", lambda: recorded.append("v2"))
        third = version_pacing.pace("s1", "ws", "status.csv", lambda: recorded.append("v3"))
    finally:
        _BACKGROUND_TASK.reset(token)
    assert first is None  # the first registration is the caller's to make now
    recorded.append("v1")
    assert second is not None and second["deferred"] and second["name"] == "status.csv"
    assert third is not None and third["version_at"] == second["version_at"]
    deadline = time.monotonic() + 5
    while len(recorded) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(interval)
    assert recorded == ["v1", "v3"]  # one version at the interval's end, the latest call's


def test_a_call_outside_a_background_script_registers_at_once(interval: float) -> None:
    assert version_pacing.pace("s2", "ws", "a.csv", lambda: None) is None
    assert version_pacing.pace("s2", "ws", "a.csv", lambda: None) is None


def test_a_background_script_s_file_gets_one_version_per_interval(
    tmp_path: Path, interval: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through ``create_artifact``: the deferred version holds the file's bytes when the
    interval ends."""

    # Long enough that two tool calls on a loaded runner still land inside one interval.
    monkeypatch.setenv("CLIO_ARTIFACTS_VERSION_INTERVAL_S", "3")

    root = tmp_path / "workspace"
    root.mkdir()
    status = root / "status.csv"
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        workspace_id, session_id = _workspace_session(client, root)
        app = cast(FastAPI, client.app)
        tool = build_create_artifact_tool(session_agent_def(app, session_id)).func
        app_token = context.set_app(app)
        session_token = context.set_session_id(session_id)
        task_token = _in_background()
        try:
            status.write_text("t,c\n0,20\n")
            first = tool(path=str(status), kind="dataset", purpose="intermediate")
            status.write_text("t,c\n1,21\n")
            second = tool(path=str(status), kind="dataset", purpose="intermediate")
            status.write_text("t,c\n2,22\n")
        finally:
            _BACKGROUND_TASK.reset(task_token)
            context.reset(session_token)
            context.reset(app_token)
        assert first["created"] == 1
        [deferred] = second["artifacts"]
        assert deferred["deferred"] is True and second["created"] == 0
        record = get_registry(app).get(workspace_id, "status.csv")
        assert record is not None and len(record.versions) == 1
        deadline = time.monotonic() + 10
        while len(record.versions) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
            record = get_registry(app).get(workspace_id, "status.csv")
            assert record is not None
        assert len(record.versions) == 2
        assert record.versions[-1].size_bytes == len("t,c\n2,22\n")


def test_a_clean_stop_records_the_waiting_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: make ``flush_pending`` return without running the waiting registration
    and the version is lost at shutdown."""

    monkeypatch.setenv("CLIO_ARTIFACTS_VERSION_INTERVAL_S", "60")
    recorded: list[str] = []
    token = _in_background()
    try:
        assert version_pacing.pace("s3", "ws", "b.csv", lambda: recorded.append("v1")) is None
        assert version_pacing.pace("s3", "ws", "b.csv", lambda: recorded.append("v2"))
    finally:
        _BACKGROUND_TASK.reset(token)
    assert version_pacing.flush_pending() == 1
    assert recorded == ["v2"]
