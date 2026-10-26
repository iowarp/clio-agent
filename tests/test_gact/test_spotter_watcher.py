"""SPOTTER uses ordinary Core mail and conversation-owned assignments."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.spotter_watcher import (
    _WAKE_COALESCED_TEXT,
    _WAKE_TURN_FINAL_TEXT,
    _live_watcher_tasks,
    _push_wake,
    _tool_outcome_fact,
    _wake_text,
    disarm_spotter_watcher,
    on_turn_finalized,
    wake_on_parent_activity,
)


def test_standing_watcher_has_no_worker_until_eligible_mail(
    watcher_world: tuple[Any, str, str],
) -> None:
    app, parent, child = watcher_world
    [task] = _live_watcher_tasks(app, parent)
    assert task.kind == "Subagent" and task.status == "active"
    assert task.owner.session_id == child and task.terminal_at is None
    assert task.resource_id == "spotter:" + parent
    assert not app.state.turn_runner.busy(child)
    assert app.state.task_history.pending_mail(child) == ()
    assert app.state.messages.get(child, []) == []


def test_parent_activity_coalesces_only_uncommitted_core_mail(
    watcher_world: tuple[Any, str, str],
) -> None:
    app, parent, child = watcher_world
    for index in range(5):
        wake_on_parent_activity(
            app, parent, tool_name="fs_read_file", result={"paths": [str(index)]}
        )
    pending = app.state.task_history.pending_mail(child)
    assert len(pending) == 1 and pending[0].revision == 5
    assert pending[0].kind == "spotter" and pending[0].sender_session == parent
    assert app.state.task_history.read_content(pending[0].content)["text"] == _WAKE_COALESCED_TEXT
    assert pending[0].id not in app.state.task_history.acknowledged_notifications(child)
    assert app.state.task_history.pending_mail(parent) == ()


def test_child_activity_never_self_wakes(watcher_world: tuple[Any, str, str]) -> None:
    app, parent, child = watcher_world
    wake_on_parent_activity(app, child, tool_name="fs_read_file")
    on_turn_finalized(app, child)
    assert app.state.task_history.pending_mail(child) == ()
    assert app.state.task_history.pending_mail(parent) == ()


def test_final_and_failed_tool_wakes_preserve_factual_content(
    watcher_world: tuple[Any, str, str],
) -> None:
    app, parent, child = watcher_world
    wake_on_parent_activity(app, parent, tool_name="fs_read_file", ok=False)
    [item] = app.state.task_history.pending_mail(child)
    text = app.state.task_history.read_content(item.content)["text"]
    assert text == _wake_text(tool_name="fs_read_file", tool_ok=False)
    assert "failed" in text
    assert item.handle == "task_watcher"


def test_explicit_disarm_uses_conversation_cancellation(
    watcher_world: tuple[Any, str, str],
) -> None:
    app, parent, child = watcher_world
    task = app.state.task_history.latest_assignment(child)
    disarm_spotter_watcher(app, app.state.sessions.get(parent))
    import asyncio

    asyncio.run_coroutine_threadsafe(
        app.state.task_operations.wait(
            app.state.conversations.caller(parent), task.handle, timeout_s=5
        ),
        app.state.mcp_app_loop,
    ).result(timeout=10)

    async def await_release() -> None:
        # Terminal publication/collection can enqueue a final admission probe.
        # Inspect quiescence on its owning loop, after every already-signalled
        # probe, rather than sampling a transient reservation from this thread.
        async with asyncio.timeout(5):
            control = app.state.conversations._controls.get(task.handle)
            if control is not None:
                await asyncio.shield(control)
            while True:
                pump = app.state.turn_runner._pumps.get(child)
                if pump is not None and not pump.done():
                    await asyncio.shield(pump)
                    continue
                if not app.state.turn_runner.busy(child):
                    break
                released = asyncio.Event()
                app.state.turn_runner.run_when_released(child, released.set)
                await released.wait()
            # Keep this observation on its owner loop: a later signal can
            # reserve an admission-only probe after this coroutine returns.
            assert not app.state.turn_runner.busy(child)

    asyncio.run_coroutine_threadsafe(await_release(), app.state.mcp_app_loop).result(timeout=10)
    current = app.state.task_history.task_snapshot(task.handle)
    assert current.status == "cancelled" and current.terminal_at is not None
    assert current.settlement == "confirmed"
    assert _live_watcher_tasks(app, parent) == []
    assert app.state.task_history.latest_assignment(child).handle == task.handle
    assert not [
        event
        for event in app.state.bus.session_events_since(child, cursor=1)
        if event.type == "turn.started"
    ]


def test_cancelled_watcher_late_probe_cannot_start_a_model(
    watcher_world: tuple[Any, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reserved late admission probe is busy bookkeeping, never another segment."""
    import asyncio

    app, parent, child = watcher_world
    task = app.state.task_history.latest_assignment(child)
    disarm_spotter_watcher(app, app.state.sessions.get(parent))

    async def probe() -> None:
        async with asyncio.timeout(5):
            control = app.state.conversations._controls.get(task.handle)
            if control is not None:
                await asyncio.shield(control)
            pump = app.state.turn_runner._pumps.get(child)
            if pump is not None:
                await asyncio.shield(pump)
            entered, release = asyncio.Event(), asyncio.Event()
            original = app.state.turn_runner.admit

            async def held_admission(sid: str) -> Any:
                if sid == child:
                    entered.set()
                    await release.wait()
                return await original(sid)

            monkeypatch.setattr(app.state.turn_runner, "admit", held_admission)
            app.state.turn_runner.signal(child)
            await entered.wait()
            pump = app.state.turn_runner._pumps[child]
            try:
                assert app.state.turn_runner.busy(child)
                reservation = app.state.turn_runner._turns[child]
                assert reservation.thread is None and reservation.task is None
            finally:
                release.set()
                await asyncio.shield(pump)
            assert not app.state.turn_runner.busy(child)

    asyncio.run_coroutine_threadsafe(probe(), app.state.mcp_app_loop).result(timeout=10)
    current = app.state.task_history.task_snapshot(task.handle)
    assert current.status == "cancelled" and current.settlement == "confirmed"
    assert app.state.task_history.latest_assignment(child).handle == task.handle
    assert not [
        event
        for event in app.state.bus.session_events_since(child, cursor=1)
        if event.type == "turn.started"
    ]


def test_watcher_profile_auto_allows_only_the_native_alert_card() -> None:
    """The watcher raises alerts through CLIO's ``raise_alert_card``; containment is CLIO's
    SPOTTER clearance, so every other non-read call (including a name that merely looks like
    an alert tool) still asks the person."""
    from clio_agent.gact.permission_gate import default_decision

    assert default_decision("spotter-watcher", "tool", "raise_alert_card", {}) == "allow"
    assert default_decision("spotter-watcher", "tool", "spotter_raise_alert", {}) == "ask"
    assert default_decision("spotter-watcher", "tool", "shell_bash", {}) == "ask"
    assert default_decision("spotter-watcher", "tool", "fs_write_file", {}) == "ask"


def test_cancelled_watcher_cannot_accept_new_evidence(watcher_world: tuple[Any, str, str]) -> None:
    app, parent, child = watcher_world
    task = app.state.task_history.latest_assignment(child)
    disarm_spotter_watcher(app, app.state.sessions.get(parent))
    with pytest.raises(ValueError):
        _push_wake(app, parent, task, "late evidence")
    assert app.state.task_history.pending_mail(child) == ()


_FORBIDDEN_DIRECTIVE_FRAGMENTS = ("check the provenance", "act per your instructions", "you should")


def test_tool_outcome_fact_reports_a_list_valued_entrys_length() -> None:
    """A structural fact -- the first list-valued key's length, named after
    its OWN key -- never an authored guess."""

    assert (
        _tool_outcome_fact({"campaign": None, "runs_checked": 10, "verdicts": [1, 2, 3]})
        == "3 verdicts reported"
    )
    assert _tool_outcome_fact({"runs": ["a", "b", "c", "d", "e"]}) == "5 runs reported: a through e"
    assert (
        _tool_outcome_fact({"runs": [{"run_id": "run-006"}, {"run_id": "run-010"}]})
        == "2 runs reported: run-006 through run-010"
    )


def test_tool_outcome_fact_is_empty_when_nothing_list_shaped() -> None:
    """Never fabricate a count: a scalar-only mapping, a non-mapping, and
    ``None`` all yield '' -- the caller then omits the parenthetical entirely
    rather than inventing a number."""

    assert _tool_outcome_fact({"ok": True, "count": 5}) == ""
    assert _tool_outcome_fact("a plain string result") == ""
    assert _tool_outcome_fact(None) == ""
    assert _tool_outcome_fact([1, 2, 3]) == ""  # a bare list is not a Mapping


def test_wake_text_tool_completed_carries_the_tool_name_and_fact() -> None:
    text = _wake_text(
        tool_name="fs_list_directory", tool_ok=True, tool_result={"entries": [1, 2, 3, 4, 5]}
    )
    assert text == (
        "Watched session activity: fs_list_directory completed "
        "(5 entries reported). The provenance store may have new entries."
    )


def test_wake_text_tool_completed_with_no_derivable_fact_omits_the_parenthetical() -> None:
    text = _wake_text(tool_name="fs_write_file", tool_ok=True, tool_result={"ok": True})
    assert text == (
        "Watched session activity: fs_write_file completed. "
        "The provenance store may have new entries."
    )
    assert "(" not in text


def test_wake_text_tool_failed_states_the_failure_as_the_fact() -> None:
    text = _wake_text(tool_name="shell_bash", tool_ok=False, tool_result=None)
    assert text == (
        "Watched session activity: shell_bash failed. The provenance store may have new entries."
    )


def test_wake_text_turn_final_form_is_the_module_constant() -> None:
    """No ``tool_name`` -- the turn-final form (#1218 e's own worked example)."""

    assert _wake_text() == _WAKE_TURN_FINAL_TEXT
    assert _WAKE_TURN_FINAL_TEXT == (
        "Watched session activity: the session's turn completed. "
        "The provenance store may have new entries."
    )


def test_coalesced_wake_text_names_no_single_tool() -> None:
    assert _WAKE_COALESCED_TEXT == (
        "Watched session activity: multiple tool calls completed. "
        "The provenance store may have new entries."
    )


@pytest.mark.parametrize(
    "text",
    [
        _wake_text(tool_name="fs_list_directory", tool_ok=True, tool_result={"r": [1]}),
        _wake_text(tool_name="shell_bash", tool_ok=False),
        _wake_text(),
        _WAKE_COALESCED_TEXT,
    ],
    ids=["tool_completed", "tool_failed", "turn_final", "coalesced"],
)
def test_no_wake_form_carries_a_directive(text: str) -> None:
    """FACTS ONLY (#1218 e): every wake shape must be free of directive
    language -- the watcher's OWN prompt owns what to do about the facts, not
    this payload."""

    lowered = text.lower()
    assert not any(fragment in lowered for fragment in _FORBIDDEN_DIRECTIVE_FRAGMENTS), text


def _watcher_reads_parent(app: Any, child: str, parent: str) -> bool:
    from clio_agent.gact.routes.session_recall import _recall_tool_policy
    from clio_agent.gact.task_access import TaskAccess

    access = TaskAccess(app.state.task_history, app.state.conversations.caller(child))
    in_candidates = parent in access.session_candidates("read")
    policy = _recall_tool_policy(app, session_id=child, target_session_id=parent, body={})
    assert policy["allowed"] is in_candidates
    return in_candidates


def test_arming_grants_the_watcher_read_on_its_parent_and_disarm_revokes_it(
    watcher_world: tuple[Any, str, str],
) -> None:
    from clio_agent.gact.spotter_watcher import ensure_spotter_watcher

    app, parent, child = watcher_world
    assert not _watcher_reads_parent(app, child, parent)
    session = app.state.sessions.get(parent)
    task = ensure_spotter_watcher(app, session)
    assert task is not None and task.owner.session_id == child
    ensure_spotter_watcher(app, session)  # idempotent: one grant, no rewrite
    grants = [
        grant
        for grant in app.state.task_history.coordination_grants(child)
        if grant.id.startswith("spotter-read:")
    ]
    assert [(g.id, g.target_session, g.rights) for g in grants] == [
        ("spotter-read:" + task.handle, parent, ("read", "observe"))
    ]
    assert grants[0].granting_human == task.owner.human_id and grants[0].revoked_at is None
    assert _watcher_reads_parent(app, child, parent)

    disarm_spotter_watcher(app, session)
    [revoked] = [
        grant
        for grant in app.state.task_history.coordination_grants(child)
        if grant.id == "spotter-read:" + task.handle
    ]
    assert revoked.revoked_at is not None
    assert not _watcher_reads_parent(app, child, parent)


def test_watcher_model_is_the_sessions_choice_then_the_default_then_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from clio_agent import conf
    from clio_agent.gact.fleet.spotter_model import is_person_choice, watcher_model_ref

    def session(**metadata: Any) -> Any:
        return SimpleNamespace(id="s", approval_mode="spotter-ai", metadata=metadata)

    monkeypatch.delenv("CLIO_FLEET_SPOTTER", raising=False)
    conf.reload()
    assert watcher_model_ref(None, session()) == ""  # type: ignore[arg-type]
    monkeypatch.setenv("CLIO_FLEET_SPOTTER", '{"provider_id": "server-b", "model_id": "small"}')
    conf.reload()
    assert watcher_model_ref(None, session()) == "server-b/small"  # type: ignore[arg-type]
    assert watcher_model_ref(None, session(spotter_model="  ")) == "server-b/small"  # type: ignore[arg-type]
    own = session(spotter_model=" server-a/big ")
    assert watcher_model_ref(None, own) == "server-a/big"  # type: ignore[arg-type]
    # Only the watcher agent's spawn with exactly that model is the person's choice.
    assert is_person_choice(None, own, "clio/spotter-ai", "server-a/big")  # type: ignore[arg-type]
    assert not is_person_choice(None, own, "clio/other", "server-a/big")  # type: ignore[arg-type]
    assert not is_person_choice(None, own, "clio/spotter-ai", "server-b/small")  # type: ignore[arg-type]
    own.approval_mode = "ask"
    assert not is_person_choice(None, own, "clio/spotter-ai", "server-a/big")  # type: ignore[arg-type]
