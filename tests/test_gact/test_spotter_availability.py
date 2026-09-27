"""SPOTTER arms on a normal install, and says why not when it cannot.

The live defect: the ``spotter-ai`` pack declared its MCP launcher as
``uv run --project ${SPOTTER_IMPL_DIR} spotter-mcp --clio-config
${SPOTTER_CLIO_CONFIG}``. Neither variable is set by any install, so arming was
refused on every normal deployment ("required environment variable
${SPOTTER_IMPL_DIR} is unset"), and the mode pickers offered the option anyway.

Both values are facts clio knows: the pack's own directory and clio's effective
provenance configuration. clio now supplies them as ``${CLIO_BLUEPRINT_DIR}``
and ``${CLIO_PROVENANCE_CONFIG}``; ``GET /v1/spotter/availability`` answers the
arming question before the click, from the same validator.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.spotter_arming import (
    REFUSAL_WATCHER_PROVENANCE_UNAVAILABLE,
    REFUSAL_WATCHER_UNMOUNTABLE,
    validate_watcher_arming,
)
from clio_agent.gact.spotter_availability import UNAVAILABLE_BLUEPRINT_NOT_INSTALLED

_BLUEPRINT_ID = "spotter-ai"

#: The launcher shape the marketplace pack now declares: every input is clio-supplied.
_AGENT_MD = """---
id: spotter-ai
version: 0.4.0
title: SPOTTER AI
description: Watcher fixture for the availability tests.
root_expert: spotter_watcher
mcp_servers:
  spotter:
    command: uv
    args:
      - run
      - --project
      - ${CLIO_BLUEPRINT_DIR}/impl
      - --no-sync
      - spotter-mcp
      - --clio-config
      - ${CLIO_PROVENANCE_CONFIG}
experts:
  - experts/spotter_watcher.md
---

SPOTTER AI watcher blueprint fixture.
"""

#: The pre-fix declaration, still valid for a deployment that sets the variables.
_LEGACY_AGENT_MD = _AGENT_MD.replace("${CLIO_BLUEPRINT_DIR}/impl", "${SPOTTER_IMPL_DIR}").replace(
    "${CLIO_PROVENANCE_CONFIG}", "${SPOTTER_CLIO_CONFIG}"
)

_WATCHER_EXPERT_MD = """---
id: spotter_watcher
title: SPOTTER Forensic Watcher
description: Watches live campaign activity.
tier: 1
module:
  kind: react
tools:
  - spotter_capabilities
---

You protect the parent session while it works.
"""


def _install(tmp_path: Path, agent_md: str = _AGENT_MD, *, synced: bool = True) -> Path:
    """Install the watcher blueprint (with a synced ``impl`` venv) in the test config root."""

    root = tmp_path / "xdg" / "clio-agent" / "agent-blueprints" / _BLUEPRINT_ID
    (root / "experts").mkdir(parents=True, exist_ok=True)
    root.joinpath("AGENT.md").write_text(agent_md, encoding="utf-8")
    root.joinpath("experts", "spotter_watcher.md").write_text(_WATCHER_EXPERT_MD, encoding="utf-8")
    if synced:
        venv = root / "impl" / ".venv"
        for bindir in (venv / "Scripts", venv / "bin"):
            bindir.mkdir(parents=True, exist_ok=True)
            bindir.joinpath("python.exe" if os.name == "nt" else "python").write_bytes(b"")
            suffix = ".exe" if bindir.name == "Scripts" else ""
            bindir.joinpath(f"spotter-mcp{suffix}").write_bytes(b"")
        for site in (venv / "Lib" / "site-packages", venv / "lib" / "python3.12" / "site-packages"):
            site.mkdir(parents=True, exist_ok=True)
            site.joinpath("_marker.pth").write_text("", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _no_deployment_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal install sets none of the pack's legacy deployment variables."""

    monkeypatch.delenv("SPOTTER_IMPL_DIR", raising=False)
    monkeypatch.delenv("SPOTTER_CLIO_CONFIG", raising=False)
    monkeypatch.delenv("CLIO_PROVENANCE_PROVIDERS", raising=False)


def _workspace(client: TestClient, root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    return client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]


# --------------------------------------------------------------------------- #
# Arming on a normal install
# --------------------------------------------------------------------------- #


def test_spotter_arms_on_a_normal_install_with_clio_supplied_inputs(tmp_path: Path) -> None:
    _install(tmp_path)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        resp = client.post(
            "/v1/sessions",
            json={"title": "t", "approval_mode": "spotter-ai", "workspace_id": wid},
        )
        assert resp.status_code in (200, 201), resp.text
        sid = resp.json()["id"]
        assert resp.json()["approval_mode"] == "spotter-ai"
        assert len(app.state.agent_task_registry.for_parent(sid)) == 1


def test_blueprint_dir_and_provenance_config_resolve_to_real_paths(tmp_path: Path) -> None:
    root = _install(tmp_path)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        from clio_agent.gact.spotter_arming import _declared_watcher_servers

        servers, _raw, _cwd = _declared_watcher_servers(app, _BLUEPRINT_ID, workspace_id=wid)

    spec = servers["spotter"]
    args = spec["args"]
    assert args[2] == f"{root}/impl"
    config_path = Path(args[-1])
    assert config_path.is_file()
    # The subprocess sees the same values in its environment.
    assert spec["env"]["CLIO_BLUEPRINT_DIR"] == str(root)
    assert spec["env"]["CLIO_PROVENANCE_CONFIG"] == str(config_path)

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    agentic = document["provenance"]["agentic"]
    assert agentic["providers"] == ["jsonl"]
    assert agentic["query_default"] == "native"
    # The journal clio actually writes, not an unset config key.
    assert Path(agentic["jsonl"]["path"]) == Path(app.state.semantic_trace_backend.replay_paths[0])
    artifacts = document["provenance"]["artifacts"]
    assert artifacts["provider"] == "native"
    assert Path(artifacts["native"]["workspace_root"]) == tmp_path / "ws"


def test_disabled_provenance_refuses_arming_with_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path)
    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "none")
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        resp = client.post(
            "/v1/sessions",
            json={"title": "t", "approval_mode": "spotter-ai", "workspace_id": wid},
        )

    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["error"] == REFUSAL_WATCHER_PROVENANCE_UNAVAILABLE
    assert "provenance_agentic_disabled" in error["details"]["detail"]
    assert "provenance.agentic.providers" in error["details"]["remedy"]
    assert app.state.sessions.list() == []


def test_flowcept_query_default_without_settings_is_a_typed_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Projected directly: building an app with Flowcept needs the optional extra."""

    from types import SimpleNamespace

    from clio_agent.gact.provenance.handoff import build_provenance_handoff

    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "jsonl,flowcept")
    monkeypatch.setenv("CLIO_PROVENANCE_QUERY_DEFAULT", "flowcept")
    monkeypatch.delenv("FLOWCEPT_SETTINGS_PATH", raising=False)
    app = SimpleNamespace(
        state=SimpleNamespace(
            semantic_trace_backend=SimpleNamespace(replay_paths=(tmp_path / "traces",)),
            artifact_provenance_backend=SimpleNamespace(provider_name="native"),
        )
    )

    missing = build_provenance_handoff(app, workspace_root=tmp_path)
    assert [problem.code for problem in missing.problems] == [
        "provenance_flowcept_settings_missing"
    ]
    assert "flowcept.settings_path" in missing.problems[0].remedy

    settings = tmp_path / "flowcept.yaml"
    monkeypatch.setenv("FLOWCEPT_SETTINGS_PATH", str(settings))
    present = build_provenance_handoff(app, workspace_root=tmp_path)
    assert present.usable
    agentic = present.document["provenance"]["agentic"]
    assert agentic["flowcept"] == {"settings_path": str(settings)}
    assert agentic["query_default"] == "flowcept"


def test_unset_variable_refusal_names_the_variable_as_the_remedy(tmp_path: Path) -> None:
    _install(tmp_path, _LEGACY_AGENT_MD)
    app = build_app(sessions_path=tmp_path / "s.json")

    refusal = validate_watcher_arming(app, env={})

    assert refusal is not None
    assert refusal.reason == REFUSAL_WATCHER_UNMOUNTABLE
    assert refusal.variable == "SPOTTER_IMPL_DIR"
    assert "SPOTTER_IMPL_DIR" in refusal.remedy
    assert refusal.details()["remedy"] == refusal.remedy


# --------------------------------------------------------------------------- #
# GET /v1/spotter/availability
# --------------------------------------------------------------------------- #


def test_availability_is_true_when_arming_would_succeed(tmp_path: Path) -> None:
    _install(tmp_path)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        body = client.get("/v1/spotter/availability", params={"workspace_id": wid}).json()

    assert body["available"] is True
    assert body["reason"] == ""
    assert body["approval_mode"] == "spotter-ai"
    assert body["agent_blueprint_id"] == _BLUEPRINT_ID


def test_availability_reports_a_missing_watcher_blueprint(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        body = client.get("/v1/spotter/availability").json()

    assert body["available"] is False
    assert body["reason"] == UNAVAILABLE_BLUEPRINT_NOT_INSTALLED
    assert "install" in body["remedy"]
    assert body["message"]


def test_availability_carries_the_arming_refusal_and_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path)
    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "none")
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        body = client.get("/v1/spotter/availability", params={"workspace_id": wid}).json()

    assert body["available"] is False
    assert body["reason"] == REFUSAL_WATCHER_PROVENANCE_UNAVAILABLE
    assert "provenance.agentic.providers" in body["remedy"]
    assert body["details"]["mcp_server"] == "spotter"


def test_availability_resolves_the_session_workspace(tmp_path: Path) -> None:
    _install(tmp_path)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        sid = client.post("/v1/sessions", json={"title": "t", "workspace_id": wid}).json()["id"]
        body = client.get("/v1/spotter/availability", params={"session_id": sid}).json()
        # Without any workspace, native artifact lineage has no root: unavailable, typed.
        bare = client.get("/v1/spotter/availability").json()

    assert body["available"] is True
    assert bare["available"] is False
    assert bare["reason"] == REFUSAL_WATCHER_PROVENANCE_UNAVAILABLE
    assert "provenance_workspace_unresolved" in bare["details"]["detail"]
