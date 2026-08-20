"""P4.3 (#1081): the model-callable cron triad + /cron command + cancel-both.

Drives cron_create/cron_list/cron_delete through the same active-app/session context the
react loop establishes (:mod:`clio_agent.gact.context`), asserting the server-generated
result-only id, the read-back, and cancel-both (deleting also clears any daemon deferred
entry — no orphan tick). Also asserts the built-in /cron command row exists.

Each test body runs inside a fresh ``contextvars.copy_context()`` so the (reset-less)
``set_app``/``set_session_id`` bindings never leak between tests or into other suites.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from clio_agent.gact import context as _ctx
from clio_agent.gact.app import build_app  # module-level so the autouse ARC fixture patches it
from clio_agent.gact.cron_tools import (
    build_cron_create_tool,
    build_cron_delete_tool,
    build_cron_list_tool,
    run_cron_command,
)
from clio_agent.gact.scheduler import CronError, ScheduleStore


def _app(tmp_path: Path) -> SimpleNamespace:
    """A minimal app carrying only what the cron tools touch."""

    state = SimpleNamespace(
        schedules=ScheduleStore(path=tmp_path / "schedules.json"),
        deferred_schedules=set(),
    )
    return SimpleNamespace(state=state)


def _in_ctx(fn: Callable[[], None]) -> None:
    """Run ``fn`` in an isolated copy of the current context (no binding leak)."""

    contextvars.copy_context().run(fn)


def _bind(app: object, sid: str) -> None:
    _ctx.set_app(app)
    _ctx.set_session_id(sid)


def test_cron_create_returns_result_only_id_list_reads_back(tmp_path: Path) -> None:
    """create returns a server-generated id in the RESULT; list reads it back."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_1")
        result = build_cron_create_tool().func(cron="0 9 * * *", prompt="daily standup")
        assert result["schedule_id"].startswith("sched_")
        assert result["recurring"] is True
        assert result["next_fire_at"]
        assert result["timezone"]

        listed = build_cron_list_tool().func()
        assert len(listed) == 1
        assert listed[0]["id"] == result["schedule_id"]
        assert listed[0]["prompt"] == "daily standup"
        assert listed[0]["cron"] == "0 9 * * *"

    _in_ctx(body)


def test_cron_delete_cancel_both_no_orphan(tmp_path: Path) -> None:
    """delete removes the store row AND clears a queued deferred entry (cancel-both)."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_1")
        result = build_cron_create_tool().func(cron="0 9 * * *", prompt="q")
        sid = result["schedule_id"]
        # Simulate the daemon having deferred this schedule (busy session).
        app.state.deferred_schedules.add(sid)

        deleted = build_cron_delete_tool().func(schedule_id=sid)
        assert deleted is True
        # Store row gone AND the deferred entry cleared — no orphan tick can resurrect it.
        assert app.state.schedules.get(sid) is None
        assert sid not in app.state.deferred_schedules
        # Idempotent.
        assert build_cron_delete_tool().func(schedule_id=sid) is False

    _in_ctx(body)


def test_cron_delete_cross_session_refused_row_intact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A model in session B cannot delete session A's schedule (no cross-session oracle).

    The refusal returns ``False`` (indistinguishable from not-found to the model), leaves
    the row AND any deferred entry intact, and emits the typed ``cron_delete_not_owner``
    reason server-side (no-silent-refusal)."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_A")
        result = build_cron_create_tool().func(cron="0 9 * * *", prompt="A's job")
        sid = result["schedule_id"]
        app.state.deferred_schedules.add(sid)

        # Switch to session B and attempt to delete A's schedule.
        _bind(app, "sess_B")
        with caplog.at_level("WARNING", logger="clio_agent.gact.cron_tools"):
            deleted = build_cron_delete_tool().func(schedule_id=sid)
        assert deleted is False  # indistinguishable from not-found
        # Row + deferred entry untouched — no cross-session cancellation.
        assert app.state.schedules.get(sid) is not None
        assert sid in app.state.deferred_schedules
        # Typed refusal reason surfaced server-side.
        assert "cron_delete_not_owner" in caplog.text

    _in_ctx(body)


def test_cron_delete_own_session_cancels_both(tmp_path: Path) -> None:
    """The owning session deletes successfully (True) with cancel-both."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_A")
        result = build_cron_create_tool().func(cron="0 9 * * *", prompt="A's job")
        sid = result["schedule_id"]
        app.state.deferred_schedules.add(sid)

        deleted = build_cron_delete_tool().func(schedule_id=sid)
        assert deleted is True
        assert app.state.schedules.get(sid) is None
        assert sid not in app.state.deferred_schedules

    _in_ctx(body)


def test_cron_create_one_shot_recurring_false(tmp_path: Path) -> None:
    """recurring=False arms a one-shot (auto-deletes after firing)."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        result = build_cron_create_tool().func(cron="0 9 * * *", prompt="q", recurring=False)
        assert result["recurring"] is False

    _in_ctx(body)


def test_cron_create_bad_cron_is_typed_error(tmp_path: Path) -> None:
    """A malformed cron surfaces a typed CronError the model can repair against."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        with pytest.raises(CronError) as ei:
            build_cron_create_tool().func(cron="99 * * * *", prompt="q")
        assert ei.value.reason == "invalid_cron"

    _in_ctx(body)


def test_cron_tools_require_active_session() -> None:
    """Outside a live turn (no app/session bound) the tools raise no_active_session."""

    def body() -> None:
        _ctx.set_app(None)
        _ctx.set_session_id("")
        with pytest.raises(CronError) as ei:
            build_cron_list_tool().func()
        assert ei.value.reason == "no_active_session"

    _in_ctx(body)


def test_cron_command_row_exists() -> None:
    """The built-in /cron command row is present and available."""

    from clio_agent.gact.runtime.commands import BACKEND_COMMANDS

    rows = {row["id"]: row for row in BACKEND_COMMANDS}
    assert "/cron" in rows
    cron_row = rows["/cron"]
    assert cron_row["status"] == "available"
    assert cron_row["enabled"] is True
    assert "/schedule" in cron_row.get("aliases", [])


# --------------------------------------------------------------------------- #
# run_cron_command — the /cron user-command dispatch (P4.3 #1081 gap fix).      #
# Mirrors run_loop_command / run_goal_command: same body-parse + typed errors. #
# --------------------------------------------------------------------------- #
def test_run_cron_command_create_text_form_registers_schedule(tmp_path: Path) -> None:
    """`/cron <5-field-cron> <prompt>` (text form) arms a schedule; body carries id + next."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_1")
        msg = run_cron_command(app, "sess_1", {"input": "0 9 * * * post the daily standup"})
        rows = app.state.schedules.list(session_id="sess_1")
        assert len(rows) == 1
        sch = rows[0]
        assert sch.cron == "0 9 * * *"
        assert sch.question == "post the daily standup"
        assert sch.recurring is True
        # Result body carries the server-generated id AND the next_fire_at (result-only id).
        assert sch.id in msg
        assert sch.next_fire_at in msg
        assert "armed" in msg.lower()

    _in_ctx(body)


def test_run_cron_command_create_args_form_registers_schedule(tmp_path: Path) -> None:
    """Structured args (cron/prompt) arm a schedule identically to the text form."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        msg = run_cron_command(
            app, "s", {"args": {"cron": "*/15 * * * *", "prompt": "poll the queue"}}
        )
        rows = app.state.schedules.list(session_id="s")
        assert len(rows) == 1
        assert rows[0].cron == "*/15 * * * *"
        assert rows[0].question == "poll the queue"
        assert rows[0].id in msg

    _in_ctx(body)


def test_run_cron_command_list_reads_back_armed_schedules(tmp_path: Path) -> None:
    """`/cron list` renders the session's armed schedules (id + cron + prompt)."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        run_cron_command(app, "s", {"input": "0 9 * * * daily digest"})
        listing = run_cron_command(app, "s", {"input": "list"})
        sch = app.state.schedules.list(session_id="s")[0]
        assert sch.id in listing
        assert "0 9 * * *" in listing
        assert "daily digest" in listing

    _in_ctx(body)


def test_run_cron_command_delete_cancels_both(tmp_path: Path) -> None:
    """`/cron delete <id>` removes the store row AND clears a queued deferred entry."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        run_cron_command(app, "s", {"input": "0 9 * * * q"})
        sch_id = app.state.schedules.list(session_id="s")[0].id
        app.state.deferred_schedules.add(sch_id)

        msg = run_cron_command(app, "s", {"input": f"delete {sch_id}"})
        assert "cancelled" in msg.lower()
        assert app.state.schedules.get(sch_id) is None
        assert sch_id not in app.state.deferred_schedules
        # Idempotent: a second delete reports nothing to cancel, creates/removes nothing.
        again = run_cron_command(app, "s", {"input": f"cancel {sch_id}"})
        assert "no schedule" in again.lower()

    _in_ctx(body)


def test_run_cron_command_delete_cross_session_refused_row_intact(tmp_path: Path) -> None:
    """`/cron delete <id>` for another session's schedule reports nothing to cancel and
    leaves the row intact (same owner check as the model tool, no cross-session oracle)."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_A")
        run_cron_command(app, "sess_A", {"input": "0 9 * * * q"})
        sch_id = app.state.schedules.list(session_id="sess_A")[0].id

        msg = run_cron_command(app, "sess_B", {"input": f"delete {sch_id}"})
        assert "no schedule" in msg.lower()
        # A's row is untouched.
        assert app.state.schedules.get(sch_id) is not None

    _in_ctx(body)


def test_run_cron_command_delete_via_args_schedule_id(tmp_path: Path) -> None:
    """The delete id may come from args.schedule_id (not just the next token)."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        run_cron_command(app, "s", {"input": "0 9 * * * q"})
        sch_id = app.state.schedules.list(session_id="s")[0].id
        msg = run_cron_command(app, "s", {"input": "delete", "args": {"schedule_id": sch_id}})
        assert "cancelled" in msg.lower()
        assert app.state.schedules.get(sch_id) is None

    _in_ctx(body)


def test_run_cron_command_subfloor_returns_typed_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-floor recurring cron surfaces the typed min_interval rejection, creates nothing."""

    monkeypatch.setenv("CLIO_SCHEDULER_MIN_INTERVAL_S", "300")

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        msg = run_cron_command(app, "s", {"input": "* * * * * hammer the api"})  # 60s < 300s
        assert msg.startswith("/cron rejected:")
        assert "min_interval_below_floor" in msg  # reason surfaced for the model to repair
        assert app.state.schedules.list(session_id="s") == []  # nothing armed

    _in_ctx(body)


def test_run_cron_command_usage_when_no_trigger_creates_nothing(tmp_path: Path) -> None:
    """A create with no cron/run_at/delay returns a usage string and arms nothing."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        msg = run_cron_command(app, "s", {"input": "remind me"})
        assert "usage" in msg.lower()
        assert app.state.schedules.list(session_id="s") == []

    _in_ctx(body)


def test_run_cron_command_empty_lists_empty_state_creates_nothing(tmp_path: Path) -> None:
    """A bare /cron (empty text, no args) lists the empty state and creates nothing."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        msg = run_cron_command(app, "s", {"input": ""})
        assert "no schedules" in msg.lower()
        assert app.state.schedules.list(session_id="s") == []

    _in_ctx(body)


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""


class _Agent:
    def forward(self, *args: object, **kwargs: object) -> _Pred:
        return _Pred()


# --------------------------------------------------------------------------- #
# Declared structured_content (P5 wire semantics) — the wait_agent_tasks       #
# treatment extended to the cron triad.                                        #
# --------------------------------------------------------------------------- #


def test_cron_create_declares_typed_structured_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declared: list[dict] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_1")
        result = build_cron_create_tool().func(cron="0 9 * * *", prompt="daily standup")
        assert len(declared) == 1
        shape = declared[0]
        assert next(iter(shape)) == "message"
        assert shape["message"] == (
            f"armed schedule {result['schedule_id']} — recurring cron 0 9 * * *; "
            f"next fire {result['next_fire_at']}"
        )
        # SAME facts as the model-facing return, riding after the message.
        assert {k: v for k, v in shape.items() if k != "message"} == result

    _in_ctx(body)


def test_cron_list_declares_typed_structured_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declared: list[dict] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_1")
        # Empty case first: the honest "no schedules" message, never "0 schedules: ".
        build_cron_list_tool().func()
        assert declared[-1] == {"message": "no schedules armed for this session", "schedules": []}

        build_cron_create_tool().func(cron="0 9 * * *", prompt="daily standup")
        build_cron_create_tool().func(prompt="one shot", delay_s=60, recurring=False)
        listed = build_cron_list_tool().func()

        shape = declared[-1]
        assert next(iter(shape)) == "message"
        assert shape["message"] == "2 schedules: 1 recurring, 1 one-shot"
        assert shape["schedules"] == listed

    _in_ctx(body)


def test_cron_delete_declares_typed_structured_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declared: list[dict] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_1")
        result = build_cron_create_tool().func(cron="0 9 * * *", prompt="q")
        sid = result["schedule_id"]

        deleted = build_cron_delete_tool().func(schedule_id=sid)
        assert deleted is True
        assert declared[-1] == {
            "message": f"cancelled schedule {sid}",
            "schedule_id": sid,
            "deleted": True,
        }

        # Idempotent re-delete: an honest "nothing to cancel" message, still declared.
        again = build_cron_delete_tool().func(schedule_id=sid)
        assert again is False
        assert declared[-1] == {
            "message": f"no schedule {sid} to cancel (already gone or never armed)",
            "schedule_id": sid,
            "deleted": False,
        }

    _in_ctx(body)


@pytest.mark.usefixtures("host_agent_executor")
def test_catalog_dispatch_routes_cron_and_schedule(tmp_path: Path) -> None:
    """The catalog command handler routes BOTH /cron and its /schedule alias to
    run_cron_command (the P4.3 #1081 gap: /cron had no dispatch branch, so it returned
    'unhandled command' and created nothing)."""

    from fastapi.testclient import TestClient

    # Context-managed so the lifespan runs and ARC is wired (the dispatch route emits a
    # semantic event through the strict ARC-as-source highway).
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent())) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

        # /cron CREATE routes to run_cron_command and actually arms a schedule.
        resp = client.post(
            f"/v1/sessions/{sid}/commands/cron", json={"input": "0 9 * * * daily standup"}
        )
        assert resp.status_code == 200
        text = resp.json()["result"]["text"]
        assert "unhandled command" not in text
        assert "armed" in text.lower()
        armed = client.app.state.schedules.list(session_id=sid)
        assert len(armed) == 1
        assert armed[0].cron == "0 9 * * *"

        # /schedule ALIAS resolves to the same handler (reads the schedule back, not a 404).
        resp2 = client.post(f"/v1/sessions/{sid}/commands/schedule", json={"input": "list"})
        assert resp2.status_code == 200
        text2 = resp2.json()["result"]["text"]
        assert "unhandled command" not in text2
        assert armed[0].id in text2
