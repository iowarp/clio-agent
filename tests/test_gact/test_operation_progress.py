"""Structured operation progress: ordered steps, honest measures, reuse, redaction."""

from __future__ import annotations

import json
from pathlib import Path

from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    InfrastructureOperation,
)
from clio_agent.gact.infrastructure.operation_events import LOG_EVENT, OperationEventLog
from clio_agent.gact.infrastructure.operation_progress import (
    OperationTracker,
    describe_command,
    redact,
)
from clio_agent.gact.infrastructure.operation_progress_parse import StepMeter
from clio_agent.gact.infrastructure.plan import DriverPlan, Readiness
from clio_agent.gact.infrastructure.reuse import Reuse, line
from clio_agent.gact.infrastructure.store import InfrastructureStore

PULL = CommandSpec(program="sh", args=["-c", "true", "clio-apptainer-pull"])
MKDIR = CommandSpec(program="mkdir", args=["-p", "--", "/x"])
RUN = CommandSpec(program="apptainer", args=["instance", "run", "x"])
STATUS = CommandSpec(program="sh", args=["-c", "status"])


def _tracker(tmp_path: Path) -> tuple[OperationTracker, InfrastructureStore, OperationEventLog]:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    row = store.put_operation(
        InfrastructureOperation(service_id="vllm", target_id="local", action="install")
    )
    events = OperationEventLog()
    return OperationTracker(store, events, row, persist_interval=0.0), store, events


def _plan() -> DriverPlan:
    return DriverPlan(
        (MKDIR, MKDIR, PULL, PULL, RUN),
        readiness=Readiness(STATUS, STATUS, STATUS, label="vLLM"),
    )


def test_steps_follow_the_plan_and_one_step_spans_both_pull_attempts(tmp_path: Path) -> None:
    tracker, store, _events = _tracker(tmp_path)
    tracker.begin()
    assert [step.state for step in tracker.row.steps] == ["running"]
    tracker.plan(_plan())
    labels = [step.label for step in tracker.row.steps]
    assert labels == [
        "Inspect target",
        "Prepare directories",
        "Pull the container image",
        "Start the container",
        "Wait for vLLM to answer",
        "Record the deployment",
    ]
    assert tracker.row.steps[0].state == "succeeded"

    ok = CommandResult(exit_code=0)
    for index, spec in enumerate(_plan().commands):
        tracker.command_started(index, spec)
        states = [step.state for step in tracker.row.steps]
        assert states.count("running") == 1
        if index == 2:
            assert tracker.row.current_step == 2
            tracker.command_finished(index, CommandResult(exit_code=124), ok=True)
            # The first attempt ran out of time: the pull step is still running.
            assert tracker.row.steps[2].state == "running"
            continue
        tracker.command_finished(index, ok)
    assert [step.state for step in tracker.row.steps[:4]] == ["succeeded"] * 4
    tracker.enter("readiness", "Waiting for vLLM")
    assert tracker.row.steps[4].state == "running"
    tracker.enter("finish", "Recording the deployment")
    row = tracker.finish("succeeded", "Completed")
    assert [step.state for step in row.steps] == ["succeeded"] * 6
    assert row.current_step is None and row.finished_at and row.elapsed_seconds is not None
    persisted = store.operation(row.id)
    assert persisted is not None and [step.state for step in persisted.steps] == ["succeeded"] * 6


def test_a_failed_and_a_cancelled_step_are_marked_and_the_rest_skipped(tmp_path: Path) -> None:
    tracker, _store, _events = _tracker(tmp_path)
    tracker.begin()
    tracker.plan(_plan())
    tracker.command_started(0, MKDIR)
    tracker.command_finished(0, CommandResult(exit_code=1), ok=False)
    row = tracker.finish("failed", "Failed.", error="mkdir failed")
    assert [step.state for step in row.steps] == [
        "succeeded",
        "failed",
        "skipped",
        "skipped",
        "skipped",
        "skipped",
    ]

    tracker, _store, _events = _tracker(tmp_path)
    tracker.begin()
    tracker.plan(_plan())
    tracker.command_started(2, PULL)
    row = tracker.finish("cancelled", "Cancelled.")
    assert row.steps[1].state == "skipped" and row.steps[2].state == "cancelled"


def test_a_reused_step_is_reported_and_marked(tmp_path: Path) -> None:
    tracker, store, events = _tracker(tmp_path)
    tracker.begin()
    tracker.plan(_plan())
    found = Reuse(kind="sif", thing="container image", identity="img@sha256:" + "a" * 64)
    tracker.command_started(2, PULL)
    tracker.output(line(found) + "\nCLIO_SHARED_IMAGE /s/x.sif\n")
    tracker.command_finished(2, CommandResult(exit_code=0))
    tracker.command_started(3, PULL)
    tracker.command_finished(3, CommandResult(exit_code=0))
    row = tracker.row
    assert row.steps[2].state == "reused"
    assert row.steps[2].message.startswith("Reusing container image (img@sha256:aaaaaaaaaaaa)")
    assert [(item.kind, item.step) for item in row.reused] == [("sif", 2)]
    published, _gap = events.after(row.id, 0)
    logs = [event.payload["line"] for event in published if event.type == LOG_EVENT]
    assert not any(text.startswith("CLIO_REUSE") for text in logs)  # structured, not shown raw
    assert logs[0].startswith("Reusing container image")

    # A reuse preflight that skips whole commands (Docker image present).
    tracker, store, events = _tracker(tmp_path)
    tracker.begin()
    tracker.plan(_plan())
    tracker.reused(found, step=tracker.step_of(2))
    tracker.command_skipped(2)
    tracker.command_skipped(3)
    assert tracker.row.steps[2].state == "reused"


def test_measures_are_determinate_only_where_measured() -> None:
    meter = StepMeter()
    assert meter.measure() is None
    meter.feed('CLIO_PROGRESS {"unit": "bytes", "current": 1024, "source": "layer_cache"}')
    progress = meter.measure()
    assert progress is not None and not progress.determinate and progress.fraction is None
    meter.feed('CLIO_PROGRESS {"unit": "bytes", "current": 0, "total": 4096, "layers": 3}')
    meter.feed('CLIO_PROGRESS {"unit": "bytes", "current": 1024}')
    progress = meter.measure()
    assert progress is not None and progress.determinate and progress.fraction == 0.25
    assert progress.unit == "bytes" and progress.total == 4096

    docker = StepMeter()
    for text in (
        "aaaaaaaaaaaa: Pulling fs layer",
        "bbbbbbbbbbbb: Pulling fs layer",
        "aaaaaaaaaaaa: Pull complete",
    ):
        docker.feed(text)
    progress = docker.measure()
    assert progress is not None and progress.unit == "layers" and progress.fraction == 0.5

    blobs = StepMeter()  # Apptainer blobs without a known total: a counter only
    blobs.feed("Copying blob 0123456789ab done")
    progress = blobs.measure()
    assert progress is not None and not progress.determinate and progress.current == 1

    build = StepMeter()
    build.feed("[ 45%] Building CXX object ggml.o")
    assert build.measure().fraction == 0.45  # type: ignore[union-attr]

    packages = StepMeter()
    packages.feed('CLIO_PROGRESS {"unit": "packages", "current": 0, "total": 4}')
    packages.feed("Collecting fastapi==0.115.0")
    assert packages.measure().fraction == 0.25  # type: ignore[union-attr]

    phase = StepMeter()
    phase.feed("INFO:    Creating SIF file...")
    progress = phase.measure()
    assert (
        progress is not None
        and not progress.determinate
        and progress.detail.startswith("Creating SIF")
    )


def test_secrets_are_redacted_but_ordinary_words_are_not() -> None:
    assert redact("key sk-live-secret-value here", ["sk-live-secret-value"]) == (
        "key [redacted] here"
    )
    assert redact("HF_TOKEN=hf_abc123 POSTGRES_PASSWORD: hunter22") == (
        "HF_TOKEN=[redacted] POSTGRES_PASSWORD: [redacted]"
    )
    assert redact('{"api_key": "abcd1234"}') == '{"api_key": "[redacted]"}'
    assert redact("Authorization: Bearer abcdefgh12345") == "Authorization: Bearer [redacted]"
    assert redact("using hf_" + "x" * 30) == "using [redacted]"
    text = "tokenizer: /models/qwen max_tokens: 4096 Resolved 120 packages"
    assert redact(text) == text


def test_commands_are_named_for_people() -> None:
    secret_run = CommandSpec(
        program="sh",
        args=["-c", 'IFS= read -r clio_secret; exec "$@"', "sh", "docker", "run", "x"],
        stdin="key\n",
    )
    assert describe_command(secret_run) == "Start the container"
    node = CommandSpec(program="python3", args=["-c", "x"], stdin=json.dumps({"action": "install"}))
    assert describe_command(node) == "Install the service"
    deploy = CommandSpec(program="bash", args=["-lc", "# clio-deploy:install\nset -e"])
    assert describe_command(deploy) == "CLIO install"
    assert describe_command(PULL) == "Pull the container image"
