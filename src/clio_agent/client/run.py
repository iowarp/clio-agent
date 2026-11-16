"""Running an agent or a flow to completion: :class:`RunResult` and the run engine.

A run is one message to a session, followed on the session's event feed until the
turn that answers it completes. The feed is opened first and the message posted once
its preamble arrives, so no event of the run is missed. While it runs:

* child tasks (a flow's researchers, an agent's subagents) surface as
  :class:`TaskStarted` / :class:`TaskSettled` events, read from the server's task
  view on each ``task.updated`` hint;
* each plugin trusted for the run (recorded on the session by the server) is printed
  once to stderr.

The result reads the server's own records: the final message, the session's
artifacts (its tree's), its usage (``GET /v1/sessions/{sid}/usage``) and, for a run
that reached ``max_turns`` or ``max_budget_usd``, the limit it reached. A run that
fails returns ``status="failed"`` with the server's typed error; it does not raise.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from clio_agent.client.events import MessageCompleted, SessionSnapshot, StreamEvent
from clio_agent.client.types import ErrorInfo

if TYPE_CHECKING:
    from clio_agent.client.client import ClioClient

#: Task statuses after which a child task does not change again.
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled", "expired", "rejected"})
_CHILD_KINDS = frozenset({"Subagent", "Session"})


@dataclass(frozen=True)
class Artifact:
    """An artifact the run created or changed."""

    path: str
    artifact_id: str
    version: int
    name: str = ""
    workspace_id: str = ""


@dataclass
class RunResult:
    """What a run returns (and what a stream ends with)."""

    status: str
    text: str
    session_id: str
    artifacts: list[Artifact] = field(default_factory=list)
    error: ErrorInfo | None = None
    trusted: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class TaskStarted(StreamEvent):
    """A child task of the run started (derived from ``task.updated``)."""

    @property
    def handle(self) -> str:
        return str(self.payload.get("handle", ""))

    @property
    def label(self) -> str:
        return str(self.payload.get("label", ""))

    @property
    def child_session_id(self) -> str:
        return str(self.payload.get("child_session_id", ""))


class TaskSettled(TaskStarted):
    """A child task of the run settled; ``status`` is its terminal status."""

    @property
    def status(self) -> str:
        return str(self.payload.get("status", ""))


def message_parts(text: str, files: list[str] | None, workspace_root: str) -> list[dict]:
    """The prompt as message parts: the text plus one workspace-file reference per file."""

    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for path in files or []:
        relative = path
        if workspace_root and os.path.isabs(path):
            relative = os.path.relpath(path, workspace_root)
        parts.append(
            {
                "type": "context_ref",
                "ref_kind": "workspace_file",
                "ref_id": relative.replace(os.sep, "/"),
            }
        )
    return parts


class RunEngine:
    """Follows one session's runs; reused by a multi-turn session handle."""

    def __init__(self, client: ClioClient, session_id: str, workspace_root: str = "") -> None:
        self.client = client
        self.session_id = session_id
        self.workspace_root = workspace_root
        self.announced: set[str] = set()
        self._tasks: dict[str, str] = {}

    # -- the run ------------------------------------------------------------- #

    def stream(self, prompt: str, files: list[str] | None = None) -> Iterator[Any]:
        """Post ``prompt`` and yield the run's events, then its :class:`RunResult`."""

        parts = message_parts(prompt, files, self.workspace_root)
        ack_id = ""
        targets: set[str] = set()
        completed: MessageCompleted | None = None
        with self.client.sessions.events(self.session_id, reconnect_attempts=5) as feed:
            for event in feed:
                if not ack_id and isinstance(event, SessionSnapshot):
                    ack_id = self.client.messages.post(self.session_id, parts=parts).message_id
                    targets = {ack_id}
                    continue
                if not ack_id:
                    continue
                yield event
                if event.type == "task.updated":
                    yield from self._task_events(event)
                elif event.type == "session.updated":
                    self._announce_trusted(event.payload.get("metadata") or {})
                elif isinstance(event, MessageCompleted):
                    identity = str(event.payload.get("turn_id") or "")
                    if identity not in targets:
                        targets |= self._recipient_turn(ack_id)
                    if identity in targets:
                        completed = event
                        break
        yield self.result(completed)

    def run(self, prompt: str, files: list[str] | None = None) -> RunResult:
        """Run ``prompt`` to completion and return its result."""

        result: Any = None
        for item in self.stream(prompt, files):
            result = item
        return result

    # -- pieces -------------------------------------------------------------- #

    def _recipient_turn(self, ack_id: str) -> set[str]:
        """The turn a message was delivered into, when it is not its own id."""

        submitted = self.client.messages.get(self.session_id, ack_id)
        receipt = (submitted.metadata or {}).get("recipient_input") or {}
        turn = str(receipt.get("turn_id") or "") if isinstance(receipt, dict) else ""
        return {turn} if turn else set()

    def _task_events(self, hint: StreamEvent) -> Iterator[StreamEvent]:
        handle = str(hint.payload.get("handle") or "")
        if str(hint.payload.get("kind") or "") not in _CHILD_KINDS or not handle:
            return
        view = self.client._request("GET", f"/v1/agent-tasks/{handle}").json()
        if view.get("child_session_id") == self.session_id:
            return  # the run's own assignment, not a child
        status = str(view.get("status") or "")
        previous = self._tasks.get(handle)
        self._tasks[handle] = status
        payload = {
            "handle": handle,
            "status": status,
            "label": view.get("run_label") or view.get("label") or "",
            "child_session_id": view.get("child_session_id") or "",
            "error_reason": view.get("error_reason"),
        }
        if previous is None:
            yield TaskStarted(id=hint.id, type="task.started", payload=payload)
        if status in TERMINAL_TASK_STATUSES and previous not in TERMINAL_TASK_STATUSES:
            yield TaskSettled(id=hint.id, type="task.settled", payload=payload)

    def _announce_trusted(self, metadata: dict[str, Any]) -> list[str]:
        from clio_agent.client.connection import print_trusted  # noqa: PLC0415

        names = [str(row.get("plugin")) for row in metadata.get("unattended_trusted") or []]
        for name in names:
            if name not in self.announced and self.client.options.banner:
                print_trusted(name)
            self.announced.add(name)
        return names

    def result(self, completed: MessageCompleted | None) -> RunResult:
        """The result from the server's records once the turn completed (or the feed ended)."""

        session = self.client.sessions.get(self.session_id)
        trusted = self._announce_trusted(session.metadata)
        error: ErrorInfo | None = None
        text = ""
        if completed is None:
            error = ErrorInfo(
                error="stream_ended",
                message="The event stream ended before the run's turn completed.",
            )
        else:
            message = self.client.messages.get(self.session_id, completed.message_id)
            text = message.text()
            error = message.error_info or (
                completed.error_info if hasattr(completed, "error_info") else None
            )
        reached = (session.metadata or {}).get("run_limit_reached")
        if isinstance(reached, dict) and reached.get("limit"):
            error = ErrorInfo(
                error="run_limit_reached",
                message=f"The run reached its {reached['limit']} limit ({reached.get('value')}).",
                details=dict(reached),
            )
        status = "completed"
        if error is not None:
            status = "cancelled" if error.error in {"cancelled", "turn_cancelled"} else "failed"
        return RunResult(
            status=status,
            text=text,
            session_id=self.session_id,
            artifacts=self.artifacts(),
            error=error,
            trusted=trusted,
            usage=self.client.sessions.usage(self.session_id),
        )

    def artifacts(self) -> list[Artifact]:
        """The artifacts the session's tree produced, newest first."""

        body = self.client._request(
            "GET",
            f"/v1/sessions/{self.session_id}/artifacts",
            params={"include_children": "true"},
        ).json()
        found: list[Artifact] = []
        for row in body.get("artifacts", []):
            if not _written_by_run(row):
                continue  # an input the run read (``used-*``), not one it created or changed
            name = str(row.get("name") or "")
            head = (row.get("versions") or [{}])[-1]
            root = self.workspace_root
            # The version records where the file is; the record's name is only its label.
            fallback = os.path.join(root, name) if root and not os.path.isabs(name) else name
            found.append(
                Artifact(
                    path=str(head.get("path") or fallback),
                    artifact_id=str(row.get("head_artifact_id") or ""),
                    version=int(row.get("latest_version") or 0),
                    name=name,
                    workspace_id=str(row.get("workspace_id") or ""),
                )
            )
        return found


def _written_by_run(row: dict[str, Any]) -> bool:
    """Whether one of the run's sessions wrote a version of this record.

    The listing also carries records the run only used (a file it read becomes a
    ``used-script`` / ``used-granted-file`` version); those are not its artifacts.
    """

    sessions = set(row.get("producing_session_ids") or [])
    for version in row.get("versions") or []:
        producer = version.get("producer") or {}
        written = not str(producer.get("designation") or "").startswith("used-")
        if written and (not sessions or producer.get("session_id") in sessions):
            return True
    return False


__all__ = [
    "Artifact",
    "RunEngine",
    "RunResult",
    "TERMINAL_TASK_STATUSES",
    "TaskSettled",
    "TaskStarted",
    "message_parts",
]
