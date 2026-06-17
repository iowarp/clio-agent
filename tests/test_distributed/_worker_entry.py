#!/usr/bin/env python3
"""Worker-process entrypoint for the cross-process tests.

A SEPARATE OS process from the test: attaches to the shared clio_run daemon
(``CLIO_CTE_WITH_RUNTIME=0``) and drains the clio-core mailbox with a handler chosen
by ``mode``. Used to exercise real multi-process behavior — death, slowness,
reclaim, and real ALCF experts — that single-event-loop tests cannot.

    argv: <prefix> <mode>
    modes: echo | crash | hardcrash | slow | alcf
"""
from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("CLIO_CTE_WITH_RUNTIME", "0")  # attach to the daemon

from clio_agent.arc.storage import make_arc_store  # noqa: E402
from clio_agent.runtime.clio_core_transport import ClioCoreMailbox, run_worker  # noqa: E402
from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult  # noqa: E402

PREFIX = sys.argv[1]
MODE = sys.argv[2] if len(sys.argv) > 2 else "echo"
PID = os.getpid()


def _make_handler(mode: str, store):
    async def echo(req: ExpertRequest) -> ExpertResult:
        return ExpertResult(
            expert_id=req.expert_id,
            answer=f"echo:{req.question}",
            workflow_state={"worker_pid": PID, "mode": mode},
        )

    async def crash(req: ExpertRequest) -> ExpertResult:
        raise RuntimeError(f"worker {PID} crash-handler")  # recorded as a failed result

    async def hardcrash(req: ExpertRequest) -> ExpertResult:
        # Simulate a worker process dying mid-delegation: it has claimed the request
        # but exits before publishing any result. Deterministic (no external kill race).
        os._exit(137)

    async def leasehold(req: ExpertRequest) -> ExpertResult:
        # Records THIS execution (so a test can count them) then holds the request
        # longer than the lease TTL — the heartbeat must keep our lease alive so no
        # other worker reclaims and double-executes.
        store.put("context", f"{PREFIX}EXEC_{PID}", b"1")
        await asyncio.sleep(8.0)
        return ExpertResult(
            expert_id=req.expert_id,
            answer=f"leased:{req.question}",
            workflow_state={"worker_pid": PID, "mode": "leasehold"},
        )

    async def slow(req: ExpertRequest) -> ExpertResult:
        await asyncio.sleep(30)  # longer than the parent's timeout
        return ExpertResult(expert_id=req.expert_id, answer="too-late", workflow_state={"worker_pid": PID})

    async def alcf(req: ExpertRequest) -> ExpertResult:
        import dspy  # noqa: PLC0415

        from clio_agent.config import create_lm, load_config_from_env  # noqa: PLC0415

        def lookup(metric: str) -> str:
            """Look up the current value of a named data metric."""
            return f"VALUE[{metric}]=42.7"

        cfg = load_config_from_env()
        lm = create_lm(cfg)
        expert = dspy.ReAct("question -> answer", tools=[dspy.Tool(lookup)])
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            pred = expert(question=req.question)
        return ExpertResult(
            expert_id=req.expert_id,
            answer=str(getattr(pred, "answer", "") or ""),
            workflow_state={
                "worker_pid": PID,
                "mode": "alcf",
                "api_base": cfg.api_base,
                "model": cfg.model,
            },
        )

    async def role(req: ExpertRequest) -> ExpertResult:
        """A real ALCF expert with a role system prompt (CLIO_WORKER_ROLE) that takes
        the question + the delegated context — for NDP-pipeline multi-hop tests."""
        import dspy  # noqa: PLC0415

        from clio_agent.config import create_lm, load_config_from_env  # noqa: PLC0415

        role_prompt = os.environ.get("CLIO_WORKER_ROLE", "You are a helpful expert.")
        lm = create_lm(load_config_from_env())
        sig = dspy.Signature("question: str, context: str -> answer: str", role_prompt)
        expert = dspy.ChainOfThought(sig)
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            pred = expert(question=req.question, context=str(req.context))
        return ExpertResult(
            expert_id=req.expert_id,
            answer=str(getattr(pred, "answer", "") or ""),
            workflow_state={"worker_pid": PID, "role": role_prompt[:40]},
        )

    async def stage(req: ExpertRequest) -> ExpertResult:
        # "data" pool: simulate a slow NDP CSV download, then return a HANDLE (a path/
        # reference), NOT the bytes — the data stays where it was staged.
        await asyncio.sleep(float(os.environ.get("CLIO_STAGE_SECS", "2.0")))
        return ExpertResult(
            expert_id=req.expert_id,
            answer=f"staged:{req.question}",
            workflow_state={"worker_pid": PID, "pool": "data", "handle": f"/data/{req.question}.csv"},
        )

    async def analyze(req: ExpertRequest) -> ExpertResult:
        # "compute" pool: analyze the data referenced by the handle (fetched via the
        # context plane), never re-staging it.
        await asyncio.sleep(float(os.environ.get("CLIO_ANALYZE_SECS", "1.0")))
        handle = req.context.get("handle", "")
        return ExpertResult(
            expert_id=req.expert_id,
            answer=f"analyzed:{handle}",
            workflow_state={"worker_pid": PID, "pool": "compute", "handle": handle},
        )

    return {
        "echo": echo, "crash": crash, "hardcrash": hardcrash, "slow": slow,
        "alcf": alcf, "role": role, "leasehold": leasehold,
        "stage": stage, "analyze": analyze,
    }[mode]


async def main() -> None:
    store = make_arc_store(backend="cte")  # attaches to the clio_run daemon
    mb = ClioCoreMailbox(store, prefix=PREFIX)
    handler = _make_handler(MODE, store)
    store.put("context", f"{PREFIX}READY_{PID}", b"1")  # readiness signal for the test
    print(f"WORKER_READY pid={PID} mode={MODE}", flush=True)

    stop = asyncio.Event()

    async def watch() -> None:
        for _ in range(4000):  # ~200s safety cap
            if store.exists("context", f"{PREFIX}STOP"):
                break
            await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(run_worker(mb, handler, stop=stop, worker_id=f"w{PID}"), watch())


if __name__ == "__main__":
    asyncio.run(main())
