"""Topic discovery uses authorized source messages and explicit discussion times."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from clio_agent.arc.task_types import TaskSnapshot
from clio_agent.gact.task_declarations import build_task_tools
from clio_agent.gact.types import Message, Part
from tests.test_gact import test_task_transcript_views as transcript_fixtures
from tests.test_gact.task_records import active_turn_fixture, record_delegation_fixture
from tests.test_gact.test_task_transcript_views import TranscriptWorld

transcript_world = transcript_fixtures.transcript_world


def message_at(
    world: TranscriptWorld,
    text: str,
    minute: int,
    identity: str,
    *,
    task: TaskSnapshot | None = None,
) -> Message:
    """Persist source prose at an explicit instant after its assignment began."""
    task = task or world.task
    stamp = datetime.fromisoformat(task.created_at) + timedelta(minutes=minute)
    row = Message(
        id=identity,
        session_id=task.owner.session_id,
        turn_id=task.invocation_id,
        role="assistant",
        created_at=stamp.isoformat(),
        updated_at=stamp.isoformat(),
        parts=[Part(id="part_" + identity, type="text", text=text)],
    )
    world.app.state.messages[row.session_id] = [
        *world.app.state.messages.get(row.session_id, ()),
        row,
    ]
    return row


def bounds(world: TranscriptWorld) -> dict[str, str]:
    """Use a window later than assignment creation so creation-time filtering fails."""
    start = datetime.fromisoformat(world.task.created_at) + timedelta(hours=1)
    return {
        "message_after": start.isoformat(),
        "message_before": (start + timedelta(hours=1)).isoformat(),
    }


@pytest.mark.parametrize("kind", [None, "Session", "Subagent"])
def test_topic_search_finds_permitted_sessions_and_children(
    transcript_world: TranscriptWorld, kind: str | None
) -> None:
    """Topic words need not be a session title, and broader scope still needs grants."""
    world = transcript_world
    world.grant()
    peer = message_at(world, "Keep catering affordable and preserve contingency.", 80, "peer")
    child = record_delegation_fixture(world.app, world.reader, handle="task_topic_child")
    message_at(world, "Catering and contingency need another check.", 80, "child", task=child)
    query = dict(kind=kind, query="catering contingency", **bounds(world))
    tree = world.app.state.task_operations.query(world.caller, **query)
    expected_tree = [] if kind == "Session" else [child.handle]
    assert [item["handle"] for item in tree["tasks"]] == expected_tree
    found = world.app.state.task_operations.query(world.caller, scope="authorized", **query)
    expected = [task.handle for task in (world.task, child) if kind is None or task.kind == kind]
    assert [item["handle"] for item in found["tasks"]] == expected
    assert found["total"] is None and found.collect is False
    if kind != "Subagent":
        read = world.observe(view="transcript")
        assert peer.to_wire() in read["tasks"][0]["items"]


def test_window_uses_message_time_and_half_open_timezone_aware_bounds(
    transcript_world: TranscriptWorld,
) -> None:
    """An older assignment matches its later discussion, including the lower boundary."""
    world = transcript_world
    world.grant()
    message_at(world, "Lower boundary decision.", 60, "lower")
    message_at(world, "Upper boundary decision.", 120, "upper")
    limits = bounds(world)
    start = datetime.fromisoformat(limits["message_after"])
    limits["message_after"] = start.astimezone(timezone(timedelta(hours=-5))).isoformat()
    assert datetime.fromisoformat(world.task.created_at) < start
    operations = world.app.state.task_operations
    found = operations.query(world.caller, scope="authorized", query="lower", **limits)
    assert [row["handle"] for row in found["tasks"]] == [world.task.handle]
    assert not operations.query(world.caller, scope="authorized", query="upper", **limits)["tasks"]


@pytest.mark.parametrize("boundary", ["message_after", "message_before"])
def test_each_message_time_bound_can_be_used_alone(
    transcript_world: TranscriptWorld, boundary: str
) -> None:
    """Omitting one side leaves that end unbounded."""
    world = transcript_world
    world.grant()
    message_at(world, "Catering contingency decision.", 80, "source")
    limits = {boundary: bounds(world)[boundary]}
    found = world.app.state.task_operations.query(
        world.caller, scope="authorized", query="catering contingency", **limits
    )
    assert [row["handle"] for row in found["tasks"]] == [world.task.handle]


def test_window_cannot_borrow_topic_words_from_an_older_discussion(
    transcript_world: TranscriptWorld,
) -> None:
    """Recent unrelated activity cannot make an old topic appear discussed recently."""
    world = transcript_world
    world.grant()
    message_at(world, "Catering was discussed earlier.", 0, "old")
    message_at(world, "Contingency is being discussed now.", 80, "recent")
    operations = world.app.state.task_operations
    assert operations.query(world.caller, scope="authorized", query="catering contingency")["tasks"]
    assert not operations.query(
        world.caller, scope="authorized", query="catering contingency", **bounds(world)
    )["tasks"]
    assert operations.query(world.caller, scope="authorized", **bounds(world))["tasks"]


def test_window_cannot_substitute_an_undated_assignment_description(
    transcript_world: TranscriptWorld,
) -> None:
    """Description search remains available, but does not prove when a topic was discussed."""
    world = transcript_world
    world.grant()
    message_at(world, "An unrelated discussion.", 80, "recent")
    operations = world.app.state.task_operations
    assert operations.query(world.caller, scope="authorized", query="transport")["tasks"]
    assert not operations.query(
        world.caller, scope="authorized", query="transport", **bounds(world)
    )["tasks"]


def test_even_a_date_only_search_requires_read_permission(
    transcript_world: TranscriptWorld,
) -> None:
    """Dates and topic matches cannot disclose an observe-only peer's conversation activity."""
    world = transcript_world
    world.grant(("observe",))
    message_at(world, "Catering contingency private decision.", 80, "private")
    for text in (None, "catering"):
        result = world.app.state.task_operations.query(
            world.caller, scope="authorized", query=text, **bounds(world)
        )
        assert result["tasks"] == [] and result["total"] is None
    world.grant(("read",))
    result = world.app.state.task_operations.query(
        world.caller, scope="authorized", **bounds(world)
    )
    assert [row["handle"] for row in result["tasks"]] == [world.task.handle]


@pytest.mark.parametrize(
    "limits",
    [
        {"message_after": "not-a-date"},
        {"message_after": "2026-10-19T00:00:00"},
        {"message_before": "2026-10-20"},
        {"message_after": "2026-02-31T00:00:00Z"},
        {"message_after": "2026-10-20T00:00:00Z", "message_before": "2026-10-20T00:00:00Z"},
        {"message_after": "2026-10-20T00:00:00Z", "message_before": "2026-10-19T00:00:00Z"},
    ],
)
def test_invalid_windows_are_rejected_before_query_execution(
    transcript_world: TranscriptWorld, limits: dict[str, str]
) -> None:
    """Invalid dates, naive timestamps and empty/reversed windows are public errors."""
    world = transcript_world
    with pytest.raises(ValueError, match="message_") as error:
        world.app.state.task_operations.query(world.caller, scope="authorized", **limits)
    response = world.client.get(f"/v1/sessions/{world.reader}/tasks/query", params=limits)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"] == str(error.value)
    assert response.json()["error"]["recoverable"] is True


def test_cursor_binds_normalized_message_times_and_preserves_legacy_omission(
    transcript_world: TranscriptWorld,
) -> None:
    """Pagination accepts equivalent timezone spellings, not a different time window."""
    world = transcript_world
    world.grant()
    message_at(world, "Catering contingency first decision.", 80, "first")
    child = record_delegation_fixture(world.app, world.reader, handle="task_page_child")
    message_at(world, "Catering contingency second decision.", 80, "second", task=child)
    operations = world.app.state.task_operations
    options: dict[str, Any] = {"scope": "authorized", "query": "catering", "limit": 1}
    limits = bounds(world)
    first = operations.query(world.caller, **options, **limits)
    assert first["cursor"] and len(first["tasks"]) == 1
    equivalent = dict(limits)
    equivalent["message_after"] = (
        datetime.fromisoformat(limits["message_after"])
        .astimezone(timezone(timedelta(hours=-5)))
        .isoformat()
    )
    following = operations.query(world.caller, cursor=first["cursor"], **options, **equivalent)
    assert [row["handle"] for row in following["tasks"]] == [child.handle]
    with pytest.raises(ValueError, match="different query"):
        operations.query(world.caller, cursor=first["cursor"], **options)
    legacy = operations.query(world.caller, **options)
    assert legacy["cursor"]
    following = operations.query(
        world.caller, cursor=legacy["cursor"], message_after=None, message_before=None, **options
    )
    assert [row["handle"] for row in following["tasks"]] == [child.handle]


def test_native_and_http_queries_share_the_same_message_time_contract(
    transcript_world: TranscriptWorld,
) -> None:
    """Agent tools and both human HTTP routes use the same search without collecting results."""
    world = transcript_world
    world.grant()
    message_at(world, "Catering contingency decision.", 80, "source")
    before = world.app.state.task_history.acknowledged_notifications(world.reader)
    tool = next(item for item in build_task_tools() if item.name == "query_tasks")
    schema = tool.format_as_litellm_function_call()["function"]["parameters"]
    for key in ("message_after", "message_before"):
        assert key in schema["properties"] and key not in schema.get("required", [])
    params = {
        "kind": "Session",
        "scope": "authorized",
        "query": "catering contingency",
        **bounds(world),
    }
    with active_turn_fixture(world.app, world.reader):
        native = asyncio.run_coroutine_threadsafe(
            tool.func(**params), world.app.state.conversations._loop
        ).result(timeout=10)
    assert [row["handle"] for row in native["tasks"]] == [world.task.handle]
    assert native.notification_ids == () and native.collect is False
    for endpoint in (f"/v1/sessions/{world.reader}/tasks/query", "/v1/tasks/query"):
        response = world.client.get(endpoint, params=params)
        assert response.status_code == 200, response.text
        assert [row["handle"] for row in response.json()["tasks"]] == [world.task.handle]
    assert world.app.state.task_history.acknowledged_notifications(world.reader) == before
