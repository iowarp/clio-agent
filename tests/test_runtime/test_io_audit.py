"""Timed clio-core and I/O rows: the history writer, the barrier, a store call.

SABOTAGE (each turns the named test red):

* ``CoreIO._submit``: submit ``operation`` without ``_timed`` ->
  ``test_a_history_writer_job_reports_its_queue_wait_and_run_time``.
* ``highway_writer.flush_session``: drop the ``io_audit.barrier`` call ->
  ``test_the_highway_barrier_reports_its_wait``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.runtime import io_audit


@pytest.fixture
def audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(log))
    io_audit._state.update(at=-io_audit._RECHECK_S, on=False)
    yield log
    io_audit._state.update(at=-io_audit._RECHECK_S, on=False)


def _rows(log: Path, stage: str) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("stage") == stage]


def test_a_history_writer_job_reports_its_queue_wait_and_run_time(audit: Path) -> None:
    from clio_agent.gact.core_io import CoreIO

    io = CoreIO()

    async def go() -> None:
        first = io.submit(time.sleep, 0.05)  # the second job waits behind it
        await io.run(lambda: None)
        first.result()
        await io.close()

    asyncio.run(go())
    jobs = _rows(audit, "core_io.job")
    assert len(jobs) == 2
    assert jobs[0]["run_ms"] >= 40 and jobs[1]["wait_ms"] >= 40
    assert jobs[0]["thread"].startswith("clio-core-io")


def test_the_highway_barrier_reports_its_wait(audit: Path) -> None:
    from clio_agent.gact.highway_writer import flush_session

    writer = SimpleNamespace(flush=lambda sid, timeout=None, report=True: time.sleep(0.02))
    flush_session(SimpleNamespace(state=SimpleNamespace(highway_writer=writer)), "sess_1")
    [row] = _rows(audit, "highway.flush")
    assert row["session_id"] == "sess_1" and row["ms"] >= 15


def test_nothing_is_written_when_the_audit_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLIO_STREAM_AUDIT_LOG", raising=False)
    io_audit._state.update(at=-io_audit._RECHECK_S, on=False)
    io_audit.rpc("put", "segments", 0.01)
    assert not io_audit.enabled()


def test_a_store_call_names_the_clio_code_that_made_it(audit: Path) -> None:
    """On L1P a 27-step session made about 1,400 segment puts; the row names who asked,
    past the store plumbing. SABOTAGE: skip no frames and ``caller`` names the arc
    module that only forwards the write."""
    namespace: dict[str, Any] = {"io_audit": io_audit}
    plumbing = compile(
        "def append():\n    io_audit.rpc('put', 'segments', 0.001)\n",
        "/x/src/clio_agent/arc/fake_store.py", "exec",
    )  # fmt: skip
    asker = compile(
        "def step_done():\n    append()\n", "/x/src/clio_agent/gact/fake_rec.py", "exec"
    )
    exec(plumbing, namespace)
    exec(asker, namespace)
    namespace["step_done"]()
    [row] = _rows(audit, "store.rpc")
    assert row["caller"] == "gact.fake_rec.step_done"
