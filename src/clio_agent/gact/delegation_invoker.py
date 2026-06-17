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
) -> Any:
    """Run a child expert, optionally through the transport-abstracted boundary.

    ``run_child`` is ``async (agent_def, prompt) -> dspy.Prediction`` (today's
    in-process runner). Default (unknown ``mode``) returns that prediction verbatim —
    full parity, zero behavior change to the live delegation path. The boundary modes:

    * ``"loopback"`` — request/result cross a JSON wire in memory and fold back,
      proving the contract is serialization-clean without any store.
    * ``"cee"`` — request/result cross the clio-core **mailbox**: serialized to blobs
      in an ``ARCStore`` (LocalFS single-box, or attached CTE on a cluster), discovered
      by a ``run_worker`` loop via a ``pending()`` scan, claimed under a TTL lease, and
      read back. This exercises the FULL detached transport against a real delegation —
      identical to the cross-node path except the worker is in-process here. Moving the
      worker to a separate process (a gact worker on another node, same store) is a
      deployment change, not a change to this call.

    ``store`` supplies the mailbox backend for ``"cee"``; when omitted a throwaway
    LocalFS store is created and cleaned up (the single-box proof). ``CLIO_CEE_TIMEOUT``
    bounds the wait (default 300s — real ALCF children are slow).
    """
    if mode not in ("loopback", "cee"):
        return await run_child(agent_def, prompt)

    async def _handler(req: ExpertRequest) -> ExpertResult:
        pred = await run_child(agent_def, req.question)
        return expert_result_from_prediction(pred, expert_id=req.expert_id)

    if mode == "loopback":
        result = await LoopbackExpertInvoker(_handler).invoke(
            expert_request_for(agent_def, prompt, session_id=session_id)
        )
        return prediction_from_result(result)

    result = await _invoke_via_cee(
        _handler, expert_request_for(agent_def, prompt, session_id=session_id), store=store
    )
    return prediction_from_result(result)


async def _invoke_via_cee(
    handler: Any, request: ExpertRequest, *, store: Any = None
) -> ExpertResult:
    """Route one delegation through the clio-core mailbox with an in-process worker
    draining it. Same transport a cross-node worker uses; only the worker's locality
    differs. Owns (and cleans up) a throwaway LocalFS store when none is supplied."""
    from clio_agent.runtime.cee_transport import CEEExpertInvoker, CEEMailbox, run_worker

    owns_store = store is None
    tmp_dir = ""
    if owns_store:
        from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

        tmp_dir = tempfile.mkdtemp(prefix="clio_cee_")
        store = make_arc_store(backend="local", data_dir=tmp_dir)

    timeout = float(os.environ.get("CLIO_CEE_TIMEOUT", "300"))
    mailbox = CEEMailbox(store)
    stop = asyncio.Event()
    worker = asyncio.ensure_future(run_worker(mailbox, handler, stop=stop, poll=0.05))
    try:
        return await CEEExpertInvoker(mailbox, timeout=timeout, poll=0.05).invoke(request)
    finally:
        stop.set()
        worker.cancel()
        with contextlib.suppress(BaseException):
            await worker
        if owns_store and tmp_dir:
            with contextlib.suppress(OSError):
                shutil.rmtree(tmp_dir, ignore_errors=True)
