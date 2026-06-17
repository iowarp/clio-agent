"""Separate-process gact worker (epic #667, #671): reconstructs + runs a delegated child
in its own build_app, the cross-process counterpart to the in-process clio_core invoker.

The unknown-expert path is unit-testable with no LM (it never reaches a child run). The
full cross-process run of a REAL child lives in the live suite.
"""

from __future__ import annotations

from clio_agent.runtime.expert_invoker import ExpertRequest


async def test_worker_handler_unknown_expert_drains_as_failed():
    """A request for an expert NOT in this worker's registry drains as a failed result —
    never hanging the parent — and without invoking an LM (the lookup fails first)."""
    from clio_agent.gact.app import build_app
    from clio_agent.runtime.clio_core_worker import build_child_handler

    app = build_app()  # no ClioAgent needed: the unknown-expert path never runs a child
    handler = build_child_handler(app)

    res = await handler(ExpertRequest("definitely_not_a_real_expert_xyz", "are you there?"))
    assert res.status == "failed"
    assert "unknown expert" in (res.error or "")
    assert res.expert_id == "definitely_not_a_real_expert_xyz"


def test_worker_module_exposes_entrypoints():
    """The worker exposes the build + run surface a launcher / test harness drives."""
    from clio_agent.runtime import clio_core_worker

    assert callable(clio_core_worker.build_child_handler)
    assert callable(clio_core_worker.build_worker_app)
    assert callable(clio_core_worker.run_clio_core_worker)
    assert callable(clio_core_worker.run_isolated_clio_core_worker)  # the detached worker
    assert hasattr(clio_core_worker, "_main")  # python -m clio_agent.runtime.clio_core_worker


def test_isolated_delegation_store_resolves_the_agents_arc_store():
    """The production parent seam (``_invoke_child_expert``) pulls the agent's OWN ARC store
    for the detached model, so parent and external workers share one mailbox. Prove that
    resolution against a REAL build_app+ClioAgent (no LM): isolated mode yields a usable
    ARCStore; every other mode yields None (no shared-store requirement)."""
    from clio_agent.agent import ClioAgent
    from clio_agent.gact.app import build_app, isolated_delegation_store

    agent = ClioAgent()
    # production wires app.state.arc = agent.arc during lifespan startup; mirror that end state
    app = build_app(agent=agent, arc=agent.arc)
    store = isolated_delegation_store(app, "clio_core_isolated")
    # it IS the agent's persistence backend, and it's a working store (put/scan round-trips)
    assert store is app.state.arc.store
    store.put("context", "probe_xyz", b"1")
    assert any(n == "probe_xyz" for n, _ in store.scan("context", "probe_"))
    # no other mode asks for a shared store
    assert isolated_delegation_store(app, "clio_core") is None
    assert isolated_delegation_store(app, "loopback") is None
    assert isolated_delegation_store(app, "") is None


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
    mailbox transport carrying it is already proven (test_clio_core_transport)."""
    from clio_agent.agent import ClioAgent
    from clio_agent.config import load_config_from_env, setup_dspy
    from clio_agent.gact.app import build_app
    from clio_agent.runtime.clio_core_worker import build_child_handler

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
    from clio_agent.runtime.clio_core_transport import ClioCoreExpertInvoker, ClioCoreMailbox

    data_dir = tmp_path / "store"
    data_dir.mkdir()
    prefix = "clio_core_calc_"
    entry = Path(__file__).parent / "_clio_core_worker_entry.py"
    log = open(tmp_path / "worker.log", "w")  # noqa: SIM115 - closed in finally
    env = {
        **os.environ,
        "CLIO_RUN_LIVE": "1",
        "CLIO_LM_PROVIDER": "argonne",
        "CLIO_LM_API_BASE": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
        "CLIO_LM_MODEL": "openai/gpt-oss-120b",
        "CLIO_ARC_STORE": "local",
        "CLIO_ARC_DATA_DIR": str(data_dir),
        "CLIO_CORE_PREFIX": prefix,
        "CLIO_GACT_SESSIONS": str(tmp_path / "sessions.json"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "CLIO_ALLOWED_ROOTS": f"{tmp_path}:{os.getcwd()}",
    }
    proc = subprocess.Popen([sys.executable, str(entry)], env=env, stdout=log, stderr=log)
    try:
        store = make_arc_store(backend="local", data_dir=str(data_dir))
        invoker = ClioCoreExpertInvoker(ClioCoreMailbox(store, prefix=prefix), timeout=150, poll=0.1)
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
    from clio_agent.runtime.clio_core_worker import build_child_handler

    monkeypatch.setattr(appmod, "_resolve_dynamic_agent", lambda app, eid: SimpleNamespace(id=eid))

    async def boom(*_a, **_k):
        raise RuntimeError("child exploded")

    monkeypatch.setattr(appmod, "run_child_expert", boom)

    handler = build_child_handler(object())  # app unused — lookup + run are stubbed
    res = await handler(ExpertRequest("x", "q"))
    assert res.status == "failed"
    assert "child exploded" in (res.error or "")


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live ALCF run: set CLIO_RUN_LIVE=1 (and Argonne auth + CLIO_LM_* env)",
)
async def test_isolated_worker_subprocesses_over_shared_store(tmp_path):
    """The lease-free isolated model end-to-end on real ALCF: TWO worker SUBPROCESSES each
    drain their OWN queue (no claim) over a shared LocalFS store and heartbeat presence; the
    parent discovers them and routes real-ALCF delegations round-robin. No daemon, no lease,
    no claim blob ever — exactly Luke's path (a) for clio-core#559."""
    import asyncio
    import subprocess
    import sys
    from pathlib import Path

    from clio_agent.arc.storage import make_arc_store
    from clio_agent.runtime.clio_core_transport import IsolatedExpertInvoker, live_workers

    data_dir = tmp_path / "store"
    data_dir.mkdir()
    role = "calc"
    entry = Path(__file__).parent / "_clio_core_worker_entry.py"
    base_env = {
        **os.environ,
        "CLIO_RUN_LIVE": "1",
        "CLIO_LM_PROVIDER": "argonne",
        "CLIO_LM_API_BASE": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
        "CLIO_LM_MODEL": "openai/gpt-oss-120b",
        "CLIO_ARC_STORE": "local",
        "CLIO_ARC_DATA_DIR": str(data_dir),
        "CLIO_CORE_ISOLATED": "1",
        "CLIO_CORE_ROLE": role,
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "CLIO_ALLOWED_ROOTS": f"{tmp_path}:{os.getcwd()}",
    }
    procs, logs = [], []
    try:
        for wid in ("w1", "w2"):
            log = open(tmp_path / f"{wid}.log", "w")  # noqa: SIM115 - closed in finally
            logs.append(log)
            env = {
                **base_env,
                "CLIO_CORE_WORKER_ID": wid,
                "CLIO_GACT_SESSIONS": str(tmp_path / f"{wid}_sessions.json"),
            }
            procs.append(subprocess.Popen([sys.executable, str(entry)], env=env, stdout=log, stderr=log))

        store = make_arc_store(backend="local", data_dir=str(data_dir))
        for _ in range(1500):  # workers build_app + a ClioAgent before they announce presence
            if len(live_workers(store, role)) >= 2:
                break
            await asyncio.sleep(0.1)
        assert len(live_workers(store, role)) >= 2, f"workers never registered: {live_workers(store, role)}"

        invoker = IsolatedExpertInvoker(store, role=role, timeout=180, poll=0.2)
        cases = [
            ("What is 2 + 2? Answer with only the number.", "4"),
            ("What is 10 * 5? Answer with only the number.", "50"),
        ]
        for i, (q, needle) in enumerate(cases):
            res = await invoker.invoke(ExpertRequest("calc", q, session_id=f"iso{i}"))
            assert res.status == "completed", f"{q!r} -> {res.status} {res.error}"
            assert needle in res.answer, f"{q!r} -> {res.answer!r}"
        # the whole point: a real cross-process delegation pool with ZERO claim blobs
        assert not any(".claim" in n for n, _ in store.scan("context", "clio_core"))
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for log in logs:
            log.close()


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live ALCF run: set CLIO_RUN_LIVE=1 (and Argonne auth + CLIO_LM_* env)",
)
async def test_parent_hinge_delegates_via_isolated_pool_over_real_alcf(tmp_path, monkeypatch):
    """END-TO-END of the WIRED parent: the production delegation hinge a real turn calls
    (``run_child_via_boundary`` with ``store = isolated_delegation_store(app, ...)`` — exactly
    what ``_invoke_child_expert`` does for ``CLIO_EXPERT_INVOKER=clio_core_isolated``) routes a
    delegation to the DETACHED isolated worker pool. The parent runs a REAL build_app+ClioAgent
    but executes NO child itself (``run_child`` raises if touched); a separate worker PROCESS
    reconstructs + runs the child on ALCF and folds the answer back. Proves the wiring, not just
    the transport: store resolved from the live agent, routed exactly-once, zero claim blobs."""
    import asyncio
    import subprocess
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    from clio_agent.agent import ClioAgent
    from clio_agent.config import load_config_from_env, setup_dspy
    from clio_agent.gact.app import build_app, isolated_delegation_store
    from clio_agent.gact.delegation_invoker import run_child_via_boundary
    from clio_agent.runtime.clio_core_transport import live_workers

    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) in {"lmstudio", "lm_studio"}:
        pytest.skip("live run must target Argonne/ALCF, not LM Studio (leave it free)")

    # the parent's ARC store must be a shared on-disk dir the workers can attach to
    monkeypatch.setenv("CLIO_ARC_STORE", "local")
    role = "calc"
    setup_dspy()
    agent = ClioAgent(data_dir=str(tmp_path / "agent"))
    # production wires app.state.arc = agent.arc during lifespan startup; mirror that end state
    app = build_app(agent=agent, arc=agent.arc, sessions_path=tmp_path / "sessions.json")
    # resolve the shared store EXACTLY as the production parent seam does
    store = isolated_delegation_store(app, "clio_core_isolated")
    assert store is not None, "agent store did not resolve for the isolated model"
    store_dir = str(store.data_dir)  # the workers attach to this same LocalFS dir

    async def parent_run_child(agent_def, prompt):
        raise AssertionError("parent must NOT run the child — the detached pool does")

    entry = Path(__file__).parent / "_clio_core_worker_entry.py"
    base_env = {
        **os.environ,
        "CLIO_RUN_LIVE": "1",
        "CLIO_LM_PROVIDER": "argonne",
        "CLIO_LM_API_BASE": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
        "CLIO_LM_MODEL": "openai/gpt-oss-120b",
        "CLIO_ARC_STORE": "local",
        "CLIO_ARC_DATA_DIR": store_dir,
        "CLIO_CORE_ISOLATED": "1",
        "CLIO_CORE_ROLE": role,
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "CLIO_ALLOWED_ROOTS": f"{tmp_path}:{os.getcwd()}",
    }
    procs, logs = [], []
    try:
        for wid in ("w1", "w2"):
            log = open(tmp_path / f"{wid}.log", "w")  # noqa: SIM115 - closed in finally
            logs.append(log)
            env = {**base_env, "CLIO_CORE_WORKER_ID": wid,
                   "CLIO_GACT_SESSIONS": str(tmp_path / f"{wid}_sessions.json")}
            procs.append(subprocess.Popen([sys.executable, str(entry)], env=env, stdout=log, stderr=log))

        for _ in range(1500):  # workers build_app + ClioAgent before announcing presence
            if len(live_workers(store, role)) >= 1:
                break
            await asyncio.sleep(0.1)
        assert live_workers(store, role), f"no worker registered: {live_workers(store, role)}"

        # THE WIRED HINGE: same call _invoke_child_expert makes for clio_core_isolated.
        out = await run_child_via_boundary(
            SimpleNamespace(id="calc"),
            "What is 2 + 2? Answer with only the number.",
            run_child=parent_run_child,  # never called — the parent does not run the child
            session_id="iso-e2e",
            mode="clio_core_isolated",
            store=store,
            role=role,
        )
        assert "4" in str(getattr(out, "answer", "")), f"answer from detached worker: {out.answer!r}"
        # exactly-once detached model — never a claim/lease blob
        assert not any(".claim" in n for n, _ in store.scan("context", "clio_core"))
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for log in logs:
            log.close()
