"""Detached expert invocation over a clio-core context store (epic #667, #659 — d).

The request and result cross through clio-core **context blobs**, not in-memory JSON:
the parent submits an :class:`ExpertRequest` to a shared store and waits; a worker —
possibly a *second clio on the same machine* — reads it from the store, runs the
child, and publishes the :class:`ExpertResult` back. The two parties share **only the
store**, so this is genuine clio-to-clio handoff.

Single box today (both parties attach the same in-process clio-core runtime, or a
LocalFS store for tests); on a cluster the same store spans nodes (#659) and the
identical code is cross-machine. Drop-in for :class:`LoopbackExpertInvoker` (same
``ExpertInvoker`` contract), so it also composes with the background monitor
(``spawn_invocation``).

Delivery semantics: a published result is exactly-once (``publish_result`` overwrites
and ``pending`` drops answered ids), but *execution* is **at-least-once** — without a
compare-and-set/lease primitive in the store, two racing workers can both run the same
request. ``claim`` makes that rare; true exactly-once needs a clio-core lease (cluster
#659). A failing or poison child is drained as a ``status="failed"`` result rather than
hanging the parent or killing the worker.

Principle (CLAUDE.md): the store carries the parent's request and the child's
result/events; no clio-side routing/completion heuristic — a detached child stays
parent-driven.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult

# The mailbox rides ARC's "context" record kind (valid on every backend); names are
# namespaced so they never collide with real context records.
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

    def has_request(self, rid: str) -> bool:
        return self._store.exists(_KIND, f"{rid}.req")

    def read_request(self, rid: str) -> Optional[ExpertRequest]:
        """Decode the request, or ``None`` if absent **or corrupted** (a poison blob
        must not crash the worker — the caller drains it as failed)."""
        data = self._store.get(_KIND, f"{rid}.req")
        if not data:
            return None
        try:
            return ExpertRequest.from_wire(json.loads(data))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            return None

    def publish_result(self, rid: str, result: ExpertResult) -> None:
        """Worker: write the child's result back to the mailbox."""
        self._store.put(_KIND, f"{rid}.res", json.dumps(result.to_wire()).encode("utf-8"))

    def read_result(self, rid: str) -> Optional[ExpertResult]:
        data = self._store.get(_KIND, f"{rid}.res")
        if not data:
            return None
        try:
            return ExpertResult.from_wire(json.loads(data))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            return None

    def has_result(self, rid: str) -> bool:
        return self._store.exists(_KIND, f"{rid}.res")

    def _read_claim(self, rid: str) -> tuple[Optional[str], float]:
        data = self._store.get(_KIND, f"{rid}.claim")
        if not data:
            return None, 0.0
        try:
            tok, ts = data.decode("utf-8").rsplit("|", 1)
            return tok, float(ts)
        except (ValueError, UnicodeDecodeError):
            return None, 0.0

    def claim(self, rid: str, token: str, *, ttl: float = 6.0, now: Optional[float] = None) -> bool:
        """TTL-gated lease claim. Wins only if no LIVE lease is held by another worker
        (the existing lease is absent, expired, or ours). A live worker renews via
        :meth:`renew` so its lease never expires; a crashed worker stops renewing and
        the lease frees in ~``ttl`` for reclaim. This is what stops a slow-but-alive
        worker from being double-executed while still reclaiming a dead one.

        NOT atomic (the store has no CAS), so a sub-millisecond *simultaneous* claim on
        a fresh request can still double-serve — true exactly-once needs a clio-core
        lease primitive (#659). Skips a request whose ``.req`` is already gone."""
        if not self.has_request(rid):
            return False
        now = time.time() if now is None else now
        holder, ts = self._read_claim(rid)
        if holder is not None and holder != token and (now - ts) < ttl:
            return False  # a live worker holds the lease
        self._store.put(_KIND, f"{rid}.claim", f"{token}|{now}".encode("utf-8"))
        holder2, _ = self._read_claim(rid)
        return holder2 == token

    def renew(self, rid: str, token: str, now: Optional[float] = None) -> None:
        """Refresh our lease timestamp (heartbeat) so a long handler isn't reclaimed."""
        holder, _ = self._read_claim(rid)
        if holder == token:
            stamp = time.time() if now is None else now
            self._store.put(_KIND, f"{rid}.claim", f"{token}|{stamp}".encode("utf-8"))

    def discard_claim(self, rid: str) -> None:
        """Drop a stray claim blob (e.g. one written just as the request was discarded)."""
        self._store.delete(_KIND, f"{rid}.claim")

    def discard(self, rid: str) -> None:
        """Remove a request and any result/claim — orphan cleanup (e.g. on timeout)."""
        for suffix in (".req", ".res", ".claim"):
            self._store.delete(_KIND, f"{rid}{suffix}")

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
    result back — ALWAYS publishing something terminal so the parent never hangs.

    * request absent → ``None`` (nothing to do).
    * request corrupted → publish a ``failed`` result (drains the poison blob).
    * handler raises → publish a ``failed`` result carrying the error.
    Cancellation propagates (cooperative shutdown).
    """
    if not mailbox.has_request(rid):
        # The request was completed + discarded by the parent between this worker's
        # pending() snapshot and now; drop any stray claim we wrote so it doesn't leak.
        mailbox.discard_claim(rid)
        return None
    req = mailbox.read_request(rid)
    if req is None:  # blob present but undecodable
        failed = ExpertResult(expert_id="", status="failed", error="corrupted_request")
        mailbox.publish_result(rid, failed)
        return failed
    try:
        result = await handler(req)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — recorded as a failed result, not raised
        result = ExpertResult(
            expert_id=req.expert_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    mailbox.publish_result(rid, result)
    return result


async def run_worker(
    mailbox: CEEMailbox,
    handler: Handler,
    *,
    stop: asyncio.Event,
    poll: float = 0.1,
    worker_id: str = "",
    lease_ttl: float = 6.0,
) -> None:
    """A worker loop draining the mailbox until ``stop`` is set — the 'second clio on
    the machine'. Each request is served under a renewed lease (``lease_ttl``) so a
    long handler isn't reclaimed and a crashed worker's lease frees fast. One
    failing/poison request can never kill the loop."""
    token = worker_id or uuid.uuid4().hex[:8]
    while not stop.is_set():
        for rid in mailbox.pending():
            if not mailbox.claim(rid, token, ttl=lease_ttl):
                continue  # a live worker holds the lease
            await _serve_under_lease(mailbox, rid, token, handler, lease_ttl)
        await asyncio.sleep(poll)


async def _serve_under_lease(
    mailbox: CEEMailbox, rid: str, token: str, handler: Handler, lease_ttl: float
) -> None:
    """Serve one request while renewing its lease, so a long handler isn't reclaimed.
    A crashed worker stops renewing and the lease frees in ~lease_ttl for reclaim."""
    hb_stop = asyncio.Event()

    async def _heartbeat() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while not hb_stop.is_set():
                await asyncio.sleep(max(lease_ttl / 3.0, 0.05))
                if not hb_stop.is_set():
                    mailbox.renew(rid, token)

    hb = asyncio.ensure_future(_heartbeat())
    try:
        await serve_one(mailbox, rid, handler)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — contain; never let one rid kill the loop
        mailbox.publish_result(
            rid, ExpertResult(expert_id="", status="failed", error=f"worker_error: {exc}")
        )
    finally:
        hb_stop.set()
        hb.cancel()
        with contextlib.suppress(BaseException):
            await hb


class CEEExpertInvoker:
    """Parent-side ``ExpertInvoker`` over the clio-core mailbox: submit the request,
    wait for a worker to publish the result. Drop-in for the loopback invoker, but the
    transport is real clio-core context. On timeout the orphaned request is discarded
    and a clear error is raised (the parent's settle loop treats it like any failed
    child — it stays the router)."""

    def __init__(self, mailbox: CEEMailbox, *, timeout: float = 60.0, poll: float = 0.1) -> None:
        self._mb = mailbox
        self._timeout = timeout
        self._poll = poll

    async def invoke(self, request: ExpertRequest) -> ExpertResult:
        rid = self._mb.submit(request)

        async def _wait() -> None:
            while not self._mb.has_result(rid):
                await asyncio.sleep(self._poll)

        try:
            await asyncio.wait_for(_wait(), timeout=self._timeout)
        except (asyncio.TimeoutError, TimeoutError):
            self._mb.discard(rid)  # don't leak the orphan request blob
            raise TimeoutError(
                f"clio-core delegation {rid} timed out after {self._timeout}s"
            ) from None
        result = self._mb.read_result(rid)
        # Delegation complete: drop req/res/claim so the mailbox doesn't grow without
        # bound over many delegations, and a late (double-executing) worker can't
        # re-serve a request whose result the parent already consumed.
        self._mb.discard(rid)
        if result is None:  # pragma: no cover — has_result was true
            raise RuntimeError(f"clio-core mailbox result vanished for {rid}")
        return result
