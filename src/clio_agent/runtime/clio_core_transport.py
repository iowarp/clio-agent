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
_PREFIX = "clio_core_"

Handler = Callable[[ExpertRequest], Awaitable[ExpertResult]]


class ClioCoreMailbox:
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
        if holder is None:
            # FRESH request: atomically create the claim. On a store with an atomic
            # put_if_absent (LocalFS O_EXCL) two workers racing a brand-new request can no
            # longer BOTH win — exactly-once. (CTE is best-effort until clio-core#559's
            # CAS lands; this still narrows the window.)
            if self._store.put_if_absent(_KIND, f"{rid}.claim", f"{token}|{now}".encode("utf-8")):
                return True
            holder, ts = self._read_claim(rid)  # lost the create race — the winner holds it
            if holder is None:
                # The .claim blob exists but is empty/torn (a husk from an interrupted write,
                # e.g. a worker killed between create and write), NOT a live lease. Overwrite
                # it to take the claim rather than dead-end forever — the TTL-reclaim path
                # below can never run while holder stays None.
                self._store.put(_KIND, f"{rid}.claim", f"{token}|{now}".encode("utf-8"))
                holder2, _ = self._read_claim(rid)
                return holder2 == token
        if holder != token and (now - ts) < ttl:
            return False  # a live worker holds the lease
        # An EXISTING claim that is ours, or another's that has EXPIRED -> (re)take it. This
        # reclaim overwrite stays non-atomic (needs the CAS), but it is far rarer than the
        # fresh-claim race and bounded by the TTL.
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


async def serve_one(mailbox: ClioCoreMailbox, rid: str, handler: Handler) -> Optional[ExpertResult]:
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
    # The parent deletes .req when it consumes a result. If it's gone now, another worker
    # already answered (claim race) and the parent moved on — publishing here would orphan
    # a late .res blob (a real leak under sustained concurrency). Skip it; clean our claim.
    if not mailbox.has_request(rid):
        mailbox.discard_claim(rid)
        return None
    mailbox.publish_result(rid, result)
    return result


async def run_worker(
    mailbox: ClioCoreMailbox,
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
    mailbox: ClioCoreMailbox, rid: str, token: str, handler: Handler, lease_ttl: float
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


class ClioCoreExpertInvoker:
    """Parent-side ``ExpertInvoker`` over the clio-core mailbox: submit the request,
    wait for a worker to publish the result. Drop-in for the loopback invoker, but the
    transport is real clio-core context. On timeout the orphaned request is discarded
    and a clear error is raised (the parent's settle loop treats it like any failed
    child — it stays the router)."""

    def __init__(self, mailbox: ClioCoreMailbox, *, timeout: float = 60.0, poll: float = 0.1) -> None:
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
        except asyncio.CancelledError:
            # The caller cancelled this delegation (e.g. a cancelled spawn_invocation).
            # Discard the request so it doesn't orphan in the mailbox AND so a worker can't
            # later pick it up, run the child (wasted compute), and publish an unconsumed
            # result. serve_one re-checks .req before publishing, so a worker mid-flight
            # drops its claim cleanly. Then let cancellation propagate.
            self._mb.discard(rid)
            raise
        result = self._mb.read_result(rid)
        # Delegation complete: drop req/res/claim so the mailbox doesn't grow without
        # bound over many delegations, and a late (double-executing) worker can't
        # re-serve a request whose result the parent already consumed.
        self._mb.discard(rid)
        if result is None:  # pragma: no cover — has_result was true
            raise RuntimeError(f"clio-core mailbox result vanished for {rid}")
        return result


# ---------------------------------------------------------------------------------------
# Isolated per-worker queues (the lease-free model — clio-core#559, per Luke Logan).
#
# The pull model above gives every worker ONE shared role queue, so workers race to claim
# each request — which needs the (non-atomic, ~0.27%-double-exec) ``claim`` lease. Luke's
# guidance: isolate requests to workers instead. Here the parent routes each request to ONE
# worker's PRIVATE queue, so that worker is the SOLE reader — no race, no claim, no lease,
# and execution is exactly-once BY CONSTRUCTION (CTE stays a simple mutable KV store; no new
# primitive needed). Resilience: the parent already waits for the result, so on timeout it
# re-routes to another LIVE worker. Load: the parent round-robins over a worker-presence list
# kept OFF the request hot path (workers heartbeat a presence blob). Built additively — the
# pull model is untouched; this is the recommended path going forward.
# ---------------------------------------------------------------------------------------


def _worker_queue_prefix(role: str, worker_id: str, *, prefix: str = _PREFIX) -> str:
    """The mailbox prefix for ONE worker's private queue (only that worker drains it)."""
    return f"{prefix}wq.{role}.{worker_id}."


def _presence_name(role: str, worker_id: str, *, prefix: str = _PREFIX) -> str:
    return f"{prefix}wp.{role}.{worker_id}"


def _presence_scan_prefix(role: str, *, prefix: str = _PREFIX) -> str:
    return f"{prefix}wp.{role}."


def heartbeat_presence(
    store: Any, role: str, worker_id: str, *, prefix: str = _PREFIX, now: Optional[float] = None
) -> None:
    """Announce/refresh a worker's presence (a timestamp blob). Off the request hot path:
    a stale presence just drops the worker from the parent's rotation after a TTL."""
    stamp = time.time() if now is None else now
    store.put(_KIND, _presence_name(role, worker_id, prefix=prefix), f"{stamp}".encode("utf-8"))


def drop_presence(store: Any, role: str, worker_id: str, *, prefix: str = _PREFIX) -> None:
    """Remove a worker's presence blob (clean shutdown)."""
    store.delete(_KIND, _presence_name(role, worker_id, prefix=prefix))


def live_workers(
    store: Any, role: str, *, prefix: str = _PREFIX, ttl: float = 6.0, now: Optional[float] = None
) -> list[str]:
    """The worker ids for ``role`` whose presence heartbeat is fresher than ``ttl``. A worker
    that stopped heartbeating (crashed/exited) falls out of the list in ~``ttl``."""
    now = time.time() if now is None else now
    scan_prefix = _presence_scan_prefix(role, prefix=prefix)
    out: list[str] = []
    for name, data in store.scan(_KIND, scan_prefix):
        try:
            ts = float(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if now - ts < ttl:
            out.append(name[len(scan_prefix):])
    return sorted(out)


async def run_isolated_worker(
    store: Any,
    handler: Handler,
    *,
    role: str,
    worker_id: str,
    prefix: str = _PREFIX,
    stop: asyncio.Event,
    poll: float = 0.1,
    presence_ttl: float = 6.0,
) -> None:
    """Drain THIS worker's private queue until ``stop`` is set, heartbeating presence so the
    parent routes to it. Because the worker is the SOLE reader of its queue there is NO claim
    and NO lease — each request runs exactly once. A failing/poison request drains as a
    ``failed`` result (``serve_one``); it never kills the loop."""
    mailbox = ClioCoreMailbox(store, prefix=_worker_queue_prefix(role, worker_id, prefix=prefix))
    heartbeat_presence(store, role, worker_id, prefix=prefix)  # announce immediately
    hb_every = max(presence_ttl / 3.0, 0.05)

    async def _beat() -> None:
        # Heartbeat on its OWN task, not inline after the drain: ``serve_one`` awaits the full
        # child handler (a multi-minute ALCF turn), and an inline refresh would not fire until
        # that returns — so a BUSY worker would age out of ``live_workers`` mid-request and the
        # parent would see a spurious empty pool (capacity collapsing exactly under load). A
        # separate task keeps presence fresh while the handler runs. Mirrors the lease _heartbeat.
        while not stop.is_set():
            await asyncio.sleep(hb_every)
            with contextlib.suppress(Exception):
                heartbeat_presence(store, role, worker_id, prefix=prefix)

    beater = asyncio.ensure_future(_beat())
    try:
        while not stop.is_set():
            served = False
            for rid in mailbox.pending():
                await serve_one(mailbox, rid, handler)  # sole reader -> no claim
                served = True
            if not served:
                await asyncio.sleep(poll)
    finally:
        beater.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await beater
        drop_presence(store, role, worker_id, prefix=prefix)


class IsolatedExpertInvoker:
    """Parent-side ``ExpertInvoker`` for the isolated model: route a request to ONE live
    worker's private queue (no claim/lease), wait, and re-route to another live worker on
    timeout. Exactly-once in the common case (a single worker reads the queue); at-least-once
    only on a reassignment after a worker dies/stalls — the unavoidable, rare resilience path,
    where the result is still correct (the parent takes whichever worker answers).

    Drop-in ``ExpertInvoker`` (``invoke(request) -> ExpertResult``) so it composes with the
    background monitor and the gact delegation hinge exactly like the pull-model invoker."""

    def __init__(
        self,
        store: Any,
        *,
        role: str,
        prefix: str = _PREFIX,
        timeout: float = 60.0,
        poll: float = 0.1,
        presence_ttl: float = 6.0,
        max_attempts: int = 3,
        ready_timeout: float = 0.0,
    ) -> None:
        self._store = store
        self._role = role
        self._prefix = prefix
        self._timeout = timeout
        self._poll = poll
        self._presence_ttl = presence_ttl
        self._max_attempts = max_attempts
        # How long to wait for a live worker to APPEAR before giving up. Default 0.0 keeps the
        # historical fail-fast behavior; a positive value lets a parent tolerate a fleet that is
        # still coming up (e.g. just launched on startup) instead of failing the first delegation.
        self._ready_timeout = ready_timeout
        self._rr = 0  # round-robin cursor over the live set

    def _pick(self, exclude: set[str]) -> Optional[str]:
        workers = [
            w
            for w in live_workers(self._store, self._role, prefix=self._prefix, ttl=self._presence_ttl)
            if w not in exclude
        ]
        if not workers:
            return None
        self._rr = (self._rr + 1) % len(workers)
        return workers[self._rr]

    async def _pick_ready(self, exclude: set[str]) -> Optional[str]:
        """Pick a live (untried) worker, waiting up to ``ready_timeout`` for one to appear.
        With ``ready_timeout == 0`` this is exactly ``_pick`` (immediate). Returns ``None`` if
        none becomes available in time — the caller decides whether that's a cold-pool error
        (nothing tried yet) or exhaustion (every reachable worker already tried)."""
        waited = 0.0
        while True:
            worker = self._pick(exclude)
            if worker is not None or waited >= self._ready_timeout:
                return worker
            await asyncio.sleep(self._poll)
            waited += self._poll

    async def invoke(self, request: ExpertRequest) -> ExpertResult:
        tried: set[str] = set()
        last_error: Optional[BaseException] = None
        for _ in range(self._max_attempts):
            worker = await self._pick_ready(tried)
            if worker is None:
                if tried:  # we exhausted the workers we could reach
                    break
                raise RuntimeError(
                    f"no live worker for role {self._role!r}"
                    + (f" within {self._ready_timeout}s" if self._ready_timeout else "")
                )
            tried.add(worker)
            mailbox = ClioCoreMailbox(
                self._store, prefix=_worker_queue_prefix(self._role, worker, prefix=self._prefix)
            )
            rid = mailbox.submit(request)

            async def _wait(mb: ClioCoreMailbox = mailbox, the_rid: str = rid) -> None:
                while not mb.has_result(the_rid):
                    await asyncio.sleep(self._poll)

            try:
                await asyncio.wait_for(_wait(), timeout=self._timeout)
            except (asyncio.TimeoutError, TimeoutError):
                # The worker died or stalled — discard and re-route to another live worker.
                # serve_one re-checks .req before publishing, so a worker that revives mid-flight
                # drops its result cleanly rather than orphaning it.
                mailbox.discard(rid)
                last_error = TimeoutError(f"worker {worker!r} for {self._role!r} timed out")
                continue
            except asyncio.CancelledError:
                mailbox.discard(rid)
                raise
            result = mailbox.read_result(rid)
            mailbox.discard(rid)
            if result is not None:
                return result
            last_error = RuntimeError(f"isolated result vanished for {rid}")
        raise last_error or TimeoutError(f"all workers for role {self._role!r} timed out")
