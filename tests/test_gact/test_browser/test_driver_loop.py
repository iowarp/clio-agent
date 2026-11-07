"""The browser loop starts Playwright's driver outside the server's process group."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from clio_agent.gact.browser.driver_loop import driver_loop

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Windows has no process groups")


def test_a_stop_signal_to_the_servers_group_does_not_reach_the_driver() -> None:
    async def child_session() -> int:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os; print(os.getsid(0))",
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await process.communicate()
        return int(out)

    loop = driver_loop()
    try:
        session = loop.run_until_complete(child_session())
    finally:
        loop.close()
    assert session != os.getsid(0)
