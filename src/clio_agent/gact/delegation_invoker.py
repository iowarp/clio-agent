"""Bridge gact's expert delegation to the transport-abstracted invoker (epic #667,
issues #671/#441).

``_run_dynamic_agent_sync`` returns a rich ``dspy.Prediction``; the settle loop reads
``answer``, ``next_expert``/``next_task``, ``expert_handoffs`` (the parent's routing
decision) and ``workflow_state`` off it. To run a child through the
:class:`ExpertInvoker` boundary — in-process today, detached on a cluster — those
fields must cross as a serializable :class:`ExpertResult` and come back without
losing the parent's decision.

This module is the mapping (the integration's core), kept separate and unit-tested
so the live wiring in ``_settle_dynamic_agent_delegations`` is a thin, low-risk swap
rather than a rewrite. The routing decision rides as a dedicated ``routing`` event so
it survives the wire verbatim — clio carries the decision, it does not re-derive it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from collections.abc import Mapping
from typing import Any

from clio_agent.runtime.expert_invoker import (
    ExpertEvent,
    ExpertRequest,
    ExpertResult,
    LoopbackExpertInvoker,
)

ROUTING_EVENT = "routing"


def expert_request_for(
    agent_def: Any,
    prompt: str,
    *,
    session_id: str = "",
    scope: str = "",
    context: dict | None = None,
) -> ExpertRequest:
    """Build the serializable request a child expert is invoked with."""
    return ExpertRequest(
        expert_id=str(getattr(agent_def, "id", "") or ""),
        question=prompt,
        session_id=session_id,
        scope=scope,
        context=dict(context or {}),
    )


def _jsonable(value: Any) -> Any:
    """Coerce a prediction field to something JSON-safe (strings/lists pass; other
    objects degrade to their string form rather than break the wire)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def expert_result_from_prediction(
    pred: Any, *, expert_id: str, status: str = "completed"
) -> ExpertResult:
    """Map a dspy.Prediction to a serializable :class:`ExpertResult`, preserving the
    answer, the typed ``workflow_state``, and the parent's routing decision."""
    ws = getattr(pred, "workflow_state", None)
    workflow_state = (
        {str(k): _jsonable(v) for k, v in ws.items()} if isinstance(ws, Mapping) else {}
    )
    routing = {
        "next_expert": str(getattr(pred, "next_expert", "") or ""),
        "next_task": str(getattr(pred, "next_task", "") or ""),
        "expert_handoffs": _jsonable(getattr(pred, "expert_handoffs", None)),
    }
    return ExpertResult(
        expert_id=expert_id,
        answer=str(getattr(pred, "answer", "") or ""),
        status=status,
        events=[ExpertEvent(ROUTING_EVENT, routing)],
        workflow_state=workflow_state,
    )


def routing_from_result(result: ExpertResult) -> dict[str, Any]:
    """Recover the parent's routing decision (next_expert/next_task/expert_handoffs)
    from a result that crossed the boundary. Empty dict if none was carried."""
    for ev in result.events:
        if ev.kind == ROUTING_EVENT:
            return dict(ev.payload)
    return {}


def prediction_from_result(result: ExpertResult) -> Any:
    """Rebuild a dspy.Prediction the settle loop can consume from a result that
    crossed the boundary. Carries the core fields (answer, routing, workflow_state);
    richer instrumentation (trajectory/tools) stays on the in-process path until the
    detached transport carries it (#659)."""
    import dspy  # noqa: PLC0415

    routing = routing_from_result(result)
    return dspy.Prediction(
        answer=result.answer,
        next_expert=routing.get("next_expert", ""),
        next_task=routing.get("next_task", ""),
        expert_handoffs=routing.get("expert_handoffs") or "",
        workflow_state=dict(result.workflow_state),
    )


async def run_child_via_boundary(
    agent_def: Any,
    prompt: str,
    *,
    run_child: Any,
    session_id: str = "",
    mode: str = "",
    store: Any = None,
    role: str = "",
) -> Any:
    """Run a child expert, optionally through the transport-abstracted boundary.

    ``run_child`` is ``async (agent_def, prompt) -> dspy.Prediction`` (today's
    in-process runner). Default (unknown ``mode``) returns that prediction verbatim —
    full parity, zero behavior change to the live delegation path. The boundary modes:

    * ``"loopback"`` — request/result cross a JSON wire in memory and fold back,
      proving the contract is serialization-clean without any store.
    * ``"clio_core"`` — request/result cross the clio-core **mailbox**: serialized to blobs
      in an ``ARCStore`` (LocalFS single-box, or attached CTE on a cluster), discovered
      by a ``run_worker`` loop via a ``pending()`` scan, claimed under a TTL lease, and
      read back. The worker runs IN-PROCESS here (driven by ``run_child``) — it exercises
      the full transport against a real delegation, identical to the cross-node path except
      the worker's locality.
    * ``"clio_core_isolated"`` — the DETACHED model: the child runs in a SEPARATE PROCESS.
      The parent routes the request to one live worker's private queue (no claim/lease,
      exactly-once by construction — clio-core#559 path (a)) via
      :class:`IsolatedExpertInvoker` and reads the result back. ``run_child`` is NOT used
      here — external ``run_isolated_clio_core_worker`` processes (one per node, same
      ``store``) reconstruct and run the child. This is the real multinode delegation hinge.

    ``store`` supplies the mailbox backend. For ``"clio_core"`` a throwaway LocalFS store is
    created+cleaned when omitted (the single-box proof); for ``"clio_core_isolated"`` it is
    REQUIRED (the parent and its detached workers must share one store) — typically the
    agent's own ARC store (``app.state.arc.store``). ``role`` selects the worker pool for the
    isolated model (defaults to ``CLIO_CORE_ROLE`` env, else the expert id). ``CLIO_CORE_TIMEOUT``
    bounds the wait (default 300s — real ALCF children are slow).
    """
    if mode not in ("loopback", "clio_core", "clio_core_isolated"):
        return await run_child(agent_def, prompt)

    request = expert_request_for(agent_def, prompt, session_id=session_id)

    if mode == "clio_core_isolated":
        result = await _invoke_via_isolated(request, store=store, role=role)
        return prediction_from_result(result)

    async def _handler(req: ExpertRequest) -> ExpertResult:
        pred = await run_child(agent_def, req.question)
        return expert_result_from_prediction(pred, expert_id=req.expert_id)

    if mode == "loopback":
        result = await LoopbackExpertInvoker(_handler).invoke(request)
        return prediction_from_result(result)

    result = await _invoke_via_clio_core(_handler, request, store=store)
    return prediction_from_result(result)


async def _invoke_via_isolated(
    request: ExpertRequest, *, store: Any, role: str = ""
) -> ExpertResult:
    """Route one delegation to the isolated detached worker pool over a SHARED store.

    No in-process worker and no lease: the parent submits to one live worker's private
    queue and reads the result back; external ``run_isolated_clio_core_worker`` processes
    (same store, heartbeating presence) do the work. The shared store is mandatory — without
    it parent and workers cannot rendezvous. ``CLIO_CORE_PREFIX`` namespaces the mailbox.

    ROUTING CONTRACT — the pool ``role`` is, by default, the child **expert id**: an empty
    ``role`` resolves to ``request.expert_id``, so a fleet must serve a pool per delegated
    expert (``CLIO_CORE_FLEET=<expert_id>:N``). Set ``CLIO_CORE_ROLE`` on the parent ONLY for
    the advanced "one shared pool serves every expert" model (workers resolve any ``expert_id``
    from their registry) — then every delegation pins to that single pool. A mismatch (fleet
    role ≠ expert id, no ``CLIO_CORE_ROLE``) surfaces loudly as ``no live worker for role
    '<expert_id>'`` rather than silently, because the requested role names the missing pool.
    """
    if store is None:
        raise ValueError(
            "clio_core_isolated needs a shared store (parent + detached workers must share "
            "one mailbox backend); pass store=app.state.arc.store"
        )
    from clio_agent.runtime.clio_core_transport import IsolatedExpertInvoker  # noqa: PLC0415

    resolved_role = role or os.environ.get("CLIO_CORE_ROLE", "") or request.expert_id
    if not resolved_role:
        raise ValueError("clio_core_isolated needs a role (set CLIO_CORE_ROLE or an expert id)")
    prefix = os.environ.get("CLIO_CORE_PREFIX", "clio_core_")
    timeout = float(os.environ.get("CLIO_CORE_TIMEOUT", "300"))
    # Tolerate a fleet that is still coming up (just-launched workers): wait this long for a
    # live worker to appear before failing the delegation. CLIO_CORE_READY_TIMEOUT=0 fails fast.
    ready_timeout = float(os.environ.get("CLIO_CORE_READY_TIMEOUT", "60"))
    # Pull-transport poll rate (config-driven via CLIO_CORE_POLL): the latency/daemon-load
    # trade-off for the result wait — smaller = lower latency, more RPCs.
    poll = float(os.environ.get("CLIO_CORE_POLL", "0.05"))
    invoker = IsolatedExpertInvoker(
        store, role=resolved_role, prefix=prefix, timeout=timeout, poll=poll, ready_timeout=ready_timeout
    )
    return await invoker.invoke(request)


async def _invoke_via_clio_core(
    handler: Any, request: ExpertRequest, *, store: Any = None
) -> ExpertResult:
    """Route one delegation through the clio-core mailbox with an in-process worker
    draining it. Same transport a cross-node worker uses; only the worker's locality
    differs. Owns (and cleans up) a throwaway LocalFS store when none is supplied."""
    from clio_agent.runtime.clio_core_transport import (
        ClioCoreExpertInvoker,
        ClioCoreMailbox,
        run_worker,
    )

    owns_store = store is None
    tmp_dir = ""
    if owns_store:
        from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

        tmp_dir = tempfile.mkdtemp(prefix="clio_core_")
        try:
            store = make_arc_store(backend="local", data_dir=tmp_dir)
        except BaseException:
            # store construction can fail (e.g. mkdir on a read-only/full mount); the
            # delegation's cleanup finally below hasn't been entered yet, so remove the
            # just-created temp dir here rather than orphan it.
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    timeout = float(os.environ.get("CLIO_CORE_TIMEOUT", "300"))
    mailbox = ClioCoreMailbox(store)
    stop = asyncio.Event()
    worker = asyncio.ensure_future(run_worker(mailbox, handler, stop=stop, poll=0.05))
    try:
        return await ClioCoreExpertInvoker(mailbox, timeout=timeout, poll=0.05).invoke(request)
    finally:
        stop.set()
        worker.cancel()
        with contextlib.suppress(BaseException):
            await worker
        if owns_store and tmp_dir:
            with contextlib.suppress(OSError):
                shutil.rmtree(tmp_dir, ignore_errors=True)
