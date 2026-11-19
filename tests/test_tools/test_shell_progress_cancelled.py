"""A torn-down progress channel never strands a cancelled shell's cleanup.

When a stopped child's connection closes while a terminal chunk is being reported,
the report can fail as a cancelled await. The stream reader used to end cancelled
then, so the shell never confirmed its process-tree cleanup, the task stayed
``task_state_uncertain`` and the parent's cancellation never settled (DELETE 409).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from clio_agent.tools.servers.shell_output import StreamCapture, read_process_stream


class _Reader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""


class _ClosedChannel:
    """Reports fail as an await on a future its owner already cancelled."""

    def __init__(self) -> None:
        self.calls = 0

    async def report_progress(self, progress: float, total=None, message=None) -> None:
        self.calls += 1
        future = asyncio.get_running_loop().create_future()
        future.cancel()
        await future


class _Blocking:
    async def report_progress(self, progress: float, total=None, message=None) -> None:
        await asyncio.Event().wait()


def _capture(tmp_path: Path) -> StreamCapture:
    return StreamCapture("stdout", inline_limit=1024, spill_path=tmp_path / "out.txt")


def test_cancelled_delivery_does_not_end_the_reader(tmp_path: Path) -> None:
    """SABOTAGE: drop the CancelledError branch in ``_forward`` and the reader ends
    cancelled after the first chunk, leaving cleanup unconfirmed."""
    capture, channel = _capture(tmp_path), _ClosedChannel()

    async def scenario() -> asyncio.Task[None]:
        reader = asyncio.create_task(
            read_process_stream(_Reader([b"one\n", b"two\n"]), capture=capture, ctx=channel)
        )
        await asyncio.gather(reader)
        return reader

    reader = asyncio.run(scenario())
    assert not reader.cancelled() and reader.exception() is None
    assert channel.calls >= 2
    assert capture.full_text() == "one\ntwo\n"


def test_cancelling_the_reader_itself_still_cancels(tmp_path: Path) -> None:
    capture = _capture(tmp_path)

    async def scenario() -> asyncio.Task[None]:
        reader = asyncio.create_task(
            read_process_stream(_Reader([b"one\n"]), capture=capture, ctx=_Blocking())
        )
        for _ in range(5):
            await asyncio.sleep(0)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        return reader

    assert asyncio.run(scenario()).cancelled()
