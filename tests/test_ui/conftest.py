"""Fixtures for the CLI (ui) test suite.

Reuses the SDK suite's in-process ASGI plumbing: the CLI is exercised as a
thin client against the REAL gact app driven by a :class:`StubAgent`, over
:class:`StreamingASGITransport` — no live server, no spawning. The injected
:class:`ClioClient` is exactly the seam :class:`ClioAgentCLI` was built to
take.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.app import build_app
from clio_agent.sdk import ClioClient
from tests.test_sdk.conftest import StreamingASGITransport, StubAgent, _fresh_arc


@pytest.fixture()
def stub_agent() -> StubAgent:
    return StubAgent(answer="Compression ratio was 3.2x on the HDF5 dataset.")


@pytest.fixture()
def app(tmp_path: Path, stub_agent: StubAgent) -> Any:
    return build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=stub_agent,
        arc=_fresh_arc(tmp_path),
    )


@pytest.fixture()
def transport(app: Any) -> Iterator[StreamingASGITransport]:
    t = StreamingASGITransport(app)
    yield t
    t.close()


@pytest.fixture()
def client(transport: StreamingASGITransport) -> Iterator[ClioClient]:
    with ClioClient("http://testserver", transport=transport) as c:
        yield c
