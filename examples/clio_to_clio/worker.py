#!/usr/bin/env python3
"""Worker CLIO: attaches to the shared clio_run daemon and runs a REAL ALCF data
expert for any delegation that lands in the clio-core mailbox. A *separate process*
from the parent — they share only clio-core context."""
import asyncio
import os
import sys

os.environ.setdefault("CLIO_CTE_WITH_RUNTIME", "0")  # attach to the daemon, don't embed

import dspy  # noqa: E402

from clio_agent.arc.storage import make_arc_store  # noqa: E402
from clio_agent.config import create_lm, load_config_from_env  # noqa: E402
from clio_agent.runtime.cee_transport import CEEMailbox, run_worker  # noqa: E402
from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult  # noqa: E402

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "x2c_"
PID = os.getpid()


def lookup(metric: str) -> str:
    """Look up the current value of a named data metric."""
    return f"VALUE[{metric}]=42.7"


async def main() -> None:
    store = make_arc_store(backend="cte")  # attaches to the clio_run daemon
    mb = CEEMailbox(store, prefix=PREFIX)
    lm = create_lm(load_config_from_env())
    expert = dspy.ReAct("question -> answer", tools=[dspy.Tool(lookup)])
    print(f"[worker {PID}] attached to clio-core daemon; data-expert ready", flush=True)

    async def handler(req: ExpertRequest) -> ExpertResult:
        print(f"[worker {PID}] expert handling: {req.question!r}", flush=True)
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            pred = expert(question=req.question)
        ans = str(getattr(pred, "answer", "") or "")
        print(f"[worker {PID}] expert answered: {ans!r}", flush=True)
        return ExpertResult(
            expert_id=req.expert_id,
            answer=ans,
            workflow_state={"worker_pid": PID, "expert": "data-react"},
        )

    stop = asyncio.Event()

    async def watch() -> None:
        for _ in range(2400):  # ~120s safety cap
            if store.exists("context", f"{PREFIX}STOP"):
                break
            await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(run_worker(mb, handler, stop=stop), watch())
    print(f"[worker {PID}] stopped", flush=True)


asyncio.run(main())
