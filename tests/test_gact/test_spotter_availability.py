"""SPOTTER arms on a normal install, and says why not when it cannot.

The live defect: the ``spotter-ai`` pack declared its MCP launcher as
``uv run --project ${SPOTTER_IMPL_DIR} spotter-mcp --clio-config
${SPOTTER_CLIO_CONFIG}``. Neither variable is set by any install, so arming was
refused on every normal deployment ("required environment variable
${SPOTTER_IMPL_DIR} is unset"), and the mode pickers offered the option anyway.

Both values are facts clio knows: the plugin's own directory and clio's
effective provenance configuration. The ``spotter`` plugin's ``mcp.json`` now
names them as ``${PLUGIN_ROOT}`` (Agent Plugins 1.0.0) and the CLIO-supplied
``${CLIO_PROVENANCE_CONFIG}``; ``GET /v1/spotter/availability`` answers the
arming question before the click, from the same validator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from clio_agent.gact.agents.resolution import plugin_service
from clio_agent.gact.app import build_app
from clio_agent.gact.session_records import PageQuery
from clio_agent.gact.spotter_arming import (
    REFUSAL_WATCHER_PROVENANCE_UNAVAILABLE,
    REFUSAL_WATCHER_UNMOUNTABLE,
    validate_watcher_arming,
)
from clio_agent.gact.spotter_availability import (
    UNAVAILABLE_AGENT_NOT_INSTALLED,
    UNAVAILABLE_WATCHER_MCP_START_FAILED,
)
from clio_agent.gact.spotter_watcher import _live_watcher_tasks
from tests._plugin_fixtures import custom_marketplace

_AGENT_REF = "clio/spotter-ai"

#: The launcher shape the marketplace ``spotter`` plugin declares: every input
#: is supplied by the plugin runtime or by clio.
_ARGS = [
    "run",
    "--project",
    "${PLUGIN_ROOT}/impl",
    "spotter-mcp",
    "--clio-config",
    "${CLIO_PROVENANCE_CONFIG}",
]

#: The pre-fix declaration, still valid for a deployment that sets the variables.
_LEGACY_ARGS = [
    "run",
    "--project",
    "${SPOTTER_IMPL_DIR}",
    "spotter-mcp",
    "--clio-config",
    "${SPOTTER_CLIO_CONFIG}",
]

_WATCHER_AGENT = """
from clio_agent.sdk import Agent


class SpotterAI(Agent):
    id = "spotter-ai"
    version = "0.4.0"
    name = "SPOTTER"
    role = "a forensic watcher that protects the parent session while it works"
    description = "Watcher fixture for the availability tests."
    plugins = Agent.plugins + ["spotter"]
"""


def _install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str] | None = None
) -> Path:
    """Make the default marketplace carry the ``spotter-ai`` watcher and its plugin."""

    return custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "spotter": {
                "servers": {
                    "spotter": {"type": "stdio", "command": "uv", "args": list(args or _ARGS)}
                },
                "files": {"impl/pyproject.toml": '[project]\nname = "spotter-mcp"\n'},
            }
        },
        agents={"spotter_ai.py": _WATCHER_AGENT},
    )


def _plugin_root(app: Any) -> Path:
    """Where the installed ``spotter`` plugin lives (its ``${PLUGIN_ROOT}``)."""

    resolution = plugin_service(app).resolve(_AGENT_REF, pin=False)
    return Path(resolution.mcp_servers["spotter"]["root"])


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


def test_spotter_arms_on_a_normal_install_with_clio_supplied_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
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
        assert len(_live_watcher_tasks(app, sid)) == 1


def test_plugin_root_and_provenance_config_resolve_to_real_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        from clio_agent.gact.spotter_arming import _declared_watcher_servers

        servers, _raw, _cwd = _declared_watcher_servers(app, _AGENT_REF, workspace_id=wid)
        root = _plugin_root(app)

    assert (root / "plugin.json").is_file() and (root / "impl").is_dir()
    spec = servers["spotter"]
    args = spec["args"]
    assert args[2] == f"{root}/impl"
    config_path = Path(args[-1])
    assert config_path.is_file()
    # The subprocess sees the same values in its environment.
    assert spec["env"]["PLUGIN_ROOT"] == str(root)
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
    _install(tmp_path, monkeypatch)
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
    assert app.state.sessions.select(PageQuery(archived=None)) == []


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


def test_attention_handoff_is_explicit_and_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an enabled capture with a configured root reaches the watcher."""
    from types import SimpleNamespace

    from clio_agent.gact.provenance.handoff import build_provenance_handoff

    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "jsonl,flowcept")
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION", "1")
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION_FILES_DIR", str(tmp_path / "captures"))
    app = SimpleNamespace(state=SimpleNamespace())
    document = build_provenance_handoff(app, workspace_root=tmp_path).document
    assert document["provenance.attention.files_dir"] == str((tmp_path / "captures").resolve())
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION", "0")
    assert (
        "provenance.attention.files_dir"
        not in build_provenance_handoff(app, workspace_root=tmp_path).document
    )


def test_unset_variable_refusal_names_the_variable_as_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch, _LEGACY_ARGS)
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


def test_availability_is_true_when_arming_would_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        body = client.get("/v1/spotter/availability", params={"workspace_id": wid}).json()

    assert body["available"] is True
    assert body["reason"] == ""
    assert body["approval_mode"] == "spotter-ai"
    assert body["agent_ref"] == _AGENT_REF


def test_availability_reports_a_missing_watcher_agent(tmp_path: Path) -> None:
    """The hermetic default marketplace ships no ``spotter-ai`` agent."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        body = client.get("/v1/spotter/availability").json()

    assert body["available"] is False
    assert body["reason"] == UNAVAILABLE_AGENT_NOT_INSTALLED
    assert body["agent_ref"] == _AGENT_REF
    assert "install" in body["remedy"]
    assert body["message"]


def test_availability_carries_the_arming_refusal_and_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "none")
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        body = client.get("/v1/spotter/availability", params={"workspace_id": wid}).json()

    assert body["available"] is False
    assert body["reason"] == REFUSAL_WATCHER_PROVENANCE_UNAVAILABLE
    assert "provenance.agentic.providers" in body["remedy"]
    assert body["details"]["mcp_server"] == "spotter"


def test_availability_resolves_the_session_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        sid = client.post("/v1/sessions", json={"title": "t", "workspace_id": wid}).json()["id"]
        body = client.get("/v1/spotter/availability", params={"session_id": sid}).json()
        # No scope at all is the deployment-level question (session defaults):
        # every real arming names a workspace, so its absence is not a refusal.
        bare = client.get("/v1/spotter/availability").json()
        # Arming itself always requires the workspace root native lineage reads.
        unscoped = validate_watcher_arming(app, env={})

    assert body["available"] is True
    assert bare["available"] is True
    assert unscoped is not None
    assert unscoped.reason == REFUSAL_WATCHER_PROVENANCE_UNAVAILABLE
    assert "provenance_workspace_unresolved" in unscoped.detail


# --------------------------------------------------------------------------- #
# Runtime start failures (c43: available=true while the SPOTTER MCP could not start)
# --------------------------------------------------------------------------- #


@pytest.fixture
def _mount_outcomes():
    from clio_agent.gact import spotter_mount_outcome

    spotter_mount_outcome.reset()
    yield spotter_mount_outcome
    spotter_mount_outcome.reset()


def test_static_pass_says_it_is_static(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _mount_outcomes
) -> None:
    _install(tmp_path, monkeypatch)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        body = client.get("/v1/spotter/availability", params={"workspace_id": wid}).json()

    assert body["available"] is True
    assert body["details"] == {"verified": "static"}


def test_recorded_watcher_start_failure_is_a_typed_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _mount_outcomes
) -> None:
    _install(tmp_path, monkeypatch)
    _mount_outcomes.record_failure("spotter", "mcp_server_exited")
    _mount_outcomes.record_failure("unrelated", "mcp_server_exited")
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = _workspace(client, tmp_path / "ws")
        scoped = client.get("/v1/spotter/availability", params={"workspace_id": wid}).json()
        bare = client.get("/v1/spotter/availability").json()

    for body in (scoped, bare):
        assert body["available"] is False
        assert body["reason"] == UNAVAILABLE_WATCHER_MCP_START_FAILED
        assert "mcp_server_exited" in body["message"]
        assert body["remedy"]
        assert body["details"]["verified"] == "runtime"
        assert body["details"]["mcp_server"] == "spotter"


def test_a_later_successful_mount_clears_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _mount_outcomes
) -> None:
    _install(tmp_path, monkeypatch)
    _mount_outcomes.record_failure("spotter", "mcp_server_exited")
    _mount_outcomes.record_success("spotter")
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        body = client.get("/v1/spotter/availability").json()

    assert body["available"] is True


def test_unrelated_namespace_failure_does_not_affect_spotter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _mount_outcomes
) -> None:
    _install(tmp_path, monkeypatch)
    _mount_outcomes.record_failure("other", "mcp_server_exited")
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        body = client.get("/v1/spotter/availability").json()

    assert body["available"] is True


def test_mount_namespaces_records_and_clears_outcomes(_mount_outcomes) -> None:
    from clio_agent.gact import mcp_readiness

    calls = {"n": 0}

    def fake_mount(_executor, namespace, _spec, *, connect):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"t": object()}

    orig = mcp_readiness.mount_namespace_for_session
    mcp_readiness.mount_namespace_for_session = fake_mount
    try:
        _mounted, failures = mcp_readiness.mount_namespaces_for_session(
            object(), {"spotter": {}}, connect=False
        )
        assert "spotter" in failures
        assert _mount_outcomes.last_failure("spotter").reason == failures["spotter"]
        mcp_readiness.mount_namespaces_for_session(object(), {"spotter": {}}, connect=False)
        assert _mount_outcomes.last_failure("spotter") is None
    finally:
        mcp_readiness.mount_namespace_for_session = orig
