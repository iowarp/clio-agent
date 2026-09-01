"""iowarp/clio-agent — spotter-ai ARM-TIME executability validation.

Live defect (sessions ``sess_71d5473bda17`` / ``sess_a35cd5416d46``): arming a
session into ``spotter-ai`` reported success while the watcher could never
execute. The ``spotter-ai`` Agent Blueprint declares its MCP server as
``uv run --project ${SPOTTER_IMPL_DIR} ... --clio-config ${SPOTTER_CLIO_CONFIG}``;
with those deployment variables unset the declaration does not resolve, the
watcher child mounts ZERO ``spotter_*`` tools and errors on every wake, and the
fail-closed clearance barrier then auto-denies EVERY destructive tool call with
``spotter_watcher_check_failed`` — an armed session was a total write lockout
whose first operator signal was a denial storm.

These tests pin the arm-time gate that closes it: a STATIC resolvability check
of the watcher blueprint's declared MCP specs (never a launch, never a
handshake) that REFUSES the transition into spotter-ai with a typed HTTP 422
instead of arming a watcher that cannot run.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.spotter_arming import (
    REFUSAL_WATCHER_PROJECT_MISSING,
    REFUSAL_WATCHER_UNMOUNTABLE,
    SPOTTER_ARMING_REASONS,
    _project_directories,
    validate_watcher_arming,
)

_BLUEPRINT_ID = "spotter-ai"

_AGENT_MD = """---
id: spotter-ai
version: 0.3.0
title: SPOTTER AI
description: Live anomaly surveillance for the arming gate tests.
root_expert: spotter_watcher
mcp_servers:
  spotter:
    command: uv
    args:
      - run
      - --project
      - ${SPOTTER_IMPL_DIR}
      - --no-sync
      - spotter-mcp
      - --clio-config
      - ${SPOTTER_CLIO_CONFIG}
experts:
  - experts/spotter_watcher.md
---

SPOTTER AI watcher blueprint fixture.
"""

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


def _install_watcher_blueprint(tmp_path: Path) -> Path:
    """Install the watcher Agent Blueprint into this test's isolated config root.

    ``tests/conftest.py::allow_pytest_tmp_path`` repoints ``CLIO_USER_DIR`` /
    ``XDG_CONFIG_HOME`` at ``tmp_path/xdg``, so this is the same global
    ``agent-blueprints`` root ``discover_agent_blueprints`` scans.
    """

    root = tmp_path / "xdg" / "clio-agent" / "agent-blueprints" / _BLUEPRINT_ID
    (root / "experts").mkdir(parents=True, exist_ok=True)
    root.joinpath("AGENT.md").write_text(_AGENT_MD, encoding="utf-8")
    root.joinpath("experts", "spotter_watcher.md").write_text(_WATCHER_EXPERT_MD, encoding="utf-8")
    return root


def _clear_deployment_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the pack's deployment inputs (the reproduced live topology)."""

    monkeypatch.delenv("SPOTTER_IMPL_DIR", raising=False)
    monkeypatch.delenv("SPOTTER_CLIO_CONFIG", raising=False)


def _set_deployment_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Set the pack's deployment inputs to a real impl dir + clio config file."""

    impl = tmp_path / "spotter-impl"
    impl.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "clio.yaml"
    config.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setenv("SPOTTER_IMPL_DIR", str(impl))
    monkeypatch.setenv("SPOTTER_CLIO_CONFIG", str(config))
    return impl


# --------------------------------------------------------------------------- #
# 1. CREATE: an unexecutable watcher refuses the transition, typed 422
# --------------------------------------------------------------------------- #


def test_create_into_spotter_ai_is_refused_when_declared_mcp_env_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_watcher_blueprint(tmp_path)
    _clear_deployment_env(monkeypatch)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        with caplog.at_level(logging.WARNING, logger="clio_agent.gact.spotter_arming"):
            resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["error"] == REFUSAL_WATCHER_UNMOUNTABLE == "spotter_watcher_unmountable"
    details = error["details"]
    assert details["reason"] == REFUSAL_WATCHER_UNMOUNTABLE
    assert details["agent_blueprint_id"] == _BLUEPRINT_ID
    assert details["mcp_server"] == "spotter"
    # The operator gets the actionable half: WHICH variable is unset.
    assert details["environment_variable"] == "SPOTTER_IMPL_DIR"
    assert "SPOTTER_IMPL_DIR" in details["detail"]
    assert error["message"]

    # Nothing was armed, and the refused create left NO session behind.
    assert app.state.sessions.list() == []
    assert app.state.agent_task_registry.for_parent("") == []
    # The refusal reaches the trace/log with its typed reason.
    assert any(
        "spotter_watcher_arm_refused" in record.message
        and REFUSAL_WATCHER_UNMOUNTABLE in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_create_into_spotter_ai_is_refused_when_project_dir_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec RESOLVES but ``--project`` names a directory that is not there."""

    _install_watcher_blueprint(tmp_path)
    monkeypatch.setenv("SPOTTER_IMPL_DIR", str(tmp_path / "not-installed"))
    monkeypatch.setenv("SPOTTER_CLIO_CONFIG", str(tmp_path / "clio.yaml"))

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["error"] == REFUSAL_WATCHER_PROJECT_MISSING == "spotter_watcher_project_missing"
    assert error["details"]["path"] == str(tmp_path / "not-installed")
    assert error["details"]["mcp_server"] == "spotter"
    assert app.state.sessions.list() == []


# --------------------------------------------------------------------------- #
# 2. A resolvable deployment arms EXACTLY as before
# --------------------------------------------------------------------------- #


def test_create_into_spotter_ai_arms_when_the_declaration_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_watcher_blueprint(tmp_path)
    _set_deployment_env(monkeypatch, tmp_path)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 200, resp.text
    sid = resp.json()["id"]
    tasks = app.state.agent_task_registry.for_parent(sid)
    assert len(tasks) == 1, tasks
    assert tasks[0].agent_ref["expert_id"] == "spotter_watcher"
    assert tasks[0].live_state == "waiting"


def test_arming_without_an_installed_watcher_blueprint_is_unchanged(tmp_path: Path) -> None:
    """No blueprint installed -> nothing declared -> nothing to resolve.

    The gate refuses only a DECLARED-but-unresolvable server; a deployment that
    declares none arms exactly as it did before (the shape every other
    ``test_spotter_watcher.py`` case relies on).
    """

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 200, resp.text
    assert len(app.state.agent_task_registry.for_parent(resp.json()["id"])) == 1


# --------------------------------------------------------------------------- #
# 3. PATCH into spotter-ai gets the SAME gate (and stays in its prior mode)
# --------------------------------------------------------------------------- #


def test_patch_into_spotter_ai_gets_the_same_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_watcher_blueprint(tmp_path)
    _clear_deployment_env(monkeypatch)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        resp = client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "spotter-ai"})

        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["error"] == REFUSAL_WATCHER_UNMOUNTABLE
        assert resp.json()["error"]["details"]["session_id"] == sid

        # The refused transition did not stick: the session keeps its prior mode
        # and no watcher was armed (so the clearance barrier never engages).
        assert app.state.sessions.get(sid).approval_mode == "ask"
        assert client.get(f"/v1/sessions/{sid}").json()["approval_mode"] == "ask"
        assert app.state.agent_task_registry.for_parent(sid) == []


def test_patch_into_spotter_ai_arms_when_the_declaration_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_watcher_blueprint(tmp_path)
    _set_deployment_env(monkeypatch, tmp_path)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        resp = client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "spotter-ai"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["approval_mode"] == "spotter-ai"
        assert len(app.state.agent_task_registry.for_parent(sid)) == 1


def test_patch_away_from_spotter_ai_is_never_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving the mode must always be possible — even if the deployment broke
    after arming, DISARM can never be refused (that would strand the lockout)."""

    _install_watcher_blueprint(tmp_path)
    _set_deployment_env(monkeypatch, tmp_path)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"}
        ).json()["id"]
        assert len(app.state.agent_task_registry.for_parent(sid)) == 1

        _clear_deployment_env(monkeypatch)
        resp = client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "ask"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["approval_mode"] == "ask"


def test_unrelated_patch_on_an_armed_session_is_never_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate fires on the TRANSITION only. A PATCH that does not re-arm (a
    rename) must keep working even if the deployment broke after arming."""

    _install_watcher_blueprint(tmp_path)
    _set_deployment_env(monkeypatch, tmp_path)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"}
        ).json()["id"]
        task_id = app.state.agent_task_registry.for_parent(sid)[0].task_id

        _clear_deployment_env(monkeypatch)
        resp = client.patch(f"/v1/sessions/{sid}", json={"title": "renamed"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "renamed"
        assert resp.json()["approval_mode"] == "spotter-ai"
        tasks = app.state.agent_task_registry.for_parent(sid)
        assert [row.task_id for row in tasks] == [task_id]


# --------------------------------------------------------------------------- #
# 4. The validator itself: static, deterministic, env-injected (no os.environ)
# --------------------------------------------------------------------------- #


def test_validate_watcher_arming_reads_an_injected_env(tmp_path: Path) -> None:
    _install_watcher_blueprint(tmp_path)
    app = build_app(sessions_path=tmp_path / "s.json")
    impl = tmp_path / "spotter-impl"
    impl.mkdir()

    unset = validate_watcher_arming(app, env={})
    assert unset is not None
    assert unset.reason == REFUSAL_WATCHER_UNMOUNTABLE
    assert unset.variable == "SPOTTER_IMPL_DIR"
    assert unset.server == "spotter"
    assert unset.blueprint_id == _BLUEPRINT_ID

    # The FIRST unresolved variable is reported; fixing it surfaces the next.
    second = validate_watcher_arming(app, env={"SPOTTER_IMPL_DIR": str(impl)})
    assert second is not None
    assert second.variable == "SPOTTER_CLIO_CONFIG"

    resolved = validate_watcher_arming(
        app,
        env={"SPOTTER_IMPL_DIR": str(impl), "SPOTTER_CLIO_CONFIG": str(tmp_path / "clio.yaml")},
    )
    assert resolved is None


def test_validate_watcher_arming_flags_a_missing_project_dir(tmp_path: Path) -> None:
    _install_watcher_blueprint(tmp_path)
    app = build_app(sessions_path=tmp_path / "s.json")

    refusal = validate_watcher_arming(
        app,
        env={
            "SPOTTER_IMPL_DIR": str(tmp_path / "gone"),
            "SPOTTER_CLIO_CONFIG": str(tmp_path / "clio.yaml"),
        },
    )
    assert refusal is not None
    assert refusal.reason == REFUSAL_WATCHER_PROJECT_MISSING
    assert refusal.path == str(tmp_path / "gone")
    assert refusal.variable == ""


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["run", "--project", "/impl", "spotter-mcp"], ["/impl"]),
        (["run", "--project=/impl"], ["/impl"]),
        (["run", "--directory", "/impl"], ["/impl"]),
        # A dangling flag has no value: unspawnable, and reported, not dropped.
        (["run", "--project"], [""]),
        # Nothing that is not a declared directory flag is guessed at.
        (["run", "--no-sync", "spotter-mcp", "--clio-config", "/etc/clio.yaml"], []),
    ],
)
def test_project_directory_scan_only_reads_declared_directory_flags(
    args: list[str], expected: list[str]
) -> None:
    assert _project_directories(args) == expected


def test_every_refusal_reason_is_in_the_closed_set() -> None:
    assert set(SPOTTER_ARMING_REASONS) == {
        REFUSAL_WATCHER_UNMOUNTABLE,
        REFUSAL_WATCHER_PROJECT_MISSING,
    }
    assert all(SPOTTER_ARMING_REASONS.values())


def test_validate_watcher_arming_is_a_no_op_without_a_declared_server(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    assert validate_watcher_arming(app, env={}) is None
