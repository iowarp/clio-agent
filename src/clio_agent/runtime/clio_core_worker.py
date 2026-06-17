"""Separate-process gact worker (epic #667, issues #671/#659).

The cross-process counterpart to the in-process clio_core invoker. The parent submits an
:class:`ExpertRequest` to a shared store exactly as today; here a **different process**
reconstructs the child expert from the request (``expert_id`` -> ``AgentDef`` via the app's
registry) and runs it through :func:`run_child_expert`, publishing the
:class:`ExpertResult` back. Both parties share ONLY the store — a LocalFS directory (single
box) or a ``clio_run`` daemon (cluster) — so this is genuine cross-process delegation, not
the in-process worker the live ``clio_core`` path used.

A worker IS a full gact instance (``build_app`` + a real ``ClioAgent``) that happens to take
its work from the mailbox instead of an HTTP turn, so per-expert models, tools, and blueprint
routing resolve identically to an in-turn child. Run one per node (or several) pointed at the
node's daemon:

    CLIO_ARC_STORE=cte CLIO_CTE_WITH_RUNTIME=0 CLIO_CORE_PREFIX=clio_core_data \\
        python -m clio_agent.runtime.clio_core_worker
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Optional

from clio_agent.runtime.clio_core_transport import ClioCoreMailbox, run_worker
from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult

Handler = Callable[[ExpertRequest], Awaitable[ExpertResult]]


def build_child_handler(app: Any) -> Handler:
    """A mailbox handler that reconstructs and runs a child expert in ``app``.

    An ``expert_id`` not in this worker's registry drains as a ``failed`` result. A child
    that RAISES is contained two ways: ``serve_one``/``_serve_under_lease`` (clio_core_transport)
    publish a failed/worker_error result for the worker loop, and — so the guarantee holds
    for ANY caller, not just via serve_one — this handler itself turns a raising run into a
    ``failed`` ExpertResult (``run_child_expert`` does NOT catch). Either way one bad
    delegation never hangs the parent or kills the loop.
    """
    import asyncio  # noqa: PLC0415

    from clio_agent.gact.app import _resolve_dynamic_agent, run_child_expert  # noqa: PLC0415
    from clio_agent.gact.delegation_invoker import expert_result_from_prediction  # noqa: PLC0415

    async def handler(req: ExpertRequest) -> ExpertResult:
        agent_def = _resolve_dynamic_agent(app, req.expert_id)
        if agent_def is None:
            return ExpertResult(
                expert_id=req.expert_id,
                status="failed",
                error=f"unknown expert {req.expert_id!r} in this worker's registry",
            )
        try:
            pred = await run_child_expert(
                app,
                agent_def,
                req.question,
                session_id=req.session_id or f"clio_core-worker:{req.expert_id}",
            )
        except asyncio.CancelledError:
            raise  # cooperative shutdown — never swallow
        except Exception as exc:  # noqa: BLE001 - drain as failed, never hang the parent
            return ExpertResult(
                expert_id=req.expert_id, status="failed", error=f"{type(exc).__name__}: {exc}"
            )
        return expert_result_from_prediction(pred, expert_id=req.expert_id)

    return handler


def build_worker_app() -> Any:
    """Construct a full gact app with a real ``ClioAgent`` — the same construction
    production uses, so per-expert model selection and tools resolve identically."""
    from clio_agent.agent import ClioAgent  # noqa: PLC0415
    from clio_agent.config import setup_dspy  # noqa: PLC0415
    from clio_agent.gact.app import build_app  # noqa: PLC0415

    setup_dspy()
    return build_app(agent=ClioAgent())


async def run_clio_core_worker(
    store: Any,
    *,
    prefix: str = "clio_core_",
    stop: Optional[asyncio.Event] = None,
    worker_id: str = "",
    lease_ttl: float = 6.0,
    poll: float = 0.1,
    app: Any = None,
) -> None:
    """Drain ``prefix`` on ``store``, running each delegated child via a real gact app until
    ``stop`` is set. Pass ``app`` to reuse an existing build (tests); otherwise one is built."""
    if app is None:
        app = build_worker_app()
    if stop is None:
        stop = asyncio.Event()
    await run_worker(
        ClioCoreMailbox(store, prefix=prefix),
        build_child_handler(app),
        stop=stop,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        poll=poll,
    )


async def run_isolated_clio_core_worker(
    store: Any,
    *,
    role: str,
    worker_id: str,
    prefix: str = "clio_core_",
    stop: Optional[asyncio.Event] = None,
    poll: float = 0.1,
    presence_ttl: float = 6.0,
    app: Any = None,
) -> None:
    """The lease-free counterpart of :func:`run_clio_core_worker`: a real gact worker that drains
    its OWN per-worker queue (no claim) and heartbeats presence, so the parent's
    ``IsolatedExpertInvoker`` routes to it. Same child reconstruction; just isolated queueing."""
    from clio_agent.runtime.clio_core_transport import run_isolated_worker  # noqa: PLC0415

    if app is None:
        app = build_worker_app()
    if stop is None:
        stop = asyncio.Event()
    await run_isolated_worker(
        store,
        build_child_handler(app),
        role=role,
        worker_id=worker_id,
        prefix=prefix,
        stop=stop,
        poll=poll,
        presence_ttl=presence_ttl,
    )


def _main() -> None:  # pragma: no cover - process entrypoint
    """``python -m clio_agent.runtime.clio_core_worker`` — attach to the store from env and drain.

    Env: ``CLIO_CORE_PREFIX`` (default ``clio_core_``); ``CLIO_CORE_WORKER_ID``; the store is built via
    ``make_arc_store`` from ``CLIO_ARC_STORE`` + ``CLIO_ARC_DATA_DIR`` (a shared LocalFS dir, or
    a daemon attach with ``CLIO_ARC_STORE=cte`` + ``CLIO_CTE_WITH_RUNTIME=0``). Set
    ``CLIO_CORE_ISOLATED=1`` + ``CLIO_CORE_ROLE`` to run the lease-free isolated model (drains
    this worker's own queue, heartbeats presence) instead of the shared-queue pull model.
    """
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

    backend = os.environ.get("CLIO_ARC_STORE", "local")
    data_dir = os.environ.get("CLIO_ARC_DATA_DIR", "") or None
    store = make_arc_store(backend=backend, data_dir=data_dir)
    prefix = os.environ.get("CLIO_CORE_PREFIX", "clio_core_")
    worker_id = os.environ.get("CLIO_CORE_WORKER_ID", "")
    if os.environ.get("CLIO_CORE_ISOLATED", "").strip().lower() in {"1", "true", "yes", "on"}:
        asyncio.run(
            run_isolated_clio_core_worker(
                store, role=os.environ["CLIO_CORE_ROLE"], worker_id=worker_id, prefix=prefix
            )
        )
    else:
        asyncio.run(run_clio_core_worker(store, prefix=prefix, worker_id=worker_id))


if __name__ == "__main__":
    _main()
