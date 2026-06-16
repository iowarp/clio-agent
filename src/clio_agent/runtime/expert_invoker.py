"""Transport-abstracted expert invocation (epic #667, issue #671) — the hinge.

An expert delegation is expressed as a serializable :class:`ExpertRequest` and
produces a serializable :class:`ExpertResult` (carrying SemanticEvent-shaped
frames). Invokers differ only in transport:

  * :class:`InProcessExpertInvoker` — calls a local async handler directly
    (parity with today's in-process delegation).
  * :class:`LoopbackExpertInvoker` — round-trips the request and result through a
    JSON wire boundary and folds them back, proving the contract is
    serialization-clean (the detached seam) WITHOUT a cluster.

On a GPU cluster the loopback's ``json`` round-trip is swapped for clio-core's
Context Transport Primitives (#659) with no orchestration change. The live fold
already accepts raw ``SemanticEvent``s from any producer (``arc/live.py``), so a
detached child's events fold into the parent context the same as a local one.

Principle (CLAUDE.md): the boundary carries the parent's decisions (the request)
and the child's results/events; it introduces no clio-side routing/completion
heuristic. A detached expert stays parent-driven.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from clio_agent.runtime.background_tasks import BackgroundTasks


@dataclass
class ExpertEvent:
    """A child-produced frame (thought / tool_call / observation / status / ...).
    ``payload`` must be JSON-serializable so it survives a detached transport."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExpertRequest:
    """What the parent hands a child expert. Everything is JSON-serializable: the
    compiled context travels IN the request, not via shared process state."""

    expert_id: str
    question: str
    session_id: str = ""
    scope: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "question": self.question,
            "session_id": self.session_id,
            "scope": self.scope,
            "context": dict(self.context),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "ExpertRequest":
        return cls(
            expert_id=d["expert_id"],
            question=d["question"],
            session_id=d.get("session_id", ""),
            scope=d.get("scope", ""),
            context=dict(d.get("context") or {}),
        )


@dataclass
class ExpertResult:
    """What a child expert returns. ``events`` fold into the parent's context."""

    expert_id: str
    answer: str = ""
    status: str = "completed"  # completed | failed | cancelled
    error: Optional[str] = None
    events: list[ExpertEvent] = field(default_factory=list)
    workflow_state: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "answer": self.answer,
            "status": self.status,
            "error": self.error,
            "events": [{"kind": e.kind, "payload": dict(e.payload)} for e in self.events],
            "workflow_state": dict(self.workflow_state),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "ExpertResult":
        return cls(
            expert_id=d["expert_id"],
            answer=d.get("answer", ""),
            status=d.get("status", "completed"),
            error=d.get("error"),
            events=[
                ExpertEvent(kind=e["kind"], payload=dict(e.get("payload") or {}))
                for e in d.get("events") or []
            ],
            workflow_state=dict(d.get("workflow_state") or {}),
        )


# A handler runs the actual expert (local DSPy module today, a remote worker later).
Handler = Callable[[ExpertRequest], Awaitable[ExpertResult]]


class ExpertInvoker(Protocol):
    """The seam: one operation, transport-agnostic."""

    async def invoke(self, request: ExpertRequest) -> ExpertResult: ...


class InProcessExpertInvoker:
    """Calls a local async handler directly — parity with the in-process path."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler

    async def invoke(self, request: ExpertRequest) -> ExpertResult:
        return await self._handler(request)


class LoopbackExpertInvoker:
    """Same handler, but the request and result cross a JSON wire boundary.

    This proves the contract is serialization-clean end to end: a non-serializable
    field in the request or result raises here, exactly as it would over a real
    detached transport. Swap the ``json`` round-trip for clio-core transport (#659)
    on a cluster and the orchestration is unchanged.
    """

    def __init__(self, handler: Handler) -> None:
        self._handler = handler

    async def invoke(self, request: ExpertRequest) -> ExpertResult:
        wire_req = json.loads(json.dumps(request.to_wire()))  # OUT, then IN on the remote side
        result = await self._handler(ExpertRequest.from_wire(wire_req))
        wire_res = json.loads(json.dumps(result.to_wire()))  # result back to the parent side
        return ExpertResult.from_wire(wire_res)


def spawn_invocation(
    tasks: BackgroundTasks,
    invoker: ExpertInvoker,
    request: ExpertRequest,
    *,
    label: str = "",
) -> str:
    """Run an expert invocation as a monitored background task — where (b) async,
    (c) monitor/wait_for, and (e) the invoker compose. Returns the task handle id;
    poll/wait/cancel it via the :class:`BackgroundTasks` registry. The child's
    events surface as incremental output lines for a status poll."""

    async def work(sink) -> ExpertResult:
        result = await invoker.invoke(request)
        for ev in result.events:
            sink.emit(ev.kind)
        return result

    return tasks.spawn(work, label=label or request.expert_id)
