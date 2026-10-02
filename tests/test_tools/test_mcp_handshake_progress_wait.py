"""MCP connects wait while THE AWAITED SERVER works; never a fixed deadline (#1577).

Found live (leg B, 2026-10-01): a handshake row read "did not respond within 20s" -- a
fixed wall-clock deadline, so a slow machine or a first launch (uv installing, Python
importing) reads as a dead server. The wait now samples the awaited server's OWN process
tree (the process the connect spawned): working -> keep waiting, up to
``tools.mcp.max_wait_s``; a whole window with no answer and no work -> typed
``NoProgressTimeout``. Real stdio MCP servers, real processes.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from clio_agent.tools import mcp_server_progress
from clio_agent.tools.mcp_server_progress import NoProgressTimeout, wait_while_server_works

_SERVER = """\
import sys, time
mode = sys.argv[1]
if mode == "slow":  # a cold start: works (CPU) for a while before it serves
    end = time.monotonic() + float(sys.argv[2])
    while time.monotonic() < end:
        pass
elif mode == "hung":  # alive, never answers, does nothing
    time.sleep(120)
from fastmcp import FastMCP
mcp = FastMCP("probe")
@mcp.tool
def ping() -> str:
    return "pong"
mcp.run(show_banner=False)
"""


@pytest.fixture
def server_script(tmp_path: Path) -> Path:
    path = tmp_path / "probe_server.py"
    path.write_text(_SERVER, encoding="utf-8")
    return path


@pytest.fixture
def busy_sibling() -> Iterator[subprocess.Popen[bytes]]:
    """A busy, unrelated child of this process -- what the clio_run daemon is."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "while True:\n    pass\n"],
        stdin=subprocess.DEVNULL,
    )
    yield proc
    proc.kill()
    proc.wait(timeout=10)


async def _list(script: Path, *args: str) -> list[str]:
    async with Client(StdioTransport(sys.executable, [str(script), *args])) as client:
        return [tool.name for tool in await client.list_tools()]


def test_a_server_still_starting_is_waited_for_past_the_window(server_script: Path) -> None:
    """The server works for 3 s before serving; the window is 1 s: it is listed.

    **Sabotage:** a fixed ``asyncio.wait_for(timeout=slice_s)`` -> TimeoutError at 1 s.
    """
    tools = asyncio.run(
        wait_while_server_works(_list(server_script, "slow", "3"), op_name="list", slice_s=1.0)
    )
    assert tools == ["ping"]


def test_a_hung_server_fails_typed_even_beside_a_busy_daemon(
    server_script: Path, busy_sibling: subprocess.Popen[bytes]
) -> None:
    """Only the awaited server's tree is measured: a busy sibling child (the clio_run
    daemon) never makes a hung server look busy.

    **Sabotage:** measure every descendant of this process (the old
    ``descendants_work``) -> the busy sibling keeps the wait alive to the ceiling.
    """
    started = time.monotonic()
    with pytest.raises(NoProgressTimeout) as info:
        asyncio.run(
            wait_while_server_works(_list(server_script, "hung"), op_name="list", slice_s=1.5)
        )
    assert info.value.reason == "no_progress"
    assert time.monotonic() - started < 30.0
    assert busy_sibling.poll() is None


def test_the_ceiling_bounds_even_a_working_server(
    server_script: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server_progress, "mcp_max_wait_s", lambda: 1.0)
    with pytest.raises(NoProgressTimeout) as info:
        asyncio.run(
            wait_while_server_works(_list(server_script, "slow", "60"), op_name="list", slice_s=0.5)
        )
    assert info.value.reason == "ceiling"


def test_a_wait_with_no_server_process_at_all_is_no_progress() -> None:
    """Nothing spawned for the awaited connect (a remote peer that never answers): one
    window, then typed -- never an unbounded wait."""

    async def never() -> None:
        await asyncio.sleep(60)

    with pytest.raises(NoProgressTimeout):
        asyncio.run(wait_while_server_works(never(), slice_s=0.2))
