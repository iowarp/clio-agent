"""Library runs against the in-process server with its scripted agent.

``ClioClient.run`` / ``session`` / ``run_flow`` and ``AsyncClioClient`` end to end
over the real GACT app (only the model is a stub), as ``clio-agent -q`` uses them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clio_agent.client import AsyncClioClient, ClioClient, ClioOptions, RunResult

#: The stub host serves the session agent's tools, as in the CLI pause tests.
pytestmark = pytest.mark.usefixtures("host_agent_executor")

# --------------------------------------------------------------------------- #
# runs against the in-process server (scripted agent)
# --------------------------------------------------------------------------- #


def test_run_returns_a_completed_result(
    client: ClioClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = client.run("what is the compression ratio?")
    assert isinstance(result, RunResult)
    assert result.status == "completed", result.error.model_dump() if result.error else None
    assert result.text == "Compression ratio was 3.2x on the HDF5 dataset."
    assert result.session_id.startswith("sess_")
    session = client.sessions.get(result.session_id)
    assert session.metadata["unattended"] is True
    assert session.workspace_id != "ws_default"  # rooted at the working folder
    assert {"turns", "tokens", "cost_usd"} <= result.usage.keys()


def test_session_streams_events_then_the_result(
    client: ClioClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = client.session(title="multi")
    items = list(handle.stream("first"))
    assert isinstance(items[-1], RunResult) and items[-1].status == "completed"
    assert any(getattr(item, "type", "") == "message.completed" for item in items)
    again = client.session(id=handle.id).run("second")
    assert again.session_id == handle.id and again.status == "completed"


def test_unknown_flow_is_a_failed_result_not_an_exception(
    client: ClioClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = client.run_flow("no-such-flow", "anything")
    assert result.status == "failed"
    assert result.error is not None and result.error.error == "flow_not_resolved"


def test_async_client_runs_the_same(
    transport: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> RunResult:
        options = ClioOptions(url="http://testserver")
        async with AsyncClioClient(options, transport=transport) as c:
            items = [item async for item in (await c.session()).stream("hi")]
            assert isinstance(items[-1], RunResult)
            return await c.run("again")

    assert asyncio.run(main()).status == "completed"
