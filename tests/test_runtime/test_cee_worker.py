"""Separate-process gact worker (epic #667, #671): reconstructs + runs a delegated child
in its own build_app, the cross-process counterpart to the in-process cee invoker.

The unknown-expert path is unit-testable with no LM (it never reaches a child run). The
full cross-process run of a REAL child lives in the live suite.
"""

from __future__ import annotations

from clio_agent.runtime.expert_invoker import ExpertRequest


async def test_worker_handler_unknown_expert_drains_as_failed():
    """A request for an expert NOT in this worker's registry drains as a failed result —
    never hanging the parent — and without invoking an LM (the lookup fails first)."""
    from clio_agent.gact.app import build_app
    from clio_agent.runtime.cee_worker import build_child_handler

    app = build_app()  # no ClioAgent needed: the unknown-expert path never runs a child
    handler = build_child_handler(app)

    res = await handler(ExpertRequest("definitely_not_a_real_expert_xyz", "are you there?"))
    assert res.status == "failed"
    assert "unknown expert" in (res.error or "")
    assert res.expert_id == "definitely_not_a_real_expert_xyz"


def test_worker_module_exposes_entrypoints():
    """The worker exposes the build + run surface a launcher / test harness drives."""
    from clio_agent.runtime import cee_worker

    assert callable(cee_worker.build_child_handler)
    assert callable(cee_worker.build_worker_app)
    assert callable(cee_worker.run_cee_worker)
    assert hasattr(cee_worker, "_main")  # python -m clio_agent.runtime.cee_worker


# --- LIVE: the worker reconstructs + runs a REAL registered child on ALCF ----------------

import os  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live ALCF run: set CLIO_RUN_LIVE=1 (and Argonne auth + CLIO_LM_* env)",
)
async def test_worker_runs_a_real_registered_child(tmp_path):
    """The untangle's payload: the worker handler resolves an expert_id to a registered
    AgentDef and runs it via run_child_expert against real ALCF, returning a real answer.
    user_agents is isolated to tmp (sessions_path) so the upsert never touches real config.
    This is the reconstruct-and-run that a separate worker PROCESS does; the cross-process
    mailbox transport carrying it is already proven (test_cee_transport)."""
    from clio_agent.agent import ClioAgent
    from clio_agent.config import load_config_from_env, setup_dspy
    from clio_agent.gact.app import build_app
    from clio_agent.runtime.cee_worker import build_child_handler

    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) in {"lmstudio", "lm_studio"}:
        pytest.skip("live run must target Argonne/ALCF, not LM Studio (leave it free)")

    setup_dspy()
    app = build_app(agent=ClioAgent(), sessions_path=tmp_path / "sessions.json")
    app.state.user_agents.upsert(
        {
            "id": "calc",
            "title": "Calculator",
            "source": "expert_pack",
            "system_prompt": "You are a precise calculator. Answer with only the number.",
        }
    )

    handler = build_child_handler(app)
    res = await handler(ExpertRequest("calc", "What is 2 + 2? Answer with only the number."))
    assert res.status == "completed", f"{res.status} {res.error}"
    assert "4" in res.answer  # a real ALCF child, reconstructed from the wire, answered
