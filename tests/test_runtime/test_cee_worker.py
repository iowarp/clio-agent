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
