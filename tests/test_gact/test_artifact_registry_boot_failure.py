"""A failed artifact-registry boot leaves the agent failed with its reason, never "starting".

Ported from the startup-fixes work (51750c91): the boot fold's only handled failure was a
stalled store; any other exception escaped the construction task with nothing recorded, so
readiness read "starting" forever and messages answered 503 with no reason.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from clio_agent.gact.artifacts import registry_boot


def test_any_other_boot_failure_is_recorded_then_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: remove the ``except Exception`` block -> agent_init_error stays None -> red."""

    def broken(_app: object) -> None:
        raise RuntimeError("registry records unreadable")

    monkeypatch.setattr(registry_boot, "rebuild_registry_at_boot", broken)
    monkeypatch.setattr(
        "clio_agent.gact.artifacts.ingest_edges.install_egress_recorder", lambda _app: None
    )
    monkeypatch.setattr("clio_agent.gact.runtime.grants.install_egress_gate", lambda _app: None)
    monkeypatch.setattr(
        "clio_agent.gact.runtime.grants.replay_persisted_root_grants", lambda _app: None
    )
    app = SimpleNamespace(state=SimpleNamespace(agent=None, agent_init_error=None))

    async def run() -> bool:
        return await registry_boot.boot_fold_artifact_registry_offloop(
            app, asyncio.get_running_loop()
        )

    with pytest.raises(RuntimeError, match="unreadable"):
        asyncio.run(run())
    assert "registry records unreadable" in str(app.state.agent_init_error)
