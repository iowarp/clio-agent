"""A plugin update reaches the conversations open now (host semantics HS3)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import connector_follow, plugin_tabs, session_warmup
from clio_agent.gact import plugin_update_follow as follow
from clio_agent.gact.agents import prompt_basis, resolution
from clio_agent.gact.agents.resolution import (
    AGENT_RESOLUTION_KEY,
    SUBAGENT_KEY,
    SessionAgentUnavailable,
)


def _row(plugin: str, digest: str, root: str = "/snap/a") -> dict[str, Any]:
    return {
        "id": plugin,
        "marketplace": "local",
        "root": root,
        "executable_digest": digest,
        "text_digest": "t",
    }


def _record(plugin: str, digest: str, root: str = "/snap/a") -> Any:
    return SimpleNamespace(
        id=plugin, marketplace="local", root=root, executable_digest=digest, text_digest="t"
    )


def test_only_a_pinned_plugin_whose_installed_copy_changed_is_updated() -> None:
    pinned = SimpleNamespace(plugins=[_row("fx", "d1"), _row("same", "d1"), _row("gone", "d1")])
    records = [
        _record("fx", "d2", root="/snap/b"),
        _record("same", "d1"),
        _record("other", "d9"),
    ]

    assert follow.updated_plugins(pinned, records) == ["fx"]
    assert follow.updated_plugins(pinned, [_record("fx", "d1")]) == []


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


class _Session:
    def __init__(self, metadata: dict[str, Any], workspace_id: str = "ws_1") -> None:
        self.metadata = metadata
        self.workspace_id = workspace_id

    def to_wire(self) -> dict[str, Any]:
        stamp = "2026-07-28T00:00:00Z"
        return {
            "id": "s1",
            "workspace_id": "ws_1",
            "title": "t",
            "created_at": stamp,
            "updated_at": stamp,
        }


@pytest.fixture
def moved(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A session pinned on ``fx`` at d1, with every side of a re-pin recorded."""

    seen: dict[str, Any] = {
        "pins": [],
        "held": [],
        "warm": [],
        "tabs": [],
        "restarts": [],
        "digest": "new",
    }
    sessions = {
        "s1": _Session({AGENT_RESOLUTION_KEY: "old"}),
        "s2": _Session({AGENT_RESOLUTION_KEY: "old"}),
        "sub": _Session({AGENT_RESOLUTION_KEY: "old", SUBAGENT_KEY: "parent"}),
        "fresh": _Session({}),
    }
    workspaces = {"ws_1": SimpleNamespace(root_path="/work")}

    def restart(root: str) -> str:
        seen["restarts"].append(root)
        return "restarted_live"

    seen["app"] = SimpleNamespace(
        state=SimpleNamespace(
            bus=_Bus(),
            sessions=SimpleNamespace(get=sessions.get),
            workspaces=SimpleNamespace(get=workspaces.get),
            agent=SimpleNamespace(request_fleet_restart=restart),
        )
    )

    def pin(_app: Any, sid: str) -> Any:
        seen["pins"].append(sid)
        if isinstance(seen["digest"], Exception):
            raise seen["digest"]
        return SimpleNamespace(digest=seen["digest"])

    monkeypatch.setattr(
        resolution, "pinned_resolution", lambda *_a: SimpleNamespace(plugins=[_row("fx", "d1")])
    )
    monkeypatch.setattr(resolution, "pin_session_agent", pin)
    monkeypatch.setattr(
        prompt_basis, "hold_basis", lambda _app, sid, prev: seen["held"].append((sid, prev))
    )
    monkeypatch.setattr(
        session_warmup,
        "start_session_warmup",
        lambda _app, sid, *, trigger: seen["warm"].append((sid, trigger)),
    )
    monkeypatch.setattr(
        plugin_tabs, "session_agent_changed", lambda _app, sid: seen["tabs"].append(sid)
    )
    return seen


def test_an_updated_plugin_pins_the_open_session_again(moved: dict[str, Any]) -> None:
    app = moved["app"]

    assert follow.follow_session(app, "s1", [_record("fx", "d2", root="/snap/b")]) == ["fx"]

    assert moved["pins"] == ["s1"]
    assert moved["held"] == [("s1", "old")]
    assert moved["warm"] == [("s1", follow.PLUGIN_UPDATED)]
    assert moved["tabs"] == ["s1"]
    assert moved["restarts"] == ["/work"], "a changed server starts from the new copy"
    [event] = app.state.bus.events
    assert event.type == "session.updated"
    assert event.payload["reason"] == follow.PLUGIN_UPDATED
    assert event.payload["plugins"] == ["fx"]
    assert event.payload["id"] == "s1"


def test_a_text_only_update_keeps_the_running_servers(moved: dict[str, Any]) -> None:
    app = moved["app"]
    record = _record("fx", "d1", root="/snap/b")
    record.text_digest = "t2"

    assert follow.follow_session(app, "s1", [record]) == ["fx"]
    assert moved["tabs"] == ["s1"]
    assert moved["restarts"] == []


def test_sessions_sharing_a_workspace_restart_its_fleet_once(moved: dict[str, Any]) -> None:
    app = moved["app"]
    restarted: set[str] = set()
    records = [_record("fx", "d2", root="/snap/b")]

    assert follow.follow_session(app, "s1", records, restarted) == ["fx"]
    assert follow.follow_session(app, "s2", records, restarted) == ["fx"]
    assert moved["tabs"] == ["s1", "s2"]
    assert moved["restarts"] == ["/work"]


def test_an_unchanged_or_unpinned_session_is_left_alone(moved: dict[str, Any]) -> None:
    app = moved["app"]

    assert follow.follow_session(app, "s1", [_record("fx", "d1")]) == []
    assert follow.follow_session(app, "sub", [_record("fx", "d2")]) == []
    assert follow.follow_session(app, "fresh", [_record("fx", "d2")]) == []
    assert follow.follow_session(app, "missing", [_record("fx", "d2")]) == []

    assert moved["pins"] == []
    assert app.state.bus.events == []


def test_an_update_not_yet_trusted_keeps_the_session_on_its_copy(moved: dict[str, Any]) -> None:
    app = moved["app"]
    moved["digest"] = SessionAgentUnavailable("main", "plugin_untrusted", [])

    assert follow.follow_session(app, "s1", [_record("fx", "d2")]) == []
    moved["digest"] = "old"  # resolves to the pin it has: nothing moved
    assert follow.follow_session(app, "s1", [_record("fx", "d2")]) == []

    assert moved["pins"] == ["s1", "s1"]
    assert moved["held"] == moved["warm"] == moved["tabs"] == moved["restarts"] == []
    assert app.state.bus.events == []


def test_open_sessions_are_looked_at_again_only_when_something_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SimpleNamespace(rev=1, revision=lambda: store.rev, records=lambda: ["record"])
    monkeypatch.setattr(resolution, "plugin_service", lambda _app: SimpleNamespace(store=store))
    open_now = ["s1", "s2"]
    monkeypatch.setattr(connector_follow, "open_session_ids", lambda _app: list(open_now))
    calls: list[str] = []

    def follow_one(_app: Any, sid: str, records: list[Any], restarted: set[str]) -> list[str]:
        calls.append(sid)
        assert records == ["record"]
        assert isinstance(restarted, set)
        if sid == "s2":
            raise RuntimeError("one session failed")
        return ["fx"]

    monkeypatch.setattr(follow, "follow_session", follow_one)

    key, moved_now = follow.follow_open_sessions(object())
    assert moved_now == {"s1": ["fx"]}
    assert follow.follow_open_sessions(object(), key) == (key, {})
    store.rev = 2
    key, _ = follow.follow_open_sessions(object(), key)
    open_now.append("s3")
    follow.follow_open_sessions(object(), key)

    assert calls == ["s1", "s2", "s1", "s2", "s1", "s2", "s3"]


def test_an_app_without_plugins_follows_nothing() -> None:
    assert follow.start(SimpleNamespace(state=SimpleNamespace())) is None
