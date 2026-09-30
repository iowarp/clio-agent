"""A session's MCP servers start in the background, before its first message.

Owner, 2026-09-30: the first message must not pay for starting the session's
servers. When a session is created or an Agent Blueprint is activated, CLIO starts
the declared servers concurrently in the background; a turn that arrives first
waits only for the listings it needs (cached or joined), and a tool call waits only
for its own server (the executor's per-namespace connect joins the warm-up).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import session_warmup
from clio_agent.gact.app import build_app


class _Executor:
    def __init__(self, specs: dict[str, Any]) -> None:
        self._clio_namespace_specs = specs


class _Agent:
    def __init__(self, executor: _Executor, pack: dict[str, Any]) -> None:
        self.executor = executor
        self.pack = pack

    def _active_tool_executor(self) -> _Executor:
        return self.executor

    def _discover_pack_servers(self, blueprint_id: str = "", *, cwd: str | None = None) -> Any:
        del blueprint_id, cwd
        return self.pack


def _specs(*names: str, always_load: tuple[str, ...] = ()) -> dict[str, Any]:
    return {n: SimpleNamespace(always_load=n in always_load) for n in names}


def test_declared_servers_start_concurrently_and_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    mounted: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def mount(executor: Any, namespace: str, spec: Any, *, connect: bool = True) -> dict:
        del executor, spec
        time.sleep(0.3)  # a server's start-up
        with lock:
            mounted.append((namespace, connect))
        return {}

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    specs = _specs("geo", "ndp", "plot", "web", "user_only", always_load=("web",))
    agent = _Agent(_Executor(specs), pack={"geo": {}, "ndp": {}, "plot": {}})

    started = time.monotonic()
    report = session_warmup.warm_session_servers(agent)
    elapsed = time.monotonic() - started

    # The blueprint's servers and the always-load service; never a service the
    # session did not ask for.
    assert sorted(mounted) == [("geo", True), ("ndp", True), ("plot", True), ("web", True)]
    assert elapsed < 0.9, f"servers started one after another ({elapsed:.2f}s)"
    assert report == {"geo": "ready", "ndp": "ready", "plot": "ready", "web": "ready"}


def test_a_server_that_fails_to_start_is_reported_typed_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mount(executor: Any, namespace: str, spec: Any, *, connect: bool = True) -> dict:
        del executor, spec, connect
        if namespace == "geo":
            raise FileNotFoundError("uvx")
        return {}

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    agent = _Agent(_Executor(_specs("geo", "ndp")), pack={"geo": {}, "ndp": {}})

    report = session_warmup.warm_session_servers(agent)

    assert report["ndp"] == "ready"
    assert report["geo"] and report["geo"] != "ready"  # the typed discovery reason


def _recorded_starts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    starts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        session_warmup,
        "start_session_warmup",
        lambda app, sid, *, trigger: starts.append((sid, trigger)),
    )
    return starts


def test_creating_a_session_starts_its_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = _recorded_starts(monkeypatch)
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
    assert starts == [(sid, "session_created")]


def test_activating_a_blueprint_starts_its_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_gact.test_agent_elicitation import _write_minimal_answer_blueprint

    agent_md = _write_minimal_answer_blueprint(tmp_path / "bp")
    starts = _recorded_starts(monkeypatch)
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        activated = c.post(f"/v1/sessions/{sid}/agent-blueprint", json={"path": str(agent_md)})
        assert activated.status_code == 200, activated.text
    assert starts == [(sid, "session_created"), (sid, "blueprint_activated")]
