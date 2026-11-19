"""A settled child's own MCP servers close with it; shared ones stay (child_servers)."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import child_servers
from clio_agent.tools.workspace_root import canonical_workspace_root

ROOT = canonical_workspace_root("/work/space")

#: server set -> its declared servers ({plugin: {server: declaration}}).
SETS = {
    "parent-set": {"base": {"viewer": {}}},
    "child-set": {"cad": {"viewer": {}, "modeler": {}, "mesher": {}}},
    "other-set": {"tools": {"mesher": {}}},
}


class _Sessions:
    def __init__(self, rows: dict[str, Any]) -> None:
        self.rows = rows

    def get(self, sid: str) -> Any:
        return self.rows.get(sid)


class _Executor:
    def __init__(self, mounted: set[str]) -> None:
        self._clio_mounted_server_sets = mounted
        self._namespace_servers = {name: object() for name in ("viewer", "modeler", "mesher")}
        self.closed = False
        self.reset: list[str] = []

    def reset_namespace(self, namespace: str) -> None:
        self.reset.append(namespace)


def _app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mounted: set[str],
    session_sets: dict[str, str],
    open_children: tuple[str, ...] = (),
    busy: tuple[str, ...] = (),
) -> tuple[Any, _Executor]:
    executor = _Executor(mounted)
    session = lambda sid, parent="": SimpleNamespace(  # noqa: E731
        id=sid, parent_session_id=parent, workspace_id="ws"
    )
    rows = {
        "parent": session("parent"),
        "child": session("child", "parent"),
        "sibling": session("sibling", "parent"),
        "elsewhere": SimpleNamespace(id="elsewhere", parent_session_id="", workspace_id="ws"),
    }
    lock = threading.Lock()
    agent = SimpleNamespace(_workspace_state=lambda: (lock, {ROOT: executor}, {}))
    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions=_Sessions(rows),
            workspaces={"ws": SimpleNamespace(root_path="/work/space")},
            agent=agent,
            turn_runner=SimpleNamespace(active_sessions=lambda: busy),
        )
    )
    from clio_agent.gact.agents import resolution
    from clio_agent.gact.routes import session_cancellation
    from clio_agent.plugins import servers

    monkeypatch.setattr(resolution, "session_server_set", lambda _app, sid: session_sets[sid])
    monkeypatch.setattr(servers, "server_set_variant", lambda _key: "")
    monkeypatch.setattr(servers, "server_set_declarations", lambda key: dict(SETS.get(key, {})))
    monkeypatch.setattr(
        session_cancellation,
        "stop_scope",
        lambda _app, _sid: [{"session_id": sid} for sid in open_children],
    )
    return app, executor


def test_only_the_childs_own_servers_close(monkeypatch: pytest.MonkeyPatch) -> None:
    app, executor = _app(
        monkeypatch,
        mounted={"parent-set", "child-set"},
        session_sets={"child": "child-set", "parent": "parent-set"},
    )
    plan = child_servers.exclusive_servers(app, "child")
    assert plan == (ROOT, executor, ["mesher", "modeler"])  # "viewer" is the conversation's


def test_a_server_another_mounted_set_uses_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    app, executor = _app(
        monkeypatch,
        mounted={"parent-set", "child-set", "other-set"},
        session_sets={"child": "child-set", "parent": "parent-set"},
    )
    assert child_servers.exclusive_servers(app, "child") == (ROOT, executor, ["modeler"])


@pytest.mark.parametrize(
    ("open_children", "busy"),
    [(("sibling",), ()), ((), ("sibling",))],
    ids=["open-sibling", "busy-session"],
)
def test_nothing_closes_while_another_session_runs_the_same_set(
    monkeypatch: pytest.MonkeyPatch, open_children: tuple[str, ...], busy: tuple[str, ...]
) -> None:
    app, _executor = _app(
        monkeypatch,
        mounted={"parent-set", "child-set"},
        session_sets={"child": "child-set", "parent": "parent-set", "sibling": "child-set"},
        open_children=open_children,
        busy=busy,
    )
    assert child_servers.exclusive_servers(app, "child") is None


def test_a_child_running_the_conversations_own_agent_closes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _executor = _app(
        monkeypatch,
        mounted={"parent-set"},
        session_sets={"child": "parent-set", "parent": "parent-set"},
    )
    assert child_servers.exclusive_servers(app, "child") is None


def test_a_top_level_conversation_is_never_a_child(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _executor = _app(
        monkeypatch, mounted={"parent-set"}, session_sets={"parent": "parent-set"}
    )
    assert child_servers.exclusive_servers(app, "parent") is None


def test_closing_resets_the_namespace_and_retires_its_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, executor = _app(
        monkeypatch,
        mounted={"parent-set", "child-set"},
        session_sets={"child": "child-set", "parent": "parent-set"},
    )
    from clio_agent.gact import session_warmup
    from clio_agent.tools import fleet_close_listeners

    released: list[str] = []
    monkeypatch.setattr(session_warmup, "release_session_fleet", released.append)
    dropped: list[tuple[str, tuple[Any, ...], str]] = []
    remove = fleet_close_listeners.listen(
        lambda root, proxies, reason: dropped.append((root, proxies, reason))
    )
    try:
        worker = child_servers.close_child_servers(app, "child")
        assert worker is not None
        worker.join(5)
    finally:
        remove()
    assert released == ["child"]
    assert executor.reset == ["mesher", "modeler"]
    servers = executor._namespace_servers
    assert dropped == [
        (ROOT, (servers["mesher"],), child_servers.CHILD_SETTLED),
        (ROOT, (servers["modeler"],), child_servers.CHILD_SETTLED),
    ]
