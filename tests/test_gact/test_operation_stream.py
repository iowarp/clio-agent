"""The live operation stream: SSE log lines in order and redacted; cancel keeps caches."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from clio_agent.gact.infrastructure import reuse
from clio_agent.gact.infrastructure import runtime as runtime_module
from clio_agent.gact.infrastructure.container_runtime import image_store_key, pull_commands
from clio_agent.gact.infrastructure.local_executor import run_local
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    InfrastructureOperation,
    ServiceActionRequest,
)
from clio_agent.gact.infrastructure.operation_events import TERMINAL_EVENT
from clio_agent.gact.infrastructure.operation_progress import OperationTracker
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.resource_ledger import shared_image_recorder
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.infrastructure.transport import InfrastructureTransportRegistry
from clio_agent.gact.routes.infrastructure import register_infrastructure_routes

posix_only = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("timeout"), reason="POSIX shell and processes"
)
SECRET = "s3cr3t-value-1234"
IMAGE = "localhost:1/clio/vllm@sha256:" + "ab" * 32
LAYER = "cd" * 32


def _frames(text: str) -> list[dict[str, Any]]:
    frames = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            frames.append(
                {
                    "event": fields["event"],
                    "id": int(fields["id"]) if "id" in fields else None,
                    "data": json.loads(fields["data"]),
                }
            )
    return frames


async def test_the_event_stream_sends_log_lines_in_order_redacted(tmp_path: Path) -> None:
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)
    store = app.state.infrastructure_store
    events = app.state.infrastructure_runtime.events
    row = store.put_operation(
        InfrastructureOperation(service_id="vllm", target_id="local", action="install")
    )
    spec = CommandSpec(program="sh", args=["-c", "pull"])
    tracker = OperationTracker(store, events, row)
    tracker.add_secrets(SECRET)
    tracker.begin()
    tracker.plan(DriverPlan((spec,)))
    tracker.command_started(0, spec)

    async def produce() -> None:
        for number in range(5):
            await asyncio.sleep(0.02)
            # Chunks need not be whole lines.
            tracker.output(f"line {number} key {SECRET}")
            tracker.output(f" token={SECRET}\n")
        tracker.command_finished(0, CommandResult(exit_code=0))
        tracker.finish("succeeded", "Completed")

    producer = asyncio.create_task(produce())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://clio") as client:
        response = await client.get(f"/v1/infrastructure/operations/{row.id}/events")
        await producer
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert SECRET not in response.text
        frames = _frames(response.text)
        assert frames[0]["event"] == "operation.snapshot" and frames[0]["id"] is None
        assert frames[-1]["event"] == TERMINAL_EVENT
        assert frames[-1]["data"]["payload"]["state"] == "succeeded"
        logs = [frame for frame in frames if frame["event"] == "operation.log"]
        assert [frame["data"]["payload"]["line"] for frame in logs] == [
            f"line {number} key [redacted] token=[redacted]" for number in range(5)
        ]
        ids = [frame["id"] for frame in frames if frame["id"] is not None]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        assert any(frame["event"] == "operation.progress" for frame in frames)

        # Resume after the third line: only what came later.
        third = logs[2]["id"]
        again = await client.get(
            f"/v1/infrastructure/operations/{row.id}/events",
            headers={"Last-Event-ID": str(third)},
        )
        resumed = [frame for frame in _frames(again.text) if frame["event"] == "operation.log"]
        assert [frame["id"] for frame in resumed] == [frame["id"] for frame in logs[3:]]

        polled = (await client.get(f"/v1/infrastructure/operations/{row.id}/log")).json()
        assert [entry["line"] for entry in polled["lines"]] == [
            frame["data"]["payload"]["line"] for frame in logs
        ]
        assert polled["complete"] is True

        persisted = (await client.get(f"/v1/infrastructure/operations/{row.id}")).json()
        assert persisted["steps"][1]["state"] == "succeeded"
        assert persisted["log_cursor"] >= logs[-1]["id"]


async def test_a_record_from_before_a_restart_streams_its_snapshot_and_ends(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)
    row = app.state.infrastructure_store.put_operation(
        InfrastructureOperation(
            service_id="vllm", target_id="local", action="install", state="failed"
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://clio") as client:
        response = await client.get(f"/v1/infrastructure/operations/{row.id}/events")
        assert [frame["event"] for frame in _frames(response.text)] == [
            "operation.snapshot",
            TERMINAL_EVENT,
        ]
        missing = await client.get("/v1/infrastructure/operations/nope/events")
        assert missing.status_code == 404


@posix_only
async def test_the_local_executor_streams_in_order_and_keeps_a_bounded_result() -> None:
    chunks: list[tuple[str, str]] = []
    result = await run_local(
        CommandSpec(program="sh", args=["-c", "echo one; echo two >&2; echo three"]),
        lambda text, stream: chunks.append((text, stream)),
    )
    assert result.exit_code == 0 and result.stdout == "one\nthree\n" and result.stderr == "two\n"
    assert [text for text, stream in chunks if stream == "stdout"] == ["one\n", "three\n"]
    timed_out = await run_local(
        CommandSpec(program="sh", args=["-c", "echo early; sleep 30"], timeout_seconds=1)
    )
    assert timed_out.exit_code == 124 and timed_out.stdout == "early\n"


def _fake_apptainer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hang, pidfile = tmp_path / "hang", tmp_path / "pid"
    script = bin_dir / "apptainer"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{tmp_path / "calls.txt"}"\n'
        "echo 'Copying blob 0123456789ab done'\n"
        'mkdir -p "$APPTAINER_CACHEDIR/blob/blobs/sha256"\n'
        f'echo layer > "$APPTAINER_CACHEDIR/blob/blobs/sha256/{LAYER}"\n'
        f'if [ -f "{hang}" ]; then echo $$ > "{pidfile}"; exec sleep 60; fi\n'
        'out=""; for a; do case "$a" in *.partial) out="$a";; esac; done\n'
        'echo sif > "$out"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return hang, pidfile


async def _settled(store: InfrastructureStore, operation_id: str) -> InfrastructureOperation:
    for _ in range(600):
        row = store.operation(operation_id)
        if row is not None and row.state not in {"queued", "running"}:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("the operation did not settle")


async def _until(condition: Any) -> None:
    for _ in range(600):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never held")


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@posix_only
async def test_cancel_mid_pull_keeps_the_layer_cache_and_a_retry_then_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hang, pidfile = _fake_apptainer(tmp_path, monkeypatch)
    hang.write_text("")
    store_dir, images = tmp_path / "store", tmp_path / "svc/images"
    images.mkdir(parents=True)
    cache = store_dir / "cache/blob/blobs/sha256" / LAYER
    sif = store_dir / f"{image_store_key(IMAGE)}.sif"

    def plan(**kwargs: Any) -> DriverPlan:
        fresh = reuse.from_scratch(kwargs["configuration"])
        scratch = str(tmp_path / "tmp")
        pulls = pull_commands(
            "apptainer", IMAGE, str(images), str(store_dir), "clio-vllm", scratch, fresh=fresh
        )
        return DriverPlan(tuple(pulls), recorders={1: shared_image_recorder()})

    store = InfrastructureStore(tmp_path / "infrastructure.json")
    runtime = InfrastructureRuntime(store, InfrastructureTransportRegistry(lambda *_: None))

    async def catalog(_target_id: str) -> Any:
        return SimpleNamespace(
            facts=SimpleNamespace(os="linux"), services=[SimpleNamespace(id="web_search")]
        )

    monkeypatch.setattr(runtime, "catalog", catalog)
    monkeypatch.setattr(runtime_module, "build_driver_plan", plan)

    def install(**configuration: str) -> InfrastructureOperation:
        request = ServiceActionRequest(
            action="install", variant_id="container", configuration=configuration
        )
        return runtime.start_action("web_search", request)

    # 1. A cold pull is cancelled mid-download.
    first = install()
    await _until(pidfile.exists)
    pid = int(pidfile.read_text())
    cancelled = await runtime.cancel(first.id)
    assert cancelled.state == "cancelled"
    pull_step = next(step for step in cancelled.steps if step.id == "command-0")
    assert pull_step.state == "cancelled"
    await _until(lambda: _gone(pid))  # the pull's process group was stopped
    assert cache.read_text() == "layer\n"  # the layer cache is kept
    assert not sif.exists() and not (store_dir / f"{sif.name}.ref").exists()
    assert runtime.events.after(first.id, 0)[0][-1].type == TERMINAL_EVENT
    logged = [
        event.payload["line"]
        for event in runtime.events.after(first.id, 0)[0]
        if event.type == "operation.log"
    ]
    assert "Copying blob 0123456789ab done" in logged  # streamed before the cancel

    # 2. The retry completes the pull (nothing to reuse yet).
    hang.unlink()
    second = await _settled(store, install().id)
    assert second.state == "succeeded", second.error
    assert second.reused == [] and sif.is_file() and cache.is_file()
    assert [step.state for step in second.steps][:2] == ["succeeded", "succeeded"]

    # 3. The next install reuses the verified SIF and says so.
    third = await _settled(store, install().id)
    assert third.state == "succeeded", third.error
    [found] = third.reused
    assert found.kind == "sif" and found.identity == IMAGE and found.message.startswith("Reusing")
    assert third.steps[1].state == "reused"
    calls = (tmp_path / "calls.txt").read_text().splitlines()
    assert len(calls) == 2

    # 4. From scratch bypasses the reuse; the flag is not kept on the record.
    fourth = await _settled(store, install(**{reuse.FROM_SCRATCH_KEY: "true"}).id)
    assert fourth.state == "succeeded" and fourth.from_scratch and fourth.reused == []
    assert len((tmp_path / "calls.txt").read_text().splitlines()) == 3
    record = store.service("local", "web_search")
    assert record is not None and reuse.FROM_SCRATCH_KEY not in record.configuration
