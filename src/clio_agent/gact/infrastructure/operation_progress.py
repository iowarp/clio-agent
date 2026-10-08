"""Structured progress and the live log of one running infrastructure operation.

The runtime drives an :class:`OperationTracker` through the operation: the
plan becomes ordered steps (consecutive commands of one kind -- the two
bounded Apptainer pull attempts -- are one step), each command's output is
fed in as it arrives (local streaming, Desktop ``exec_output`` frames, or a
followed target log), redacted, measured (:mod:`.operation_progress_parse`),
scanned for reuse lines (:mod:`.reuse`) and published as live log events
(:mod:`.operation_events`). Every write of the durable operation record goes
through :meth:`OperationTracker.put`, so the steps are never overwritten by a
stale copy; frequent progress is persisted at most once per
``persist_interval`` and always at step transitions and the end.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.gact.infrastructure import reuse as reuse_helper
from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec
from clio_agent.gact.infrastructure.operation_events import (
    LOG_EVENT,
    TERMINAL_EVENT,
    OperationEventLog,
)
from clio_agent.gact.infrastructure.operation_models import OperationStep, ReuseReport, StepState
from clio_agent.gact.infrastructure.operation_progress_parse import StepMeter
from clio_agent.gact.semantic_events import REDACTED_VALUE, SENSITIVE_KEYS

if TYPE_CHECKING:
    from clio_agent.gact.infrastructure.models import InfrastructureOperation
    from clio_agent.gact.infrastructure.plan import DriverPlan
    from clio_agent.gact.infrastructure.store import InfrastructureStore

MAX_LINE_CHARS = 4000
_KEYS = "|".join(sorted(re.escape(key) for key in SENSITIVE_KEYS | {"api-key", "apikey"}))
# ``<...sensitive key>`` then ``=`` / ``:`` then a value (not a Bearer/Basic
# scheme word, handled below, nor an already redacted value).
_ASSIGNED = re.compile(
    rf"(?i)([\w.-]*(?:{_KEYS}))(\"?\s*[=:]\s*\"?)"
    rf"((?!(?:bearer|basic)\b)(?!\[redacted\])[^\s\"',;}}]{{4,}})"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_SHAPES = re.compile(
    r"\b(?:hf_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"
)
_CLIO_DEPLOY = re.compile(r"^# clio-deploy:([\w-]+)")
_NODE_ACTIONS = {
    "prepare": "Prepare the service directory",
    "install": "Install the service",
    "start": "Start the service",
    "stop": "Stop the service",
    "status": "Check the service",
    "uninstall": "Remove the service",
    "delete_data": "Delete retained data",
    "verify": "Verify the service",
    "logs": "Read the service logs",
}
_ENGINE_VERBS = {
    "pull": "Pull the container image",
    "image": "Check for the image",
    "run": "Start the container",
    "start": "Start the container",
    "rm": "Remove the previous container",
    "stop": "Stop the container",
    "exec": "Prepare the model",
    "instance": "Start the container",
    "logs": "Read the container logs",
    "inspect": "Check the container",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(line: str, secrets: Iterable[str] = ()) -> str:
    """``line`` without known secrets, credential assignments or token shapes.

    Known values are replaced first (the house ``[redacted]`` convention of the
    supervisors and ``semantic_events``), then ``<sensitive key>=value`` /
    ``"<sensitive key>": "value"`` (``SENSITIVE_KEYS``), ``Bearer``/``Basic``
    credentials and well-known token shapes.
    """

    for secret in secrets:
        line = line.replace(secret, REDACTED_VALUE)
    line = _BEARER.sub(lambda match: f"{match.group(1)} {REDACTED_VALUE}", line)
    line = _ASSIGNED.sub(lambda match: match.group(1) + match.group(2) + REDACTED_VALUE, line)
    return _TOKEN_SHAPES.sub(REDACTED_VALUE, line)


def describe_command(spec: CommandSpec) -> str:
    """A person-facing step name for a command the plan did not label."""

    program, args = spec.program.rsplit("/", 1)[-1], spec.args
    script = args[1] if len(args) > 1 and args[0] in {"-c", "-lc"} else ""
    if program == "sh" and "read -r clio_secret" in script and len(args) > 4:
        return describe_command(CommandSpec(program=args[3], args=args[4:]))
    if len(args) > 2 and args[2] == "clio-apptainer-pull":
        return "Pull the container image"
    if match := _CLIO_DEPLOY.match(script):
        return f"CLIO {match.group(1).replace('-', ' ')}"
    if program in {"docker", "podman", "apptainer"} and args:
        return _ENGINE_VERBS.get(args[0], f"Run {program} {args[0]}")
    if program == "python3" and spec.stdin:
        try:
            action = json.loads(spec.stdin).get("action", "")
        except (ValueError, AttributeError):
            action = ""
        return _NODE_ACTIONS.get(action, "Run the service supervisor")
    if program in {"mkdir"} or "CLIO_CREATED_DIR" in script:
        return "Prepare directories"
    if "port_in_use" in script:
        return "Check the port is free"
    if "still present" in script:
        return "Remove previous resources"
    if program == "test":
        return "Check for the image"
    if program == "uv" and args[:2] == ["tool", "install"]:
        return "Install the package"
    return f"Run {program}"


class OperationTracker:
    """Own the durable operation record and live timeline while an operation runs."""

    def __init__(
        self,
        store: InfrastructureStore,
        events: OperationEventLog,
        row: InfrastructureOperation,
        *,
        secrets: Iterable[str] = (),
        persist_interval: float = 1.0,
        publish_interval: float = 0.25,
    ) -> None:
        self.store = store
        self.events = events
        self._row = row
        self._secrets = {value for value in secrets if value and len(value) >= 4}
        self._persist_interval = persist_interval
        self._publish_interval = publish_interval
        self._started = time.monotonic()
        self._step_started: dict[int, float] = {}
        self._command_step: dict[int, int] = {}
        self._step_commands: dict[int, list[int]] = {}
        self._skipped: set[int] = set()
        self._meters: dict[int, StepMeter] = {}
        self._partial: dict[str, str] = {}
        self._streamed = False
        self._last_persist = 0.0
        self._last_publish = 0.0

    @property
    def row(self) -> InfrastructureOperation:
        """The latest operation record."""

        return self._row

    # ---- record writes -----------------------------------------------------

    def put(self, **updates: Any) -> InfrastructureOperation:
        """Persist ``updates`` on the latest record (and publish the progress)."""

        self._row = self._row.model_copy(update=updates)
        self._flush(force=True)
        return self._row

    def _flush(self, *, force: bool = False) -> None:
        now = time.monotonic()
        steps = list(self._row.steps)
        for index, started in self._step_started.items():
            if index < len(steps) and steps[index].state == "running":
                steps[index] = steps[index].model_copy(
                    update={"elapsed_seconds": round(now - started, 1)}
                )
        self._row = self._row.model_copy(
            update={
                "steps": steps,
                "elapsed_seconds": round(now - self._started, 1),
                "log_cursor": self.events.latest_id(self._row.id),
            }
        )
        if force or now - self._last_persist >= self._persist_interval:
            self._row = self.store.put_operation(self._row)
            self._last_persist = now
        if force or now - self._last_publish >= self._publish_interval:
            self._last_publish = now
            self.events.publish(self._row.id, "operation.progress", self.progress_payload())

    def progress_payload(self) -> dict[str, Any]:
        """The progress part of the record, as published on the event stream."""

        return self._row.model_dump(
            mode="json",
            include={
                "state",
                "progress",
                "steps",
                "current_step",
                "reused",
                "started_at",
                "elapsed_seconds",
                "from_scratch",
                "error",
            },
        )

    # ---- steps ---------------------------------------------------------------

    def begin(self, *, from_scratch: bool = False) -> None:
        """Mark the operation running with its first step, inspecting the target."""

        self._step_started[0] = time.monotonic()
        self.put(
            state="running",
            progress="Inspecting target",
            started_at=_now(),
            from_scratch=from_scratch,
            current_step=0,
            steps=[
                OperationStep(
                    id="inspect", label="Inspect target", state="running", started_at=_now()
                )
            ],
        )

    def plan(self, plan: DriverPlan) -> None:
        """Expand the steps from the compiled plan (the inspection step is done)."""

        steps = [self._finished(self._row.steps[0], "succeeded")] if self._row.steps else []
        previous = ""
        for index, spec in enumerate(plan.commands):
            label = plan.step_labels.get(index) or describe_command(spec)
            if label != previous or not self._command_step:
                steps.append(OperationStep(id=f"command-{index}", label=label))
            previous = label
            self._command_step[index] = len(steps) - 1
            self._step_commands.setdefault(len(steps) - 1, []).append(index)
        if plan.readiness is not None:
            label = plan.readiness.label
            steps.append(
                OperationStep(
                    id="readiness",
                    label=label[:1].upper() + label[1:]
                    if plan.readiness.capability == "installed"
                    else f"Wait for {label} to answer",
                )
            )
        if plan.after_ready or plan.after_ready_hook is not None:
            steps.append(OperationStep(id="after-ready", label="Prepare the model"))
        steps.append(OperationStep(id="finish", label="Record the deployment"))
        self.put(steps=steps, current_step=None)

    def _finished(self, step: OperationStep, state: StepState) -> OperationStep:
        index = next((i for i, row in enumerate(self._row.steps) if row.id == step.id), -1)
        started = self._step_started.pop(index, None)
        return step.model_copy(
            update={
                "state": state,
                "finished_at": _now(),
                "elapsed_seconds": round(time.monotonic() - started, 1)
                if started is not None
                else step.elapsed_seconds,
            }
        )

    def _set_step(self, index: int, state: StepState, **updates: Any) -> None:
        steps = list(self._row.steps)
        if not 0 <= index < len(steps):
            return
        if state == "running":
            if steps[index].state != "running":
                self._step_started[index] = time.monotonic()
                updates.setdefault("started_at", _now())
            steps[index] = steps[index].model_copy(update={"state": state, **updates})
        else:
            steps[index] = self._finished(steps[index], state).model_copy(update=updates)
        self._row = self._row.model_copy(update={"steps": steps})

    def enter(self, step_id: str, message: str = "") -> None:
        """Start a named phase step (``readiness``, ``after-ready``, ``finish``)."""

        index = next((i for i, row in enumerate(self._row.steps) if row.id == step_id), None)
        if index is None:
            return
        self._close_running(before=index)
        self._set_step(index, "running", message=message)
        self.put(current_step=index, **({"progress": message} if message else {}))

    def _close_running(self, *, before: int) -> None:
        for index, step in enumerate(self._row.steps[:before]):
            if step.state == "running":
                self._set_step(index, "succeeded")
            elif step.state == "pending":
                self._set_step(index, "skipped")

    def step_of(self, index: int) -> int | None:
        """The step a plan command belongs to."""

        return self._command_step.get(index)

    def add_secrets(self, *values: str | None) -> None:
        """Values never shown in the live log (a deployment key)."""

        self._secrets.update(value for value in values if value and len(value) >= 4)

    def command_started(self, index: int | None, spec: CommandSpec) -> None:
        """A command is about to run (``index`` None: one outside the plan's list)."""

        self._learn_secrets(spec)
        self._streamed = False
        self._partial.clear()
        step = self._command_step.get(index) if index is not None else None
        if step is None:
            return
        self._close_running(before=step)
        if self._row.steps[step].state != "running":
            self._set_step(step, "running")
            self.put(current_step=step, progress=self._row.steps[step].label)

    def command_skipped(self, index: int) -> None:
        """A plan command was skipped because a reuse preflight verified its result."""

        self._skipped.add(index)
        step = self._command_step.get(index)
        if step is None:
            return
        if all(command in self._skipped for command in self._step_commands.get(step, [])):
            reused = [row.message for row in self._row.reused if row.step == step]
            if self._row.steps[step].state not in {"reused", "succeeded"}:
                self._set_step(step, "reused", message=reused[-1] if reused else "Already present")
            self.put()

    def command_finished(
        self, index: int | None, result: CommandResult, *, ok: bool = True
    ) -> None:
        """A plan command returned (its output already streamed, or now from the result).

        ``ok`` is whether its exit code was allowed; a failed command leaves its
        step running for :meth:`finish` to mark failed.
        """

        for stream in tuple(self._partial):
            self._line(self._partial.pop(stream), stream)
        if not self._streamed:
            for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
                for line in text.splitlines():
                    self._line(line, stream)
        step = self._command_step.get(index) if index is not None else None
        if step is None:
            self._flush()
            return
        commands = self._step_commands.get(step, [])
        last = commands[-1] if commands else index
        if index == last and ok:
            state: StepState = "reused" if self._step_reused(step) else "succeeded"
            self._set_step(step, state)
        self._flush(force=index == last)

    def _step_reused(self, step: int) -> bool:
        return any(row.step == step for row in self._row.reused)

    # ---- output ----------------------------------------------------------

    def output(self, chunk: str, stream: str = "stdout") -> None:
        """Feed raw output as it arrives (any chunking; lines are reassembled)."""

        self._streamed = True
        text = self._partial.pop(stream, "") + chunk.replace("\r\n", "\n").replace("\r", "\n")
        *lines, rest = text.split("\n")
        if rest:
            self._partial[stream] = rest
        for line in lines:
            self._line(line, stream)
        self._flush()

    def log_text(self, text: str, stream: str = "log") -> None:
        """Feed whole lines read from a target log file (a supervised install)."""

        for line in text.splitlines():
            self._line(line, stream)
        self._flush()

    def redact(self, line: str) -> str:
        """``line`` as the live log may show it (see :func:`redact`)."""

        return redact(line, self._secrets)

    def _learn_secrets(self, spec: CommandSpec) -> None:
        if not spec.stdin:
            return
        try:
            body = json.loads(spec.stdin)
        except ValueError:
            body = None
        if isinstance(body, dict):
            self._secrets.update(
                str(value)
                for key, value in body.items()
                if key.casefold() in SENSITIVE_KEYS and isinstance(value, str) and len(value) >= 4
            )
            return
        # A one-line stdin is a secret handed to the launch (see secret_env).
        lines = spec.stdin.splitlines()
        if len(lines) == 1 and len(lines[0].strip()) >= 8:
            self._secrets.add(lines[0].strip())

    def _line(self, raw: str, stream: str) -> None:
        line = self.redact(raw.rstrip())
        if not line.strip():
            return
        step = self._row.current_step
        if line.lstrip().startswith(reuse_helper.MARKER):
            for found in reuse_helper.parse(line):
                self.reused(found, step=step)
            return
        meter = self._meters.setdefault(step if step is not None else -1, StepMeter())
        if meter.feed(line) and step is not None:
            steps = list(self._row.steps)
            if 0 <= step < len(steps):
                steps[step] = steps[step].model_copy(update={"progress": meter.measure()})
                self._row = self._row.model_copy(update={"steps": steps})
        if line.lstrip().startswith(reuse_helper.PROGRESS_MARKER):
            return
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " …"
        self.events.publish(
            self._row.id, LOG_EVENT, {"line": line, "stream": stream, "step": step}
        )

    def reused(self, found: reuse_helper.Reuse, *, step: int | None = None) -> None:
        """Record one verified reuse (deduplicated) and say so in the log."""

        message = found.message()
        if any(
            row.kind == found.kind and row.identity == found.identity for row in self._row.reused
        ):
            return
        report = ReuseReport(
            kind=found.kind,
            thing=found.thing,
            identity=found.identity,
            path=found.path,
            size_bytes=found.size_bytes,
            saved_seconds=found.saved_seconds,
            message=message,
            step=step,
        )
        self._row = self._row.model_copy(update={"reused": [*self._row.reused, report]})
        if step is not None and 0 <= step < len(self._row.steps):
            steps = list(self._row.steps)
            steps[step] = steps[step].model_copy(update={"message": message})
            self._row = self._row.model_copy(update={"steps": steps})
        self.events.publish(self._row.id, "operation.reuse", report.model_dump(mode="json"))
        self.events.publish(
            self._row.id, LOG_EVENT, {"line": message, "stream": "clio", "step": step}
        )
        self.put(progress=message)

    def message(self, text: str) -> None:
        """The operation's one-line progress text (and the running step's message)."""

        step = self._row.current_step
        if step is not None and 0 <= step < len(self._row.steps):
            steps = list(self._row.steps)
            steps[step] = steps[step].model_copy(update={"message": text})
            self._row = self._row.model_copy(update={"steps": steps})
        self._row = self._row.model_copy(update={"progress": text})
        self._flush()

    # ---- end -----------------------------------------------------------------

    def finish(self, state: str, progress: str, **updates: Any) -> InfrastructureOperation:
        """Settle the steps, persist the terminal record and close the live timeline."""

        for stream in tuple(self._partial):
            self._line(self._partial.pop(stream), stream)
        final: StepState = (
            "succeeded"
            if state == "succeeded"
            else "cancelled"
            if state == "cancelled"
            else "failed"
        )
        for index, step in enumerate(self._row.steps):
            if step.state == "running":
                self._set_step(index, final)
            elif step.state == "pending":
                self._set_step(index, "skipped")
        row = self.put(
            state=state,
            progress=progress,
            current_step=None,
            finished_at=_now(),
            **updates,
        )
        self.events.publish(row.id, TERMINAL_EVENT, row.model_dump(mode="json"))
        return row


__all__ = ["MAX_LINE_CHARS", "OperationTracker", "describe_command", "redact"]
