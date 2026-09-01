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

Second live defect (``sess_086cf23a960b``): the gate stat'd the launcher's
``--project`` DIRECTORY but never its ENTRY POINT. With ``SPOTTER_IMPL_DIR``
pointed at a real directory whose venv had been created but never synced (a
bare ``python.exe``, no ``site-packages``), the gate PASSED, ``uv run --project
<dir> --no-sync spotter-mcp`` then failed "program not found", and the watcher
errored ``custom_agent_tools_unavailable`` on every wake — the exact denial
storm the gate exists to prevent. The entry-point cases below pin that closure.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.spotter_arming import (
    REFUSAL_WATCHER_ENTRYPOINT_MISSING,
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

#: Same launcher WITHOUT ``--no-sync``: ``uv run`` provisions the environment
#: itself before exec, so its venv contents are not a static precondition.
_AGENT_MD_SYNC_ENABLED = _AGENT_MD.replace("      - --no-sync\n", "")

#: A launcher that is not ``uv run`` at all — the shape the static entry-point
#: resolution deliberately does not model.
_AGENT_MD_NON_UV = _AGENT_MD.replace("    command: uv\n", "    command: node\n").replace(
    """      - run
      - --project
      - ${SPOTTER_IMPL_DIR}
      - --no-sync
      - spotter-mcp
""",
    """      - --project
      - ${SPOTTER_IMPL_DIR}
      - server.js
""",
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


def _install_watcher_blueprint(tmp_path: Path, agent_md: str = _AGENT_MD) -> Path:
    """Install the watcher Agent Blueprint into this test's isolated config root.

    ``tests/conftest.py::allow_pytest_tmp_path`` repoints ``CLIO_USER_DIR`` /
    ``XDG_CONFIG_HOME`` at ``tmp_path/xdg``, so this is the same global
    ``agent-blueprints`` root ``discover_agent_blueprints`` scans.
    """

    root = tmp_path / "xdg" / "clio-agent" / "agent-blueprints" / _BLUEPRINT_ID
    (root / "experts").mkdir(parents=True, exist_ok=True)
    root.joinpath("AGENT.md").write_text(agent_md, encoding="utf-8")
    root.joinpath("experts", "spotter_watcher.md").write_text(_WATCHER_EXPERT_MD, encoding="utf-8")
    return root


def _install_impl_venv(
    impl: Path, *, entrypoint: str = "spotter-mcp", synced: bool = True, distribution: str = ""
) -> Path:
    """Build the on-disk shape ``uv run --no-sync`` needs, with plain dirs/files.

    No real ``uv`` runs here: the check is a stat, so the fixture only has to
    reproduce the LAYOUT — ``.venv/{Scripts,bin}`` for the console script and
    ``.venv/{Lib,lib/pythonX.Y}/site-packages`` for the distribution fallback.
    Both platform layouts are laid down so the assertions do not fork on
    ``os.name`` (the resolver probes both for the same reason).

    Args:
        impl: The project directory the launcher's ``--project`` names.
        entrypoint: The console script to install; empty installs none.
        synced: When false the venv exists but carries NO ``site-packages`` —
            the live ``sess_086cf23a960b`` topology (a bare interpreter).
        distribution: An extra ``<name>-<version>.dist-info`` to drop into
            site-packages, for the "distribution present, console script not
            yet linked" path.
    """

    venv = impl / ".venv"
    for bindir in (venv / "Scripts", venv / "bin"):
        bindir.mkdir(parents=True, exist_ok=True)
        # Every venv has an interpreter; the live defect had ONLY this.
        bindir.joinpath("python.exe" if os.name == "nt" else "python").write_bytes(b"")
        if entrypoint:
            suffix = ".exe" if bindir.name == "Scripts" else ""
            bindir.joinpath(f"{entrypoint}{suffix}").write_bytes(b"")
    if synced:
        for site in (venv / "Lib" / "site-packages", venv / "lib" / "python3.12" / "site-packages"):
            site.mkdir(parents=True, exist_ok=True)
            site.joinpath("_marker.pth").write_text("", encoding="utf-8")
            if distribution:
                site.joinpath(f"{distribution}-0.1.0.dist-info").mkdir(exist_ok=True)
    return venv


def _clear_deployment_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the pack's deployment inputs (the reproduced live topology)."""

    monkeypatch.delenv("SPOTTER_IMPL_DIR", raising=False)
    monkeypatch.delenv("SPOTTER_CLIO_CONFIG", raising=False)


def _set_deployment_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, install_venv: bool = True
) -> Path:
    """Set the pack's deployment inputs to a real, SYNCED impl dir + config file."""

    impl = tmp_path / "spotter-impl"
    impl.mkdir(parents=True, exist_ok=True)
    if install_venv:
        _install_impl_venv(impl)
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
# 1b. The ENTRY POINT, not just the directory (live defect sess_086cf23a960b)
# --------------------------------------------------------------------------- #


def test_create_into_spotter_ai_is_refused_when_the_project_venv_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The declared ``--project`` directory EXISTS but has no venv at all.

    ``uv run --no-sync`` cannot provision one, so the entry point can never
    resolve — the directory stat that used to pass here is not enough.
    """

    _install_watcher_blueprint(tmp_path)
    impl = _set_deployment_env(monkeypatch, tmp_path, install_venv=False)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        with caplog.at_level(logging.WARNING, logger="clio_agent.gact.spotter_arming"):
            resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert (
        error["error"] == REFUSAL_WATCHER_ENTRYPOINT_MISSING == "spotter_watcher_entrypoint_missing"
    )
    details = error["details"]
    assert details["reason"] == REFUSAL_WATCHER_ENTRYPOINT_MISSING
    assert details["entrypoint"] == "spotter-mcp"
    assert details["project_dir"] == str(impl)
    assert details["venv_state"] == "missing_venv"
    assert details["mcp_server"] == "spotter"
    assert "spotter-mcp" in details["detail"]

    # Fail-closed AT ARMING: the refused create left no session behind.
    assert app.state.sessions.list() == []
    assert any(
        "spotter_watcher_arm_refused" in record.getMessage()
        and REFUSAL_WATCHER_ENTRYPOINT_MISSING in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_create_into_spotter_ai_is_refused_when_the_venv_is_unsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduced live topology: a venv holding ONLY an interpreter.

    ``SPOTTER_IMPL_DIR`` was set, the directory existed, the old gate passed —
    and ``uv run --no-sync spotter-mcp`` then died "program not found".
    """

    _install_watcher_blueprint(tmp_path)
    impl = _set_deployment_env(monkeypatch, tmp_path, install_venv=False)
    _install_impl_venv(impl, entrypoint="", synced=False)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 422, resp.text
    details = resp.json()["error"]["details"]
    assert details["reason"] == REFUSAL_WATCHER_ENTRYPOINT_MISSING
    assert details["venv_state"] == "unsynced"
    assert app.state.sessions.list() == []


def test_create_into_spotter_ai_is_refused_when_the_entrypoint_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully synced venv that simply does not provide THIS console script."""

    _install_watcher_blueprint(tmp_path)
    impl = _set_deployment_env(monkeypatch, tmp_path, install_venv=False)
    _install_impl_venv(impl, entrypoint="some-other-tool")

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 422, resp.text
    details = resp.json()["error"]["details"]
    assert details["reason"] == REFUSAL_WATCHER_ENTRYPOINT_MISSING
    assert details["venv_state"] == "entrypoint_absent"
    assert details["entrypoint"] == "spotter-mcp"
    assert app.state.sessions.list() == []


def test_create_into_spotter_ai_arms_when_the_entrypoint_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console script IS in the project venv -> the launcher can spawn."""

    _install_watcher_blueprint(tmp_path)
    impl = _set_deployment_env(monkeypatch, tmp_path)
    assert (impl / ".venv" / ("Scripts" if os.name == "nt" else "bin")).is_dir()

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 200, resp.text
    assert len(app.state.agent_task_registry.for_parent(resp.json()["id"])) == 1


def test_a_matching_distribution_satisfies_the_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No console script, but site-packages carries the matching distribution.

    The name is normalized PEP-503 style (``spotter-mcp`` -> ``spotter_mcp``),
    and this fallback can only ever make the gate MORE permissive — it never
    invents a refusal.
    """

    _install_watcher_blueprint(tmp_path)
    impl = _set_deployment_env(monkeypatch, tmp_path, install_venv=False)
    _install_impl_venv(impl, entrypoint="", distribution="spotter_mcp")

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 200, resp.text


def test_a_sync_enabled_uv_launcher_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``--no-sync``, ``uv run`` PROVISIONS the venv before exec.

    An absent venv is therefore not a static precondition failure, so refusing
    would be a false refusal of a working deployment. The skip is typed-logged,
    never silent.
    """

    _install_watcher_blueprint(tmp_path, agent_md=_AGENT_MD_SYNC_ENABLED)
    _set_deployment_env(monkeypatch, tmp_path, install_venv=False)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 200, resp.text
    assert len(app.state.agent_task_registry.for_parent(resp.json()["id"])) == 1


def test_a_non_uv_launcher_shape_keeps_todays_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``node ... server.js`` has no venv to model, so only the directory is checked."""

    _install_watcher_blueprint(tmp_path, agent_md=_AGENT_MD_NON_UV)
    _set_deployment_env(monkeypatch, tmp_path, install_venv=False)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})

    assert resp.status_code == 200, resp.text
    assert len(app.state.agent_task_registry.for_parent(resp.json()["id"])) == 1


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
    _install_impl_venv(impl)

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


def test_validate_watcher_arming_flags_a_missing_entrypoint(tmp_path: Path) -> None:
    """The validator names the entry point, the project, and the venv state."""

    _install_watcher_blueprint(tmp_path)
    app = build_app(sessions_path=tmp_path / "s.json")
    impl = tmp_path / "spotter-impl"
    impl.mkdir()
    env = {"SPOTTER_IMPL_DIR": str(impl), "SPOTTER_CLIO_CONFIG": str(tmp_path / "clio.yaml")}

    missing = validate_watcher_arming(app, env=env)
    assert missing is not None
    assert missing.reason == REFUSAL_WATCHER_ENTRYPOINT_MISSING
    assert missing.entrypoint == "spotter-mcp"
    assert missing.venv_state == "missing_venv"
    assert missing.path == str(impl)
    assert missing.variable == ""

    _install_impl_venv(impl, entrypoint="", synced=False)
    assert (validate_watcher_arming(app, env=env) or missing).venv_state == "unsynced"

    _install_impl_venv(impl, entrypoint="")
    assert (validate_watcher_arming(app, env=env) or missing).venv_state == "entrypoint_absent"

    _install_impl_venv(impl)
    assert validate_watcher_arming(app, env=env) is None


def test_every_refusal_reason_is_in_the_closed_set() -> None:
    assert set(SPOTTER_ARMING_REASONS) == {
        REFUSAL_WATCHER_UNMOUNTABLE,
        REFUSAL_WATCHER_PROJECT_MISSING,
        REFUSAL_WATCHER_ENTRYPOINT_MISSING,
    }
    assert all(SPOTTER_ARMING_REASONS.values())


def test_validate_watcher_arming_is_a_no_op_without_a_declared_server(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    assert validate_watcher_arming(app, env={}) is None
