"""Separate-process gact worker (epic #667, issues #671/#659).

The cross-process counterpart to the in-process cee invoker. The parent submits an
:class:`ExpertRequest` to a shared store exactly as today; here a **different process**
reconstructs the child expert from the request (``expert_id`` -> ``AgentDef`` via the app's
registry) and runs it through :func:`run_child_expert`, publishing the
:class:`ExpertResult` back. Both parties share ONLY the store — a LocalFS directory (single
box) or a ``clio_run`` daemon (cluster) — so this is genuine cross-process delegation, not
the in-process worker the live ``cee`` path used.

A worker IS a full gact instance (``build_app`` + a real ``ClioAgent``) that happens to take
its work from the mailbox instead of an HTTP turn, so per-expert models, tools, and blueprint
routing resolve identically to an in-turn child. Run one per node (or several) pointed at the
node's daemon:

    CLIO_ARC_STORE=cte CLIO_CTE_WITH_RUNTIME=0 CLIO_CEE_PREFIX=cee_data \\
        python -m clio_agent.runtime.cee_worker
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Optional

from clio_agent.runtime.cee_transport import CEEMailbox, run_worker
from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult

Handler = Callable[[ExpertRequest], Awaitable[ExpertResult]]


def build_child_handler(app: Any) -> Handler:
    """A mailbox handler that reconstructs and runs a child expert in ``app``.

    An ``expert_id`` not in this worker's registry drains as a ``failed`` result (it never
    hangs the parent); ``run_child_expert`` itself records a child that raises as a failed
    prediction, so one bad delegation can't kill the worker loop.
    """
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
        pred = await run_child_expert(
            app,
            agent_def,
            req.question,
            session_id=req.session_id or f"cee-worker:{req.expert_id}",
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


async def run_cee_worker(
    store: Any,
    *,
    prefix: str = "cee_",
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
        CEEMailbox(store, prefix=prefix),
        build_child_handler(app),
        stop=stop,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        poll=poll,
    )


def _main() -> None:  # pragma: no cover - process entrypoint
    """``python -m clio_agent.runtime.cee_worker`` — attach to the store from env and drain.

    Env: ``CLIO_CEE_PREFIX`` (role queue, default ``cee_``); ``CLIO_CEE_WORKER_ID``; the store
    is built via ``make_arc_store`` from ``CLIO_ARC_STORE`` + ``CLIO_ARC_DATA_DIR`` (a shared
    LocalFS dir, or a daemon attach with ``CLIO_ARC_STORE=cte`` + ``CLIO_CTE_WITH_RUNTIME=0``).
    """
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

    backend = os.environ.get("CLIO_ARC_STORE", "local")
    data_dir = os.environ.get("CLIO_ARC_DATA_DIR", "") or None
    store = make_arc_store(backend=backend, data_dir=data_dir)
    asyncio.run(
        run_cee_worker(
            store,
            prefix=os.environ.get("CLIO_CEE_PREFIX", "cee_"),
            worker_id=os.environ.get("CLIO_CEE_WORKER_ID", ""),
        )
    )


if __name__ == "__main__":
    _main()
