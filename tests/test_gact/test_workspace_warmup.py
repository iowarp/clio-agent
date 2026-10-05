"""Draft preparation starts the real workspace fleet without a saved session."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context, session_warmup, workspace_warmup
from clio_agent.gact.agent_blueprints import install_agent_blueprint
from clio_agent.gact.app import build_app
from clio_agent.gact.session_defaults import UpdateSessionDefaultsRequest
from clio_agent.tools.execution import get_active_tool_blueprint_id, get_active_tool_workspace_root
from clio_agent.tools.workspace_root import canonical_workspace_root


class _Executor:
    def __init__(self) -> None:
        self._clio_namespace_specs = {
            "web": SimpleNamespace(always_load=True),
            "science": SimpleNamespace(always_load=False),
            "unused": SimpleNamespace(always_load=False),
        }
        self.prepared: set[str] = set()

    def is_namespace_prepared(self, namespace: str) -> bool:
        return namespace in self.prepared


class _Agent:
    def __init__(self) -> None:
        self.executors: dict[str, _Executor] = {}
        self.bound: list[tuple[str, str, str]] = []
        self.leases: list[str] = []

    @contextmanager
    def lease_workspace_fleet(self, root: str) -> Iterator[None]:
        self.leases.append(root)
        try:
            yield
        finally:
            self.leases.remove(root)

    def _active_tool_executor(self) -> _Executor:
        root = canonical_workspace_root(get_active_tool_workspace_root())
        assert root in [canonical_workspace_root(item) for item in self.leases]
        self.bound.append((root, get_active_tool_blueprint_id(), context.active_tool_session_id()))
        return self.executors.setdefault(root, _Executor())

    def _discover_pack_servers(self, blueprint_id: str, *, cwd: str | None) -> dict[str, Any]:
        assert cwd
        return {"science": {}} if blueprint_id == "science-blueprint" else {}


@pytest.fixture
def threads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[threading.Thread]]:
    """Join every background preparation before the test tears down its app."""
    started: list[threading.Thread] = []
    original = threading.Thread.start

    def start(thread: threading.Thread) -> None:
        if thread.name.startswith("clio-") and "warmup" in thread.name:
            started.append(thread)
        original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    yield started
    for thread in started:
        thread.join(5)
        assert not thread.is_alive()


def test_draft_prepares_default_fleet_and_first_session_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, threads: list[threading.Thread]
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "AGENT.md").write_text("---\nid: science-blueprint\ntitle: Science\n---\nScience")
    install_agent_blueprint(source=str(source), scope="global", cwd=tmp_path)
    mounted: list[tuple[_Executor, str]] = []

    def mount(executor: _Executor, namespace: str, spec: Any, *, connect: bool) -> dict:
        assert connect
        executor.prepared.add(namespace)
        mounted.append((executor, namespace))
        return {}

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        agent = _Agent()
        app.state.agent = agent
        app.state.session_defaults.update(
            UpdateSessionDefaultsRequest(blueprint_id="science-blueprint")
        )
        workspace = app.state.workspaces.create(name="draft", root_path=str(tmp_path))
        before = app.state.sessions.list()
        response = client.post(f"/v1/workspaces/{workspace.id}/warmup")
        assert response.status_code == 202
        assert response.json() == {"status": "warming"}
        threads[-1].join(5)
        assert not threads[-1].is_alive()
        assert app.state.sessions.list() == before
        assert {namespace for _, namespace in mounted} == {"web", "science"}
        assert agent.bound == [(canonical_workspace_root(tmp_path), "science-blueprint", "")]
        assert agent.leases == []
        executor = agent.executors[canonical_workspace_root(tmp_path)]

        created = client.post("/v1/sessions", json={"workspace_id": workspace.id})
        assert created.status_code == 200
        threads[-1].join(5)
        assert not threads[-1].is_alive()
        assert agent.bound[-1][2] == created.json()["id"]
        assert agent.executors[canonical_workspace_root(tmp_path)] is executor
        assert len(mounted) == 2, "already connected servers must not start again"


def test_warmup_validates_registered_root_and_advertises_support(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        assert client.post("/v1/workspaces/missing/warmup").status_code == 404
        workspace = app.state.workspaces.create(name="missing", root_path=str(tmp_path / "gone"))
        assert client.post(f"/v1/workspaces/{workspace.id}/warmup").status_code == 409
        capabilities = client.get("/v1/capabilities", headers={"X-GACT-Version": "0.3"}).json()
        assert capabilities["capabilities"]["x_clio_workspace_warmup"] is True


def _app(agent: Any, blueprint: str = "") -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            session_defaults=SimpleNamespace(get=lambda: SimpleNamespace(blueprint_id=blueprint)),
        )
    )


def test_disabled_and_unready_agents_do_not_start_threads(
    monkeypatch: pytest.MonkeyPatch, threads: list[threading.Thread], tmp_path: Path
) -> None:
    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: False)
    assert workspace_warmup.start_workspace_warmup(_app(_Agent()), str(tmp_path)) == "disabled"
    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)
    assert workspace_warmup.start_workspace_warmup(_app(None), str(tmp_path)) == "unavailable"
    assert workspace_warmup.start_workspace_warmup(_app(_Agent()), "") == "unavailable"
    assert threads == []


def test_concurrent_requests_coalesce_but_other_workspaces_and_apps_do_not(
    monkeypatch: pytest.MonkeyPatch, threads: list[threading.Thread], tmp_path: Path
) -> None:
    release = threading.Event()
    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)

    def warm(agent: Any) -> dict[str, str]:
        assert release.wait(5)
        return {}

    monkeypatch.setattr(session_warmup, "warm_session_servers", warm)
    app = _app(_Agent())
    try:
        for _ in range(3):
            assert workspace_warmup.start_workspace_warmup(app, str(tmp_path)) == "warming"
        assert len(threads) == 1
        workspace_warmup.start_workspace_warmup(app, str(tmp_path / "second"))
        workspace_warmup.start_workspace_warmup(_app(_Agent()), str(tmp_path))
        assert len(threads) == 3
        app.state.session_defaults.get = lambda: SimpleNamespace(blueprint_id="changed")
        workspace_warmup.start_workspace_warmup(app, str(tmp_path))
        assert len(threads) == 4
    finally:
        release.set()


def test_failed_preparation_can_be_retried(
    monkeypatch: pytest.MonkeyPatch, threads: list[threading.Thread], tmp_path: Path, caplog: Any
) -> None:
    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)

    def fail(agent: Any) -> dict[str, str]:
        raise RuntimeError("temporary startup failure")

    monkeypatch.setattr(session_warmup, "warm_session_servers", fail)
    app = _app(_Agent())
    workspace_warmup.start_workspace_warmup(app, str(tmp_path))
    threads[-1].join(5)
    assert "workspace_warmup_failed" in caplog.text
    assert app.state.agent.leases == []
    monkeypatch.setattr(session_warmup, "warm_session_servers", lambda agent: {})
    workspace_warmup.start_workspace_warmup(app, str(tmp_path))
    assert len(threads) == 2
