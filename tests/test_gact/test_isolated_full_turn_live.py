"""LIVE end-to-end: a real delegating TURN reaches an external isolated worker fleet (#667).

This is the proof the user asked for — not the boundary call in isolation, but the FULL live
default: a POSTed user turn whose parent expert hands off to a child runs that child in a
SEPARATE worker PROCESS (the isolated fleet) on real ALCF, through the actual settle loop
(``_execute_delegated_experts`` → ``_invoke_child_expert`` → isolated transport), and folds the
real answer back into the assistant turn.

Only the PARENT's planner is scripted (so the handoff is deterministic instead of LM-flaky);
the CHILD is NOT scripted here — it runs for real in a worker subprocess where this process's
monkeypatches do not reach. The parent's ``_run_prompt_user_agent`` is even rigged to fail if
it is ever asked to run the child, so a passing run PROVES the child executed out-of-process.

Gated by ``CLIO_RUN_LIVE=1``. ALCF only (no local GPU).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CLIO_RUN_LIVE") != "1",
        reason="live ALCF run: set CLIO_RUN_LIVE=1 (and Argonne auth + CLIO_LM_* env)",
    ),
]


def _pred(**kw: Any) -> Any:
    return type("Pred", (), kw)()


async def test_real_delegating_turn_reaches_external_isolated_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.arc.memory import ARCMemory
    from clio_agent.arc.storage import make_arc_store
    from clio_agent.config import load_config_from_env
    from clio_agent.runtime.worker_fleet import (
        LocalSubprocessSpawner,
        WorkerFleet,
        WorkerSpec,
        localfs_worker_env,
    )

    from .conftest import complete_turn

    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) in {"lmstudio", "lm_studio"}:
        pytest.skip("live run must target Argonne/ALCF, not LM Studio (leave it free)")

    # --- isolated config env (mirror the expert-pack fixture) ---------------------------
    from tests.conftest import _write_test_default_registry_blueprint

    # NB: isolate only XDG_CONFIG_HOME, NOT HOME — the ALCF/Globus token the worker
    # subprocesses need lives under the real ``~/.globus`` (HOME-based). Overriding HOME hides
    # it and the workers fail to auth. (The parent uses agent=object() and never auths.)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    xdg_root = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root))
    _write_test_default_registry_blueprint(xdg_root)
    monkeypatch.chdir(workspace)

    # opt into the detached isolated model for this turn
    monkeypatch.setenv("CLIO_EXPERT_INVOKER", "clio_core_isolated")
    monkeypatch.setenv("CLIO_CORE_READY_TIMEOUT", "180")
    monkeypatch.setenv("CLIO_CORE_TIMEOUT", "240")
    monkeypatch.delenv("CLIO_CORE_ROLE", raising=False)  # role defaults to the child expert id
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", f"{tmp_path}:{os.getcwd()}")

    # --- parent (root) + child (calc) expert packs --------------------------------------
    experts = workspace / ".clio" / "experts"
    experts.mkdir(parents=True)
    experts.joinpath("root.md").write_text(
        "---\nid: root\ntitle: Root Expert\nparent_id: analysis\ntier: 2\n---\nCoordinate.\n",
        encoding="utf-8",
    )
    experts.joinpath("calc.md").write_text(
        "---\nid: calc\ntitle: Calculator\nparent_id: root\ntier: 3\n---\n"
        "You are a precise calculator. Answer with only the number.\n",
        encoding="utf-8",
    )

    # --- script ONLY the parent planner; the child must never run in THIS process --------
    async def fake_stream_unavailable(app, enriched_text, sid, emit_chunk, **kwargs):
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    calls: list[str] = []

    def fake_prompt_agent(base_agent, agent_def, question, session_id):
        del base_agent, question, session_id
        calls.append(agent_def.id)
        if agent_def.id == "calc":
            raise AssertionError("child 'calc' must run in the external worker, not the parent")
        if agent_def.id == "root" and calls.count("root") == 1:
            return _pred(
                answer="ROOT_PLAN",
                selected_expert="root",
                routing_rationale="hand off the arithmetic to calc",
                next_expert="calc",
                next_task="What is 2 + 2? Answer with only the number.",
            )
        return _pred(
            answer="ROOT_DONE",
            selected_expert="root",
            routing_rationale="parent resumed after child",
            next_expert="finish",
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    # --- parent app over a real shared ARC store (the fleet attaches to the same dir) ----
    store_dir = tmp_path / "store"
    store = make_arc_store(backend="local", data_dir=str(store_dir))
    arc = ARCMemory(data_dir=str(store_dir), store=store)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object(), arc=arc)
    assert app.state.arc.store is store  # the isolated model will route over THIS store

    # --- bring up the external worker fleet (separate processes) over that store ---------
    entry = Path(__file__).resolve().parent.parent / "test_runtime" / "_clio_core_worker_entry.py"
    fleet = WorkerFleet(
        store,
        [WorkerSpec("calc", replicas=2)],
        spawner=LocalSubprocessSpawner(
            command=[sys.executable, str(entry)], log_dir=str(tmp_path / "wlogs")
        ),
        worker_env=localfs_worker_env(store),
    )
    try:
        fleet.start(wait_ready=False)
        await fleet.wait_ready_async(timeout=180)
        assert fleet.live_counts()["calc"] >= 1

        with TestClient(app) as client:
            sid = client.post(
                "/v1/sessions",
                json={"title": "isolated delegating turn", "agent": {"id": "root"}},
            ).json()["id"]
            assistant = complete_turn(client, sid, "please add two and two", timeout=240)

        # the parent ran (twice: plan + resume) but NEVER ran the child locally
        assert calls.count("root") >= 1
        assert "calc" not in calls

        # the settle loop delegated to calc, the EXTERNAL worker answered, and it folded back
        handoffs = assistant["metadata"]["expert_handoffs"]
        flat: list[dict[str, Any]] = []
        stack = list(handoffs)
        while stack:
            row = stack.pop(0)
            flat.append(row)
            kids = row.get("children")
            if isinstance(kids, list):
                stack.extend(k for k in kids if isinstance(k, dict))
        calc_handoff = next(r for r in flat if r.get("agent_id") == "calc")
        assert calc_handoff["status"] == "completed", calc_handoff
        assert calc_handoff["parent_id"] == "root"
        assert "4" in str(calc_handoff.get("output_summary", "")), calc_handoff
        # the isolated model never writes a claim/lease blob
        assert not any(".claim" in n for n, _ in store.scan("context", "clio_core_"))
    finally:
        fleet.stop()
