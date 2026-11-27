"""A run lasts until a turn completes that no Stop hook blocked.

Live (``clio-agent -q``, 2026-11-25): a Stop hook denied the end of beam-1.0-fp8's
turn and queued one more, but ``-q`` returned on the first completion; the
harness then stopped the server and the re-driven turn was cancelled (or, with a
shared home, resumed after the next ``clio start`` under another model).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.client import ClioClient
from clio_agent.gact.hooks import install_global_dispatcher
from tests.test_client.conftest import StubAgent
from tests.test_gact._hook_fixtures import make_command_dispatcher

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@pytest.fixture
def block_stop_once(tmp_path: Path):
    marker = tmp_path / "blocked_once"
    body = (
        "import os, sys\n"
        f"marker = {str(marker)!r}\n"
        "if not os.path.exists(marker):\n"
        "    open(marker, 'w').write('x')\n"
        "    sys.stderr.write('not finished: answer.md is missing')\n"
        "    sys.exit(2)\n"
        "sys.exit(0)\n"
    )
    install_global_dispatcher(make_command_dispatcher(tmp_path, event="Stop", body=body))
    yield
    install_global_dispatcher(None)


def test_run_follows_the_turn_a_stop_hook_queued(
    block_stop_once: None, client: ClioClient, stub_agent: StubAgent
) -> None:
    result = client.session(title="stop hook").run("make the figure")
    assert result.status == "completed", result.error
    assert [q for q, _ in stub_agent.calls][0] == "make the figure"
    assert len(stub_agent.calls) == 2  # the re-driven turn ran before run() returned
    assert "answer.md is missing" in stub_agent.calls[1][0]


def test_run_without_a_blocking_hook_ends_on_its_turn(
    client: ClioClient, stub_agent: StubAgent
) -> None:
    result = client.session(title="no hook").run("hello")
    assert result.status == "completed"
    assert len(stub_agent.calls) == 1
