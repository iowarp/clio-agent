"""A session released while one of its writes is in flight keeps that write.

The write updates the session's in-memory index and puts it. A release racing it
dropped the in-memory state, and a reload could read the stored index before the
write's put landed: the reloaded index lacked the write's chunk, and its next put
overwrote the stored index, so those messages no longer read back. A reload could also
build the scope's view before the write's lane chunk landed, while the writer folded
its atom into the dropped view: the live store never rendered it.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest

from clio_agent.arc import lane_writer
from clio_agent.arc.lane_index import is_ws_content_scope
from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory

SCOPE = "agentA"


def _texts(arc: ARCMemory, session: str) -> list[str]:
    return [s.content["text"] for s in arc.render_segments(session, SCOPE)]


def test_a_release_racing_a_write_never_loses_its_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capacity 1: every append opens a chunk and puts the index. While the second
    append is about to put the index, another thread releases and reloads the session.

    SABOTAGE: drop the retire-under-lock in ``_forget`` (pop the state directly) and the
    reload reads the index without the second chunk; the third append's index put then
    erases it and a fresh read misses "a1"."""
    monkeypatch.setattr(lane_writer, "_chunk_capacity", lambda: 1)
    session = "race_" + uuid.uuid4().hex[:12]
    store = _MemoryStore()
    arc = ARCMemory(store=store, working_set_fold=True)
    segs: Any = arc._segments
    arc.append_segment(session, SCOPE, "thought", {"text": "a0"}, step=0)

    put_index = segs._put_index
    racer: list[threading.Thread] = []

    def racing_put(session_id: str, index: Any) -> None:
        if not racer:

            def release_and_reload() -> None:
                segs.release(session_id)
                segs._session(session_id)

            thread = threading.Thread(target=release_and_reload, daemon=True)
            racer.append(thread)
            thread.start()
            time.sleep(0.3)  # the racer reloads now unless the release waits for this write
        put_index(session_id, index)

    monkeypatch.setattr(segs, "_put_index", racing_put)
    arc.append_segment(session, SCOPE, "thought", {"text": "a1"}, step=1)
    racer[0].join(timeout=10)
    assert not racer[0].is_alive()
    monkeypatch.setattr(segs, "_put_index", put_index)
    arc.append_segment(session, SCOPE, "thought", {"text": "a2"}, step=2)

    assert _texts(arc, session) == ["a0", "a1", "a2"]
    reloaded = ARCMemory(store=store, working_set_fold=True)
    assert _texts(reloaded, session) == ["a0", "a1", "a2"]


def test_a_release_racing_a_write_never_loses_it_from_the_live_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the index put and before the writer takes the new lane chunk's lock,
    another thread releases the session and renders the scope, building its view in a
    new state. The release must wait for the write, so that view (the live one) holds
    the write's atom.

    SABOTAGE: make ``_write_view`` skip ``pinned_session`` (the view and lock only) and
    the racer builds its view from the chunk before "a1" is put; the writer folds "a1"
    into the dropped view, and the live render misses it."""
    monkeypatch.setattr(lane_writer, "_chunk_capacity", lambda: 1)
    session = "race_" + uuid.uuid4().hex[:12]
    store = _MemoryStore()
    arc = ARCMemory(store=store, working_set_fold=True)
    segs: Any = arc._segments
    arc.append_segment(session, SCOPE, "thought", {"text": "a0"}, step=0)

    writer = threading.current_thread()
    put_index, lock_for = segs._put_index, segs._lock_for
    indexed: list[str] = []
    racer: list[threading.Thread] = []
    seen: list[list[str]] = []

    def noting_put(session_id: str, index: Any) -> None:
        put_index(session_id, index)
        indexed.append(session_id)

    def racing_lock(session_id: str, scope: str) -> Any:
        if threading.current_thread() is writer and indexed and is_ws_content_scope(scope):
            if not racer:

                def release_and_render() -> None:
                    segs.release(session_id)
                    seen.append(_texts(arc, session_id))

                thread = threading.Thread(target=release_and_render, daemon=True)
                racer.append(thread)
                thread.start()
                time.sleep(0.3)  # the racer builds the view now unless the release waits
        return lock_for(session_id, scope)

    monkeypatch.setattr(segs, "_put_index", noting_put)
    monkeypatch.setattr(segs, "_lock_for", racing_lock)
    arc.append_segment(session, SCOPE, "thought", {"text": "a1"}, step=1)
    assert racer, "the write put the index before its chunk"
    racer[0].join(timeout=10)
    assert not racer[0].is_alive()

    assert seen == [["a0", "a1"]]
    assert _texts(arc, session) == ["a0", "a1"]
    assert _texts(ARCMemory(store=store, working_set_fold=True), session) == ["a0", "a1"]
