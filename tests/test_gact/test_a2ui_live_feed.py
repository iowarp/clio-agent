"""A surface fed by a background script is state, not transcript (taskable run_code CT10).

The REAL store and producer path (``_common.apply_messages``, as ``update_a2ui_data_model``
calls it) on a real app, with and without a background task in the context.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from clio_agent.gact import a2ui_live_feed
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.app import build_app
from clio_agent.gact.code_task_context import _BACKGROUND_TASK, BackgroundTask
from clio_agent.gact.messages import MessageStore

CATALOG = workspace_catalog_id()


def _world(tmp_path: Path) -> tuple[Any, str]:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.message_store = MessageStore(path=None)
    sid = app.state.sessions.create(workspace_id="ws_default", title="feed").id
    app.state.a2ui_store.apply_batch(
        sid,
        [
            {"version": "v0.9.1", "createSurface": {"surfaceId": "s1", "catalogId": CATALOG}},
            {
                "version": "v0.9.1",
                "updateComponents": {
                    "surfaceId": "s1",
                    "components": [
                        {"id": "root", "component": "Text", "text": {"path": "/current"}}
                    ],
                },
            },
        ],
    )
    return app, sid


def _update(app: Any, sid: str, value: Any) -> Any:
    return _common.apply_messages(
        app,
        sid,
        [
            {
                "version": "v0.9.1",
                "updateDataModel": {"surfaceId": "s1", "path": "/", "value": value},
            }
        ],
        catalog_id=CATALOG,
    )


def _a2ui_parts(app: Any, sid: str) -> int:
    """A2UI parts in the transcript: persisted, or held for the turn's message."""
    persisted = sum(
        1 for m in app.state.a2ui_store._messages(sid) for p in (m.parts or []) if p.type == "a2ui"
    )
    live = (getattr(app.state, "live_assistant_parts", {}) or {}).get(sid, [])
    return persisted + sum(1 for p in live if getattr(p, "type", "") == "a2ui")


def _model(app: Any, sid: str) -> Any:
    record = app.state.a2ui_store.get(sid, "s1")
    updates = [m["updateDataModel"] for m in record.messages if "updateDataModel" in m]
    return updates[-1]["value"] if updates else None, record.revision, len(record.messages)


def test_a_background_feed_updates_in_place_and_checkpoints_once(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Thirty updates from a background task leave no transcript part until the
    checkpoint, which is one part; viewers got the latest value at once and transient
    upserts kept the replay log clear; the revision never went backwards.

    SABOTAGE: route background updates through the turn writer again -> thirty parts
    -> red."""
    monkeypatch.setenv("CLIO_A2UI_LIVE_CHECKPOINT_S", "3600")
    app, sid = _world(tmp_path)
    before = _a2ui_parts(app, sid)
    history_before = len(app.state.bus.session_events_since(sid))
    token = _BACKGROUND_TASK.set(BackgroundTask("task_feed", threading.Event()))
    try:
        for i in range(30):
            _update(app, sid, {"current": i})
    finally:
        _BACKGROUND_TASK.reset(token)
    value, revision, slots = _model(app, sid)
    assert value == {"current": 29}
    assert slots == 3  # createSurface, the components, one current model
    assert _a2ui_parts(app, sid) == before  # nothing written to the transcript yet
    assert len(app.state.bus.session_events_since(sid)) == history_before  # transient

    a2ui_live_feed.flush_task(app, "task_feed")
    assert _a2ui_parts(app, sid) == before + 1  # one checkpoint
    value, after, _slots = _model(app, sid)
    assert value == {"current": 29} and after >= revision
    assert not (app.state.a2ui_live_feeds.get(sid) or {})


def test_a_replay_from_checkpoints_alone_keeps_the_model_and_revision(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """After the feed is checkpointed and the cache dropped, a cold replay of the creation
    part and the checkpoint gives the latest model at no lower revision (the floor)."""
    monkeypatch.setenv("CLIO_A2UI_LIVE_CHECKPOINT_S", "3600")
    app, sid = _world(tmp_path)
    token = _BACKGROUND_TASK.set(BackgroundTask("task_feed", threading.Event()))
    try:
        for i in range(12):
            _update(app, sid, {"current": i})
    finally:
        _BACKGROUND_TASK.reset(token)
    _value, live_revision, _slots = _model(app, sid)
    a2ui_live_feed.flush_task(app, "task_feed")
    app.state.a2ui_store._projection_cache.clear()  # a restart: nothing but the transcript
    value, replayed, slots = _model(app, sid)
    assert value == {"current": 11} and replayed >= live_revision and slots == 3


def test_an_update_in_the_agents_own_turn_is_still_a_transcript_part(tmp_path: Path) -> None:
    app, sid = _world(tmp_path)
    before = _a2ui_parts(app, sid)
    _update(app, sid, {"current": "turn"})
    assert _a2ui_parts(app, sid) == before + 1
