"""Detached expert invocation over a clio-core context store (epic #667, #659 — d).

The request and result cross through clio-core **context blobs**, not in-memory JSON:
the parent submits an :class:`ExpertRequest` to a shared store and waits; a worker —
possibly a *second clio on the same machine* — reads it from the store, runs the
child, and publishes the :class:`ExpertResult` back. The two parties share **only the
store**, so this is genuine clio-to-clio handoff.

Single box today (both parties attach the same in-process clio-core runtime, or a
LocalFS store for tests); on a cluster the same store spans nodes (#659) and the
identical code is cross-machine. This is the real transport the
:class:`LoopbackExpertInvoker` stood in for — same `ExpertInvoker` contract, so it
also composes with the background monitor/wait_for primitive (`spawn_invocation`).

Principle (CLAUDE.md): the store carries the parent's request and the child's
result/events; no clio-side routing/completion heuristic — a detached child stays
parent-driven.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable, Optional

from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult

# The mailbox rides ARC's "context" record kind (a valid ARC kind on every backend);
# names are namespaced so they never collide with real context records.
_KIND = "context"
_PREFIX = "cee_"

Handler = Callable[[ExpertRequest], Awaitable[ExpertResult]]


class CEEMailbox:
    """A request/result mailbox over any clio-core context store (an ``ARCStore``:
    CTE for real clio-core, LocalFS for a single box / tests). Parties communicate
    only by reading/writing blobs here."""

    def __init__(self, store: Any, *, prefix: str = _PREFIX) -> None:
        self._store = store
        self._prefix = prefix

    def submit(self, request: ExpertRequest) -> str:
        """Parent: place a request in the mailbox, return its id."""
        rid = f"{self._prefix}{uuid.uuid4().hex[:12]}"
        self._store.put(_KIND, f"{rid}.req", json.dumps(request.to_wire()).encode("utf-8"))
        return rid

    def read_request(self, rid: str) -> Optional[ExpertRequest]:
        data = self._store.get(_KIND, f"{rid}.req")
        return ExpertRequest.from_wire(json.loads(data)) if data else None

    def publish_result(self, rid: str, result: ExpertResult) -> None:
        """Worker: write the child's result back to the mailbox."""
        self._store.put(_KIND, f"{rid}.res", json.dumps(result.to_wire()).encode("utf-8"))

    def read_result(self, rid: str) -> Optional[ExpertResult]:
        data = self._store.get(_KIND, f"{rid}.res")
        return ExpertResult.from_wire(json.loads(data)) if data else None

    def has_result(self, rid: str) -> bool:
        return self._store.exists(_KIND, f"{rid}.res")

    def pending(self) -> list[str]:
        """Request ids awaiting a result (the worker's queue) — discovered from the
        store, so a worker that never saw the request in memory still finds it."""
        out: list[str] = []
        for name, _ in self._store.scan(_KIND, self._prefix):
            if name.endswith(".req"):
                rid = name[:-4]
                if not self.has_result(rid):
                    out.append(rid)
        return out


async def serve_one(mailbox: CEEMailbox, rid: str, handler: Handler) -> Optional[ExpertResult]:
    """Worker side: read the request from clio-core, run the child, publish the
    result back. Returns the result (or None if the request vanished)."""
    req = mailbox.read_request(rid)
    if req is None:
        return None
    result = await handler(req)
    mailbox.publish_result(rid, result)
    return result


async def run_worker(
    mailbox: CEEMailbox,
    handler: Handler,
    *,
    stop: asyncio.Event,
    poll: float = 0.01,
) -> None:
    """A worker loop draining the mailbox until ``stop`` is set — the 'second clio on
    the machine'. Each pending request is served (child run, result published)."""
    while not stop.is_set():
        for rid in mailbox.pending():
            await serve_one(mailbox, rid, handler)
        await asyncio.sleep(poll)


class CEEExpertInvoker:
    """Parent-side ``ExpertInvoker`` over the clio-core mailbox: submit the request,
    wait for a worker to publish the result. Drop-in for the loopback invoker, but
    the transport is real clio-core context."""

    def __init__(self, mailbox: CEEMailbox, *, timeout: float = 60.0, poll: float = 0.01) -> None:
        self._mb = mailbox
        self._timeout = timeout
        self._poll = poll

    async def invoke(self, request: ExpertRequest) -> ExpertResult:
        rid = self._mb.submit(request)

        async def _wait() -> None:
            while not self._mb.has_result(rid):
                await asyncio.sleep(self._poll)

        await asyncio.wait_for(_wait(), timeout=self._timeout)
        result = self._mb.read_result(rid)
        if result is None:  # pragma: no cover — has_result was true
            raise RuntimeError(f"clio-core mailbox result vanished for {rid}")
        return result
