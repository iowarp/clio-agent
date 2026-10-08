"""Host facts, Windows acquisition ownership and provider-check presentation regressions."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.config import LMProviderConfig
from clio_agent.gact.infrastructure import node_models
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    InfrastructureTarget,
    SshRoute,
    TargetFacts,
)
from clio_agent.gact.infrastructure.probe import probe_target
from clio_agent.gact.infrastructure.storage import resolved_locations
from clio_agent.gact.routes.infrastructure import register_infrastructure_routes
from clio_agent.providers.handshake import cache
from clio_agent.providers.handshake.model import AuthState, ConnectivityState, HandshakeReport
from clio_agent.runtime.lm_provider_probe import _probe_codex_direct
from tests.test_gact.test_model_acquisition import job


def test_ssh_display_sequences_cannot_become_storage_paths() -> None:
    """Replay real CSI/OSC terminal bytes, keeping the host's actual data root."""
    target = InfrastructureTarget(
        id="ares",
        label="Ares",
        kind="ssh",
        transport_state="connected",
        ssh=SshRoute(host="ares.example", platform="linux"),
    )

    async def execute(spec: CommandSpec) -> CommandResult:
        return CommandResult(
            exit_code=0,
            stdout="Linux|x86_64|none|0|0|1\n"
            "clio-agent-data|/home/alice/.local/share/clio-agent\x1b[2;1H\x1b]0;terminal\x07\n",
        )

    facts = asyncio.run(probe_target(target, execute))
    assert facts.agent_data_root == "/home/alice/.local/share/clio-agent"
    assert resolved_locations(target, facts).models == "/home/alice/.local/share/clio-agent/models"


def test_invalid_host_path_is_actionable_without_pydantic_trace() -> None:
    """Other control characters stay rejected rather than silently changing a path."""
    target = InfrastructureTarget(id="bad", label="Bad", kind="ssh")
    facts = TargetFacts(
        target_id="bad", label="Bad", os="linux", arch="x86_64", agent_data_root="/data\x01"
    )
    with pytest.raises(ValueError, match="Inspect it again or choose a storage root"):
        resolved_locations(target, facts)


def test_download_control_uses_real_host_identity_and_reuses_owned_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real OS lock and identity, recording only the test child-launch seam."""
    root = tmp_path / "operations"
    root.mkdir()
    calls: list[dict[str, Any]] = []

    def spawn(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append({"argv": argv, **kwargs})
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(node_models.subprocess, "Popen", spawn)
    request = {
        "repository": "org/model",
        "revision": "main",
        "destination": str(tmp_path / "model with spaces"),
    }
    first = node_models.start(root, request, "# test-only inert launch recorder")
    second = node_models.start(root, request, "# same source")
    assert first["id"] == second["id"] and len(calls) == 1
    assert calls[0]["start_new_session"] == (sys.platform != "win32")
    assert node_models.process_identity(os.getpid())
    assert not node_models.alive({"pid": os.getpid(), "process_identity": "a different process"})
    assert node_models.inspect_jobs(root)[0]["state"] == "queued"
    assert (root / first["id"] / "receipt.json").exists()


def test_worker_cancel_checkpoint_preserves_cached_bytes(tmp_path: Path) -> None:
    """A receipt becomes cancelled only after its own worker observes the marker."""
    receipt = tmp_path / "receipt.json"
    partial = tmp_path / "partial-model"
    partial.write_bytes(b"keep these cached bytes")
    row = job(tmp_path)
    node_models.write_json(receipt, row)
    assert not node_models.cancel_checkpoint(receipt, row)
    (tmp_path / "cancel").touch()
    assert node_models.cancel_checkpoint(receipt, row)
    assert json.loads(receipt.read_text())["state"] == "cancelled"
    assert partial.read_bytes() == b"keep these cached bytes"


def test_local_windows_model_route_uses_own_python_and_windows_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept Windows acquisition through the real routes, without running a model worker."""
    from clio_agent.gact.routes import infrastructure_models

    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))

    async def facts(target: InfrastructureTarget, execute: Any = None) -> TargetFacts:
        return TargetFacts(
            target_id=target.id,
            label=target.label,
            os="windows",
            arch="x86_64",
            transport_state="connected",
            uv_available=True,
            agent_data_root="C:\\CLIO data",
        )

    monkeypatch.setattr(infrastructure_models, "probe_target", facts)
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path / "infrastructure")
    calls: list[CommandSpec] = []

    async def execute(target_id: str, spec: CommandSpec) -> CommandResult:
        assert target_id == "local"
        calls.append(spec)
        body = json.loads(spec.stdin)
        row = job(body["destination"], state="queued")
        return CommandResult(exit_code=0, stdout="\x1b[2;1H" + json.dumps(row))

    monkeypatch.setattr(app.state.infrastructure_runtime, "execute_on_target", execute)
    with TestClient(app) as client:
        response = client.post(
            "/v1/infrastructure/targets/local/models",
            json={"repository": "org/model", "revision": "main"},
        )
        assert response.status_code == 202, response.text
        assert calls[0].program == sys.executable
        assert (
            json.loads(calls[0].stdin)["destination"] == "C:\\CLIO data\\models\\org--model--main"
        )


def test_codex_health_uses_fresh_verified_handshake_without_new_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful provider check changes the warning; deferred/static evidence cannot."""
    from clio_agent.providers.codex import credentials

    monkeypatch.setattr(credentials, "direct_signed_in", lambda: True)
    monkeypatch.setattr(cache, "_cache", {})
    config = LMProviderConfig(provider="codex", api_base="codex://direct", model="gpt-6-luna")
    key = cache.cache_key("codex", config.api_base)
    report = HandshakeReport(
        provider_id="codex",
        provider_kind="subscription",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
    )
    cache.put_cached(key, report)
    assert _probe_codex_direct(config, "default:codex", "subscription").state.value == "ready"
    cache.put_cached(key, replace(report, models_source="static"))
    assert _probe_codex_direct(config, "default:codex", "subscription").state.value == "degraded"
    cache.invalidate(key)
    row = _probe_codex_direct(config, "default:codex", "subscription")
    assert "Provider setup" in row.next_action
