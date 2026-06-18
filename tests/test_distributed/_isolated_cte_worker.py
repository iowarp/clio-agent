"""Isolated worker entrypoint for the CTE cross-process test (NOT collected — leading
underscore). A SEPARATE OS process that attaches to the shared ``clio_run`` daemon and drains
its OWN isolated queue over CTE (no claim/lease). Echo handler keeps it LM-free so the test
exercises the transport, not inference. Spawned by ``test_isolated_cross_process.py``.
"""

from __future__ import annotations

import asyncio
import os


def main() -> None:
    os.environ["CLIO_CTE_WITH_RUNTIME"] = "0"  # attach to the daemon, don't embed
    os.environ.setdefault("CLIO_ARC_STORE", "cte")

    from clio_agent.arc.storage import make_arc_store
    from clio_agent.runtime.clio_core_transport import run_isolated_worker
    from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult

    store = make_arc_store(backend="cte")
    wid = os.environ["CLIO_CORE_WORKER_ID"]
    role = os.environ["CLIO_CORE_ROLE"]
    prefix = os.environ["CLIO_CORE_PREFIX"]
    # Optional: a sentinel-blob stop (test-driven). Unset = run until the process is signalled
    # (how the cluster deployer manages workers — SIGTERM, not a stop blob).
    stop_key = os.environ.get("CLIO_CORE_STOP_KEY", "")

    async def handler(req: ExpertRequest) -> ExpertResult:
        # tag the answer with THIS process's pid so the parent can prove it ran out-of-process
        return ExpertResult(expert_id=req.expert_id, answer=f"WORKER{os.getpid()}:{req.question}")

    async def run() -> None:
        stop = asyncio.Event()
        coros = [
            run_isolated_worker(
                store, handler, role=role, worker_id=wid, prefix=prefix, stop=stop, poll=0.05
            )
        ]
        if stop_key:

            async def watch() -> None:
                while not store.exists("context", stop_key):
                    await asyncio.sleep(0.1)
                stop.set()

            coros.append(watch())
        await asyncio.gather(*coros)

    asyncio.run(run())


if __name__ == "__main__":
    main()
