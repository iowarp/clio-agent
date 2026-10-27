"""Shared helpers for the GACT test suite.

POST /messages used to be synchronous (the response body carried
the assistant message). It now returns an ack
``{message_id, accepted_at}`` and the assistant turn arrives
asynchronously via SSE / GET /messages. ``complete_turn`` wraps that
flow for tests that just want "POST + return the assistant".
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import ExitStack
from functools import wraps
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_arc.conftest import core_arc as core_arc
from tests.test_gact.spotter_records import watcher_world as watcher_world


@pytest.fixture
def tool_declaration_scope() -> Iterator[None]:
    """Restore the caller context when testing declaration producers directly."""
    from clio_agent.gact.agents.tool_instrumentation import _DECLARED_STRUCTURED_CONTENT

    token = _DECLARED_STRUCTURED_CONTENT.set(None)
    try:
        yield
    finally:
        _DECLARED_STRUCTURED_CONTENT.reset(token)


@pytest.fixture(name="world")
def question_world(tmp_path: Path) -> Iterator[tuple[Any, TestClient, str]]:
    """Share the actual Question application fixture with its answer regressions."""
    from clio_agent.gact.app import build_app
    from tests.test_gact.task_records import record_assignment_fixture

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Questions"}).json()["id"]
        record_assignment_fixture(app, sid)
        yield app, client, sid


@pytest.fixture(autouse=True)
def _released_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deploy plans built here come from a released CLIO, not this test checkout's commit."""
    for owner in ("drivers", "controller_source"):
        monkeypatch.setattr(
            f"clio_agent.gact.infrastructure.{owner}.controller_commit", lambda: None
        )


@pytest.fixture(autouse=True)
def a2ui_session_lifespan(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Own real app startup and shutdown for the retained A2UI client helper."""
    module = request.module
    original = getattr(module, "_session_client", None)
    if not callable(original) or original.__module__ != "tests.test_gact.test_a2ui_v3":
        yield
        return
    with ExitStack() as stack:

        @wraps(original)
        def started(*args: Any, **kwargs: Any) -> Any:
            client, session_id, path = original(*args, **kwargs)
            stack.enter_context(client)
            return client, session_id, path

        monkeypatch.setattr(module, "_session_client", started)
        yield


@pytest.fixture
def native_builtin_lifespan(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Own real native app lifespans and dispatch opted-in tool calls through MCP."""
    from fastapi import FastAPI

    from clio_agent.gact import context
    from clio_agent.gact.agents.tool_instrumentation import _clio_native_tool_class
    from tests.test_gact.task_records import record_assignment_fixture

    tool_class = _clio_native_tool_class()
    original = tool_class.__call__
    with ExitStack() as stack:

        @wraps(original)
        def invoke(tool: Any, *positional: Any, **keywords: Any) -> Any:
            app, sid = context.active_app(), context.active_session_id()
            if isinstance(app, FastAPI) and sid and getattr(tool.func, "_clio_builtin", False):
                if not getattr(app.state, "native_tools", None):
                    stack.enter_context(TestClient(app))
                if app.state.task_history.latest_assignment(sid) is None:
                    record_assignment_fixture(app, sid)
                tool = app.state.builtin_boundary.bind(tool)
            return original(tool, *positional, **keywords)

        monkeypatch.setattr(tool_class, "__call__", invoke)
        yield


@pytest.fixture
def builtin_tool_context(world: tuple[Any, TestClient, str]) -> Iterator[None]:
    """Bind an actual Core assignment and MCP boundary for standalone tool checks."""
    from tests.test_gact.task_records import active_turn_fixture

    app, _client, session_id = world
    with active_turn_fixture(app, session_id):
        yield


@pytest.fixture(autouse=True)
def _default_test_arc(request, tmp_path, monkeypatch):
    """Give every GACT test app a real, per-app ARC unless it asked otherwise.

    ARC is the source of the highway: ``_emit_semantic_event`` routes EVERY event through
    ``arc.record_semantic_event``, which records ARC's view and then DERIVES the highway
    (trace/SSE/hooks) via the sink wired by ``_set_app_arc``. It now fails loud when no ARC
    is reachable, so the many tests that build ``build_app(...)`` without an ``arc`` -- and
    which previously relied on a silent ``sink.emit`` bypass -- need a real ARC. A real
    ``ARCMemory`` (not a stub) is used because the GACT layer also drives its op-logger,
    cache-stats and live-fold surfaces, which the stub would not provide.

    Wraps the ``build_app`` bound in the test module so a call that OMITS ``arc`` gets a
    fresh ``ARCMemory`` rooted in the test's ``tmp_path`` (each app its OWN arc, so the
    derive sink wires to that app's bus). Calls that pass ``arc=`` explicitly -- including
    the ones that exercise the arc-absent path with ``arc=None`` -- are left as written.
    """
    module = request.module
    real_build_app = getattr(module, "build_app", None)
    if real_build_app is None:
        yield
        return

    from clio_agent.arc.memory import ARCMemory
    from clio_agent.arc.storage import make_arc_store

    counter = {"n": 0}

    def _build_app_with_default_arc(*args, **kwargs):
        if "arc" not in kwargs:
            counter["n"] += 1
            # Native task hosts require the same confirmed Core writer as production.
            # The suite's private daemon and namespace fixture own and clear this store.
            kwargs["arc"] = ARCMemory(
                data_dir=str(tmp_path / f"arc_{counter['n']}"),
                store=make_arc_store(backend="cte"),
            )
        return real_build_app(*args, **kwargs)

    monkeypatch.setattr(module, "build_app", _build_app_with_default_arc)
    yield


def _fold_published_parts(history: list[Any], message_id: str) -> list[dict[str, Any]] | None:
    """Fold ``message.*`` wire events into the parts list a client would hold.

    Returns ``None`` when ``message_id`` was never minted on this bus as a
    turn's assistant message (a ``message.created`` with ``role: assistant``
    and EMPTY parts — direct API inserts and the #756 envelope's fresh error
    message carry no such event, and a trimmed history loses it first).
    """

    minted = False
    parts: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for event in history:
        payload = event.payload
        if event.type == "message.created":
            if (
                payload.get("id") == message_id
                and payload.get("role") == "assistant"
                and not payload.get("parts")
            ):
                minted = True
            continue
        if payload.get("message_id") != message_id:
            continue
        if event.type == "message.part.added":
            part = dict(payload.get("part") or {})
            folded = {
                "id": part.get("id", ""),
                "type": part.get("type", ""),
                "agent_id": part.get("agent_id", ""),
                "text": part.get("text", "") or "",
                # #883: fold tool_call.thought so live==reload ranges over the field
                # the presentation model dedups (a tool_call gets one added event and
                # no delta/completed, so its thought is final at add).
                "thought": part.get("thought", "") or "",
                "stream_source": str((part.get("metadata") or {}).get("stream_source") or ""),
                "live_text": (
                    payload.get("stream_source") == "live"
                    and part.get("type") in {"text", "thinking"}
                ),
                "completed": False,
            }
            parts.append(folded)
            by_id[folded["id"]] = folded
        elif event.type == "message.part.delta":
            delta_part = by_id.get(str(payload.get("part_id") or ""))
            if delta_part is not None:
                delta_part["text"] += str((payload.get("delta") or {}).get("text_append") or "")
        elif event.type == "message.part.completed":
            completed_part = by_id.get(str(payload.get("part_id") or ""))
            if completed_part is not None:
                completed_part["completed"] = True
                completed_part["text"] = str(payload.get("final_text") or "")
    if not minted:
        return None
    # A live streamed text/thinking part that never completed was dropped from
    # the ledger (empty after clean) — a fold consumer discards it too, so
    # live and reload cannot disagree (design §4 row 4).
    return [p for p in parts if not (p["live_text"] and not p["completed"])]


@pytest.fixture(autouse=True)
def _live_equals_reload_property(monkeypatch):
    """#767 PR3 (design §8.2b): live == reload, enforced for EVERY gact turn.

    Folding the published ``message.created`` / ``part.added`` / ``part.delta``
    / ``part.completed`` stream must reconstruct the persisted assistant
    ``Message.parts`` field-for-field (id, arrival-order sequence, type,
    agent_id, text/final_text, stream_source). Wraps the persistence funnel so
    every existing scenario (streaming, SSE, thinking blocks, delegation,
    cancellation) doubles as a regression for the reconciliation-drift class
    this epic ends.
    """

    from clio_agent.gact import app as gact_app

    violations: list[str] = []
    real_append = gact_app._append_session_message

    def _checked_append(app, sid, msg, *args, **kwargs):
        result = real_append(app, sid, msg, *args, **kwargs)
        try:
            if getattr(msg, "role", "") != "assistant":
                return result
            history = list(app.state.bus._history.get(sid, []))
            folded = _fold_published_parts(history, msg.id)
            if folded is None:
                return result
            persisted = [
                {
                    "id": part.id,
                    "sequence": part.sequence,
                    "type": part.type,
                    "agent_id": part.agent_id,
                    "text": part.text or "",
                    "thought": part.thought or "",
                    "stream_source": str(part.metadata.get("stream_source") or ""),
                }
                for part in msg.parts
            ]
            reconstructed = [
                {
                    "id": p["id"],
                    "sequence": index,
                    "type": p["type"],
                    "agent_id": p["agent_id"],
                    "text": p["text"],
                    "thought": p["thought"],
                    "stream_source": p["stream_source"],
                }
                for index, p in enumerate(folded, start=1)
            ]
            if reconstructed != persisted:
                violations.append(
                    f"live==reload violated for message {msg.id!r} (session {sid!r}):\n"
                    f"  folded SSE stream -> {reconstructed}\n"
                    f"  persisted parts   -> {persisted}"
                )
        except Exception as exc:  # noqa: BLE001 - the property check must never mask the turn
            violations.append(
                f"live==reload check crashed for {getattr(msg, 'id', '?')!r}: {exc!r}"
            )
        return result

    monkeypatch.setattr(gact_app, "_append_session_message", _checked_append)
    yield
    assert not violations, "\n".join(violations)


# ``host_agent_executor`` (the #948 S4b turn-executor seam) is defined in the ROOT
# ``tests/conftest.py`` so every suite that drives a turn with a ``build_app``
# host fake (test_gact, test_client, test_ui) can opt into it by name.


def settle_turn_slot(client: TestClient, sid: str, *, timeout: float = 30.0) -> None:
    """Wait until the session's turn slot is free (``TurnRunner.busy`` False).

    ``complete_turn`` returns when the assistant message is persisted, and since #1333
    the GOAL judge is AWAITED after that persistence (the loop is live during the judge),
    so "the assistant replied" no longer implies "the goal was evaluated". Tests that
    assert goal/loop metadata after a turn wait for the slot to clear first. A plain dict
    read on the runner, safe from the test thread."""

    runner = cast(FastAPI, client.app).state.turn_runner
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump = runner._pumps.get(sid)
        if not runner.busy(sid) and (pump is None or pump.done()):
            _await_release_callbacks(runner, sid, max(deadline - time.monotonic(), 1.0))
            pump = runner._pumps.get(sid)
            if not runner.busy(sid) and (pump is None or pump.done()):
                return
        time.sleep(0.02)
    raise TimeoutError(f"turn slot for {sid} still busy after {timeout}s")


def _await_release_callbacks(runner: Any, sid: str, backstop_s: float) -> None:
    """Cross the app-loop boundary after physical reservation retirement and callbacks."""

    import threading  # noqa: PLC0415

    loop = runner._loop
    if loop is None:
        raise AssertionError(f"Admitted turn for {sid} has no application loop")
    drained = threading.Event()
    try:
        loop.call_soon_threadsafe(drained.set)
    except RuntimeError:
        raise AssertionError(f"The application loop closed before {sid} retired") from None
    if not drained.wait(backstop_s):
        raise TimeoutError(f"the app loop did not drain within {backstop_s:g}s")


def complete_turn(
    client: TestClient,
    sid: str,
    text: str,
    *,
    json_override: dict[str, Any] | None = None,
    # 30s: post-S4b every turn builds+runs a real blueprint module through the
    # single dispatch branch; 10s was calibrated for the deleted legacy fake
    # dispatch and flaked on slow 2-core CI runners (settle timeouts on green
    # local runs).
    timeout: float = 30.0,
    poll_interval: float = 0.05,
) -> dict[str, Any]:
    """POST a user message, wait for the assistant turn to settle,
    return the assistant message dict.

    Polls ``GET /v1/sessions/{sid}/messages`` until either:

    - The user message we just POSTed is followed (chronologically)
      by an assistant message — that's the settled turn.
    - The deadline fires, in which case the test fails fast with
      a TimeoutError so a stuck background task doesn't hang CI.

    Tests that explicitly want to inspect the ack body should call
    POST directly. This helper is the convenience for "fire a turn
    and see what came back".
    """

    body = {"parts": [{"type": "text", "text": text}]}
    if json_override:
        body.update(json_override)
    ack = client.post(f"/v1/sessions/{sid}/messages", json=body)
    assert ack.status_code == 202, ack.text
    assert ack.json()["task_handle"].startswith("task_")
    body = ack.json()
    user_id = body["message_id"]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        # Newest-first ordering: the assistant we want is the
        # message immediately *before* the user message in the list
        # (i.e. one index lower).
        for i, m in enumerate(msgs):
            if m.get("id") == user_id:
                if (
                    i > 0
                    and msgs[i - 1]["role"] == "assistant"
                    and not msgs[i - 1].get("metadata", {}).get("live")
                ):
                    # #1334: finalize runs on the turn executor, so the assistant
                    # message can be visible a few ms before the session status and
                    # retry bookkeeping settle; "the turn completed" = the slot cleared.
                    settle_turn_slot(client, sid, timeout=max(1.0, deadline - time.monotonic()))
                    return msgs[i - 1]
                break
        time.sleep(poll_interval)

    raise TimeoutError(f"turn for user message {user_id!r} did not settle within {timeout:g}s")
