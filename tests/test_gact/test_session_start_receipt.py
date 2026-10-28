"""A child's SessionStart receipt: ``started`` only when a hook can actually run.

A kill between the ``started`` receipt and ``completed`` leaves the child's startup
settlement unknown forever (it is never admitted again, and hooks are never
replayed). With no SessionStart hook that could run, nothing is started, so the
receipt stays ``pending`` and a re-run of the (empty) startup is safe.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.hooks import dispatcher as hook_dispatch
from clio_agent.gact.hooks.config import HookEntry, HookMatch, HookRun
from clio_agent.gact.hooks.events import SESSION_START
from clio_agent.gact.sessions import SessionStore


class _Killed(BaseException):
    """Stands in for the process dying inside the startup window."""


def _child(monkeypatch: pytest.MonkeyPatch, *, hooks: bool) -> tuple[SessionStore, str, list]:
    store = SessionStore()
    store.set_lifecycle_observer(lambda *_: None)
    monkeypatch.setattr("clio_agent.gact.hooks.session_start_may_run_hooks", lambda _sid: hooks)
    persisted: list[str] = []
    original = store._persist

    def persist(session: Any) -> None:
        if session.parent_session_id:
            persisted.append(session.creation_hook_state)
        original(session)

    monkeypatch.setattr(store, "_persist", persist)
    child = store.create(
        workspace_id="ws_test",
        parent_session_id="sess_parent",
        creation_id="receipt-test",
        creation_digest="ab" * 32,
        defer_initialization=True,
    )
    return store, child.id, persisted


@pytest.mark.parametrize("hooks", [False, True])
def test_the_started_receipt_is_written_only_when_a_hook_can_run(
    monkeypatch: pytest.MonkeyPatch, hooks: bool
) -> None:
    store, sid, persisted = _child(monkeypatch, hooks=hooks)
    dispatched: list[bool] = []

    def dispatch(**kwargs: Any) -> None:
        dispatched.append(kwargs["hooks_expected"])

    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", dispatch)
    store.initialize(sid)
    assert dispatched == [hooks]
    assert ("started" in persisted) is hooks
    assert store.get(sid).creation_hook_state == "completed"


def test_a_kill_inside_a_hookless_startup_leaves_it_safe_to_run_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sid, _persisted = _child(monkeypatch, hooks=False)

    def killed(**_: Any) -> None:
        raise _Killed

    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", killed)
    with pytest.raises(_Killed):
        store.initialize(sid)
    assert store.get(sid).creation_hook_state == "pending"
    assert store.startup_retirement_error(sid) is None
    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", lambda **_: None)
    store.initialize(sid)
    assert store.get(sid).creation_hook_state == "completed"


def test_a_kill_inside_a_hook_keeps_its_settlement_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sid, _persisted = _child(monkeypatch, hooks=True)

    def killed(**_: Any) -> None:
        raise _Killed

    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", killed)
    with pytest.raises(_Killed):
        store.initialize(sid)
    assert store.get(sid).creation_hook_state == "started"
    assert "settlement is unconfirmed" in str(store.startup_retirement_error(sid))
    with pytest.raises(RuntimeError, match="effects are unconfirmed"):
        store.initialize(sid)  # never replayed


def test_only_a_trusted_enabled_session_start_hook_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("clio_agent.gact.hooks.plugin_hooks.plugin_hook_entries", lambda _sid: [])
    monkeypatch.setattr(hook_dispatch, "_GLOBAL", None)
    assert not hook_dispatch.session_start_may_run_hooks("sess_x")

    def entry(**overrides: Any) -> HookEntry:
        fields = {
            "id": "probe",
            "on": frozenset({SESSION_START}),
            "match": HookMatch(),
            "run": HookRun(type="command", command="test-only"),
            **overrides,
        }
        return HookEntry(**fields)

    for entries, expected in (
        ([entry()], True),
        ([entry(on=frozenset({"SessionEnd"}))], False),
        ([entry(enabled=False)], False),
        ([entry(trust="untrusted")], False),
    ):
        monkeypatch.setattr(hook_dispatch, "_GLOBAL", hook_dispatch.HookDispatcher(entries))
        assert hook_dispatch.session_start_may_run_hooks("sess_x") is expected
    assert not hook_dispatch.dispatch_session_start(
        session_id="sess_x", hooks_expected=False
    ).records
