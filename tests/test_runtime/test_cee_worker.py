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


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live ALCF run: set CLIO_RUN_LIVE=1 (and Argonne auth + CLIO_LM_* env)",
)
async def test_worker_subprocess_runs_child_over_shared_store(tmp_path):
    """FULL untangle: a real gact child runs in a SEPARATE OS process. The parent submits a
    delegation to a shared LocalFS store and runs NO worker itself; a worker SUBPROCESS (its
    own build_app) reconstructs + runs the child on ALCF and publishes back. No daemon -> not
    blocked by the cross-process wedge. Proof it crossed processes: the parent runs no handler,
    so only the subprocess could have produced the answer."""
    import subprocess
    import sys
    from pathlib import Path

    from clio_agent.arc.storage import make_arc_store
    from clio_agent.runtime.cee_transport import CEEExpertInvoker, CEEMailbox

    data_dir = tmp_path / "store"
    data_dir.mkdir()
    prefix = "cee_calc_"
    entry = Path(__file__).parent / "_cee_worker_entry.py"
    log = open(tmp_path / "worker.log", "w")  # noqa: SIM115 - closed in finally
    env = {
        **os.environ,
        "CLIO_RUN_LIVE": "1",
        "CLIO_LM_PROVIDER": "argonne",
        "CLIO_LM_API_BASE": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
        "CLIO_LM_MODEL": "openai/gpt-oss-120b",
        "CLIO_ARC_STORE": "local",
        "CLIO_ARC_DATA_DIR": str(data_dir),
        "CLIO_CEE_PREFIX": prefix,
        "CLIO_GACT_SESSIONS": str(tmp_path / "sessions.json"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "CLIO_ALLOWED_ROOTS": f"{tmp_path}:{os.getcwd()}",
    }
    proc = subprocess.Popen([sys.executable, str(entry)], env=env, stdout=log, stderr=log)
    try:
        store = make_arc_store(backend="local", data_dir=str(data_dir))
        invoker = CEEExpertInvoker(CEEMailbox(store, prefix=prefix), timeout=150, poll=0.1)
        res = await invoker.invoke(
            ExpertRequest("calc", "What is 2 + 2? Answer with only the number.", session_id="xproc")
        )
        assert res.status == "completed", f"{res.status} {res.error}"
        assert "4" in res.answer  # answered by a DIFFERENT process over a shared store
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


async def test_worker_handler_drains_a_raising_child_as_failed(monkeypatch):
    """Defense-in-depth: a child that RAISES drains as a failed result from the handler
    itself (not propagating), so the guarantee holds even outside serve_one's containment."""
    from types import SimpleNamespace

    import clio_agent.gact.app as appmod
    from clio_agent.runtime.cee_worker import build_child_handler

    monkeypatch.setattr(appmod, "_resolve_dynamic_agent", lambda app, eid: SimpleNamespace(id=eid))

    async def boom(*_a, **_k):
        raise RuntimeError("child exploded")

    monkeypatch.setattr(appmod, "run_child_expert", boom)

    handler = build_child_handler(object())  # app unused — lookup + run are stubbed
    res = await handler(ExpertRequest("x", "q"))
    assert res.status == "failed"
    assert "child exploded" in (res.error or "")
