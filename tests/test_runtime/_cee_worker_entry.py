"""Test worker entrypoint (NOT collected — leading underscore). A real gact worker in its
OWN process: builds an app, registers a 'calc' expert into an isolated config, and drains a
shared LocalFS mailbox via run_cee_worker. Spawned by test_cee_worker's cross-process test.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


def main() -> None:
    from clio_agent.agent import ClioAgent
    from clio_agent.arc.storage import make_arc_store
    from clio_agent.config import setup_dspy
    from clio_agent.gact.app import build_app
    from clio_agent.runtime.cee_worker import run_cee_worker

    setup_dspy()
    app = build_app(agent=ClioAgent(), sessions_path=Path(os.environ["CLIO_GACT_SESSIONS"]))
    # this worker's registry — the parent only sends an expert_id; the worker reconstructs it
    app.state.user_agents.upsert(
        {
            "id": "calc",
            "title": "Calculator",
            "source": "expert_pack",
            "system_prompt": "You are a precise calculator. Answer with only the number.",
        }
    )
    store = make_arc_store(backend="local", data_dir=os.environ["CLIO_ARC_DATA_DIR"])
    asyncio.run(
        run_cee_worker(store, prefix=os.environ.get("CLIO_CEE_PREFIX", "cee_"), app=app)
    )


if __name__ == "__main__":
    main()
