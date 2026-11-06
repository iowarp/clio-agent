"""A session released while one of its writes is in flight keeps that write.

The write updates the session's in-memory index and puts it. A release racing it
dropped the in-memory state, and a reload could read the stored index before the
write's put landed: the reloaded index lacked the write's chunk, and its next put
overwrote the stored index, so those messages no longer read back.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest

from clio_agent.arc import lane_writer
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
