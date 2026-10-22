"""A flow run's durable record: workflow checkpoints in task history.

A run is identified by its owning assignment and a run invocation. Its content
maps every ``ctx`` invocation id to what the task layer accepted for it (the
handle) and, once settled, how it ended; ``ctx.step`` results and plan edits
are recorded the same way. Each change is confirmed as a
:class:`~clio_agent.arc.task_types.WorkflowCheckpoint` under the owning
assignment, and the assignment's details point at the latest checkpoint.
Nothing else stores flow state.

On restart the runtime re-runs ``forward()`` from the top and each call looks
itself up here: a recorded handle is re-attached, a recorded step result is
returned, and only work that never reached acceptance is submitted.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from typing import Any
from uuid import uuid4

from clio_agent.flows.outcomes import FlowCheckpointLimit, FlowReplayMismatch

#: One run may record at most this many entries (calls, steps, questions, edits).
MAX_ENTRIES = 10_000


def digest(value: Any) -> str:
    """A stable digest of JSON-able data (sorted keys, repr for the rest)."""

    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class FlowJournal:
    """The checkpointed state of one flow run, opened lazily by ``ctx``."""

    def __init__(
        self,
        app: Any,
        *,
        session_id: str,
        root_handle: str,
        run_invocation: str,
        flow_id: str,
        declaration: str,
        request: Any,
        depth: int,
    ) -> None:
        self.app = app
        self.history = app.state.task_history
        self.session_id = session_id
        self.root_handle = root_handle
        self.run_invocation = run_invocation
        self.flow_id = flow_id
        self.declaration = declaration
        self.request_digest = digest({"request": request})
        self.run_id = "flowrun_" + digest([root_handle, run_invocation])[:24]
        self.content: dict[str, Any] = {
            "id": self.run_id,
            "run_id": self.run_id,
            "root_session": session_id,
            "flow": flow_id,
            "root_handle": root_handle,
            "invocation_id": run_invocation,
            "declaration": declaration,
            "request": request if _jsonable(request) else repr(request),
            "depth": depth,
            "status": "running",
            "entries": 0,
            "calls": {},
            "steps": {},
            "events": {},
            "result": None,
        }
        self.replayed = False
        #: ``(root_handle, run_invocation)`` of the run a restart continues: its completed
        #: calls and steps carry over, so only what did not complete runs again.
        self.resume_from: tuple[str, str] | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """Load an existing run (replay) or confirm a new one."""

        existing = await self.history.io.run(self._load)
        if existing is not None:
            if (
                existing.get("declaration") != self.declaration
                or existing.get("request_digest", self.request_digest) != self.request_digest
            ):
                raise FlowReplayMismatch(
                    f"flow run {self.run_id} was recorded for different code or input",
                    run_id=self.run_id,
                )
            self.content = existing
            self.replayed = True
            return
        self.content["request_digest"] = self.request_digest
        if self.resume_from is not None:
            previous = await self.history.io.run(self._load, *self.resume_from)
            if previous is not None:
                self._carry_over(previous)
        await self.save()

    def _carry_over(self, previous: dict[str, Any]) -> None:
        """Keep the previous run's completed work under this run's invocations."""

        old, new = str(previous.get("run_id") or ""), self.run_id
        rename = {old + ":": new + ":"} if old else {}

        def key(invocation: str) -> str:
            for prefix, replacement in rename.items():
                if invocation.startswith(prefix):
                    return replacement + invocation[len(prefix) :]
            return invocation

        calls = {
            key(invocation): row
            for invocation, row in (previous.get("calls") or {}).items()
            if row.get("status") == "completed"
        }
        steps = {key(invocation): row for invocation, row in (previous.get("steps") or {}).items()}
        self.content["calls"].update(calls)
        self.content["steps"].update(steps)
        self.content["entries"] = len(calls) + len(steps)
        self.content["resumed_from"] = {
            "run_id": old,
            "root_handle": self.resume_from[0] if self.resume_from else "",
            "calls": len(calls),
            "steps": len(steps),
        }

    def _load(self, root_handle: str = "", run_invocation: str = "") -> dict[str, Any] | None:
        root_handle, run_invocation = (
            root_handle or self.root_handle,
            run_invocation or self.run_invocation,
        )
        with self.history.serialized():
            checkpoint = self.history.workflow_checkpoint(root_handle, run_invocation)
            if checkpoint is None:
                return None
            value = self.history.read_content(checkpoint.content)
            return dict(value) if isinstance(value, dict) else None

    # ------------------------------------------------------------- entries
    def call(self, invocation: str) -> dict[str, Any] | None:
        return self.content["calls"].get(invocation)

    def step(self, invocation: str) -> dict[str, Any] | None:
        return self.content["steps"].get(invocation)

    async def record_call(self, invocation: str, row: dict[str, Any]) -> None:
        self._count(invocation not in self.content["calls"])
        self.content["calls"][invocation] = row
        await self.save()

    async def record_step(self, invocation: str, row: dict[str, Any]) -> None:
        self._count(invocation not in self.content["steps"])
        self.content["steps"][invocation] = row
        await self.save()

    async def record_event(self, key: str, event: dict[str, Any]) -> None:
        if key in self.content["events"]:
            return
        self._count(True)
        self.content["events"][key] = event
        await self.save()

    async def publish(self, view: dict[str, Any]) -> None:
        """Replace the run's live view; it is overwritten, so it is not an entry."""

        if self.content.get("view") == view:
            return
        self.content["view"] = view
        await self.save()

    async def finish(self, status: str, result: Any = None) -> None:
        self.content["status"] = status
        self.content["result"] = result if _jsonable(result) else repr(result)
        await self.save()

    def _count(self, new: bool) -> None:
        if not new:
            return
        if self.content["entries"] >= MAX_ENTRIES:
            raise FlowCheckpointLimit(
                f"flow run {self.run_id} reached its {MAX_ENTRIES}-entry checkpoint limit",
                run_id=self.run_id,
                entries=self.content["entries"],
            )
        self.content["entries"] += 1

    # ---------------------------------------------------------------- save
    async def save(self) -> None:
        async with self._lock:
            snapshot = copy.deepcopy(self.content)
            await self.history.io.run(self._save, snapshot)

    def _save(self, content: dict[str, Any]) -> None:
        from clio_agent.arc.task_types import (  # noqa: PLC0415
            AssignmentDetails,
            WorkflowCheckpoint,
            argument_digest,
        )
        from clio_agent.gact.native_work import now  # noqa: PLC0415

        history = self.history
        with history.serialized():
            owner = history.task_snapshot(self.root_handle)
            if owner is None:
                raise ValueError("The flow's owning assignment is unavailable")
            reference = history.write_content(self.session_id, content)
            history.record_workflow(
                WorkflowCheckpoint(
                    root_handle=self.root_handle,
                    session_id=self.session_id,
                    invocation_id=self.run_invocation,
                    execution_id=self.run_id,
                    declaration=self.declaration,
                    request_digest=argument_digest({"request": content.get("request")}),
                    content=reference,
                )
            )
            if owner.details is not None:
                details = AssignmentDetails.model_validate(history.read_content(owner.details))
                pointer = history.write_content(
                    self.session_id,
                    details.model_copy(
                        update={"workflow": reference, "workflow_id": self.run_id}
                    ).model_dump(mode="json"),
                )
                history.record_snapshot(
                    owner.model_copy(update={"details": pointer, "updated_at": now()}),
                    observation_id=uuid4().hex,
                )
            history.workflow_reference_repaired(self.root_handle)


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def repair_run_reference(app: Any, root_handle: str) -> None:
    """Point an owning assignment at its latest flow checkpoint again (startup recovery).

    A crash between confirming a checkpoint and updating the owner's details leaves
    the owner's projection stale; this repairs only the projection, from the
    already confirmed checkpoint, and never re-executes anything.
    """

    from clio_agent.arc.task_types import AssignmentDetails  # noqa: PLC0415
    from clio_agent.gact.native_work import now  # noqa: PLC0415

    history = app.state.task_history
    with history.serialized():
        root = history.task_snapshot(root_handle)
        checkpoint = history.workflow_checkpoint(root_handle)
        if root is None or checkpoint is None or root.details is None:
            history.workflow_reference_repaired(root_handle)
            return
        details = AssignmentDetails.model_validate(history.read_content(root.details))
        if details.workflow != checkpoint.content:
            reference = history.write_content(
                root.owner.session_id,
                details.model_copy(
                    update={"workflow": checkpoint.content, "workflow_id": checkpoint.execution_id}
                ).model_dump(mode="json"),
            )
            history.record_snapshot(
                root.model_copy(update={"details": reference, "updated_at": now()}),
                observation_id=uuid4().hex,
            )
        history.workflow_reference_repaired(root_handle)
