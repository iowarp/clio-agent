#!/usr/bin/env python3
"""Parent CLIO: a separate process that delegates a data-analysis task to the data
expert running on the worker CLIO, entirely through clio-core context (the shared
clio_run daemon). It never imports the worker — they share only clio-core."""
import asyncio
import os
import sys

os.environ.setdefault("CLIO_CTE_WITH_RUNTIME", "0")  # attach to the daemon, don't embed

from clio_agent.arc.storage import make_arc_store  # noqa: E402
from clio_agent.runtime.cee_transport import CEEExpertInvoker, CEEMailbox  # noqa: E402
from clio_agent.runtime.expert_invoker import ExpertRequest  # noqa: E402

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "x2c_"
PID = os.getpid()
QUESTION = "Use the lookup tool to get the 'throughput' metric and report its exact value."


async def main() -> None:
    store = make_arc_store(backend="cte")  # attaches to the SAME clio_run daemon
    mb = CEEMailbox(store, prefix=PREFIX)
    inv = CEEExpertInvoker(mb, timeout=90)
    print(f"[parent {PID}] delegating to the data expert via clio-core...", flush=True)
    try:
        res = await inv.invoke(ExpertRequest("data", QUESTION, session_id="x-sess"))
        wp = res.workflow_state.get("worker_pid")
        print(f"[parent {PID}] got answer: {res.answer!r}", flush=True)
        print(
            f"[parent {PID}] worker_pid={wp} my_pid={PID} "
            f"CROSS_PROCESS={wp is not None and wp != PID}",
            flush=True,
        )
        print(f"[parent {PID}] VALUE_42.7_IN_ANSWER={'42.7' in res.answer}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[parent {PID}] FAILED: {type(exc).__name__}: {exc}", flush=True)
    finally:
        store.put("context", f"{PREFIX}STOP", b"1")  # tell the worker to stop


asyncio.run(main())
