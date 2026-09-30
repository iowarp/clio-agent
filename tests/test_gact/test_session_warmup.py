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


def test_connected_servers_are_not_started_again(monkeypatch: pytest.MonkeyPatch) -> None:
    mounted: list[str] = []

    def mount(executor: Any, namespace: str, spec: Any, *, connect: bool = True) -> dict:
        del executor, spec, connect
        mounted.append(namespace)
        return {}

    monkeypatch.setattr("clio_agent.gact.mcp_readiness.mount_namespace_for_session", mount)
    executor = _Executor(_specs("geo", "ndp"))
    executor.is_namespace_prepared = lambda ns: ns == "geo"  # type: ignore[attr-defined]

    report = session_warmup.warm_session_servers(_Agent(executor, pack={"geo": {}, "ndp": {}}))

    assert mounted == ["ndp"]
    assert report == {"geo": "ready", "ndp": "ready"}


def test_one_warmup_per_session_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    runs: list[str] = []

    def warm(app: Any, sid: str, trigger: str) -> None:
        del app
        runs.append(trigger)
        release.wait(5)

    monkeypatch.setattr(session_warmup, "session_warmup_enabled", lambda: True)
    monkeypatch.setattr(session_warmup, "_warm_and_release", _wrap(warm))
    app = SimpleNamespace(state=SimpleNamespace(agent=_Agent(_Executor({}), pack={})))
    first = session_warmup.start_session_warmup(app, "s1", trigger="session_created")
    second = session_warmup.start_session_warmup(app, "s1", trigger="turn_started")
    other = session_warmup.start_session_warmup(app, "s2", trigger="turn_started")
    release.set()
    for thread in (first, other):
        assert thread is not None
        thread.join(5)

    assert second is None, "a session already warming is not warmed twice"
    assert sorted(runs) == ["session_created", "turn_started"]
    third = session_warmup.start_session_warmup(app, "s1", trigger="turn_started")
    assert third is not None, "once done, the next turn may warm again"
    third.join(5)


def _wrap(warm: Any) -> Any:
    def run(app: Any, sid: str, trigger: str) -> None:
        try:
            warm(app, sid, trigger)
        finally:
            session_warmup._release(sid)

    return run


def test_a_turn_starts_the_warmup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn builds its request from listings and never waits for a server; the
    servers it may call start alongside it (after an answered question too, when the
    idle reaper has closed them)."""
    from .conftest import complete_turn

    starts = _recorded_starts(monkeypatch)
    from .test_post_messages import FakeClioAgent

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent())
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        complete_turn(c, sid, "hi")
    assert (sid, "turn_started") in starts


class _LeasingAgent:
    def __init__(self) -> None:
        self.leases: list[str] = []

    def lease_workspace_fleet(self, root: str) -> Any:
        from contextlib import contextmanager

        @contextmanager
        def lease() -> Any:
            self.leases.append(root)
            try:
                yield
            finally:
                self.leases.remove(root)

        return lease()


def _app_with_session(agent: Any) -> Any:
    session = SimpleNamespace(workspace_id="ws1")
    workspace = SimpleNamespace(root_path="D:/work")
    return SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            sessions={"s1": session},
            workspaces={"ws1": workspace},
        )
    )


def test_a_session_waiting_on_its_user_keeps_its_servers_until_the_next_turn() -> None:
    """Field report (2026-09-30): after answering a question the session sat on
    "Setting up session" -- the idle reaper had closed its servers meanwhile."""
    agent = _LeasingAgent()
    app = _app_with_session(agent)

    session_warmup.hold_session_fleet(app, "s1")
    assert agent.leases == ["D:/work"]
    session_warmup.hold_session_fleet(app, "s1")  # a second pause does not stack
    assert agent.leases == ["D:/work"]

    session_warmup.release_session_fleet("s1")
    assert agent.leases == []


def test_a_hold_ends_by_itself_when_nobody_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_warmup, "hold_while_waiting_s", lambda: 0.2)
    agent = _LeasingAgent()
    session_warmup.hold_session_fleet(_app_with_session(agent), "s1")
    assert agent.leases == ["D:/work"]
    deadline = time.monotonic() + 5
    while agent.leases and time.monotonic() < deadline:
        time.sleep(0.05)
    assert agent.leases == []


def test_a_turn_with_every_server_connected_does_no_discovery() -> None:
    """The warm-up a turn starts must cost nothing when the servers are up: it
    runs beside the turn's own prologue, so it may not re-read blueprints then."""

    class _NoDiscovery(_Agent):
        def _discover_pack_servers(self, blueprint_id: str = "", *, cwd: str | None = None) -> Any:
            raise AssertionError("discovery ran although every server was connected")

    # "user_only" is a configured service this session does not use: never connected.
    executor = _Executor(_specs("geo", "ndp", "user_only"))
    executor.is_namespace_prepared = lambda ns: ns != "user_only"  # type: ignore[attr-defined]
    executor._clio_warmed_namespaces = frozenset({"geo", "ndp"})  # type: ignore[attr-defined]

    assert session_warmup.warm_session_servers(_NoDiscovery(executor, pack={})) == {}
