"""The loop's cached context read (Phase 11a) on real clio-core.

``StepRecorder.read_steps`` keeps the messages of every closed step per view generation
and re-folds only the open tail step. Whatever the plane does between reads (appends of
a step's segments, steers, a compaction, a delete, a restart), the cached read must
equal a fresh ``fold_steps`` of the live plane. Rehydrated media are cached by the
descriptor's sha256 per file and re-hydrated (integrity check included) once the file
changes.

Sabotage: making ``ContextReader.read`` ignore the generation (keep the closed
messages across an op) turns ``test_the_cached_read_equals_a_fresh_fold`` red at the
compaction; dropping the stat from the media key turns
``test_media_are_reused_until_the_file_changes`` red.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import make_arc_store
from clio_agent.gact.agents.clio_react_record import StepRecorder, fold_steps, result_part
from clio_agent.gact.agents.media_cache import clear_media_cache

SCOPE = "agentA"


@pytest.fixture
def arc():
    memory = ARCMemory(store=make_arc_store(backend="cte", namespace=f"rd-{uuid.uuid4().hex[:8]}"))
    yield memory
    memory._store.clear()


def _step(arc: ARCMemory, session: str, i: int) -> None:
    arc.append_segment(session, SCOPE, "thought", {"text": f"T{i}", "thinking": []}, step=i)
    call = {"id": f"c{i}", "name": "search", "args": {"q": str(i)}}
    arc.append_segment(session, SCOPE, "tool_call", call, step=i)
    arc.append_segment(
        session, SCOPE, "observation", {"call_id": f"c{i}", "text": f"O{i}", "is_error": False}
    )


def test_the_cached_read_equals_a_fresh_fold(arc: ARCMemory) -> None:
    session = "rd_" + uuid.uuid4().hex[:10]
    recorder = StepRecorder(arc, session, SCOPE, expert_id=SCOPE)
    arc.append_segment(session, SCOPE, "user", {"text": "question"})

    def fresh() -> list[Any]:
        return fold_steps(arc.render_segments(session, SCOPE))

    reads = []
    for i in range(6):
        _step(arc, session, i)
        reads.append(recorder.read_steps())
        assert reads[-1] == fresh()
        if i == 2:
            arc.append_segment(session, SCOPE, "user", {"text": "steer", "source": "steer"})
            assert recorder.read_steps() == fresh()
    for before, after in zip(reads, reads[1:], strict=False):
        assert after[: len(before)] == before  # append-only: the prefix holds

    # An op that keeps the segment count (a replace) must not reuse the closed steps.
    early = next(s for s in arc.render_segments(session, SCOPE) if s.kind == "thought")
    arc.replace_segment(session, SCOPE, early.id, {"text": "EDITED", "thinking": []})
    assert recorder.read_steps() == fresh()

    live = arc.render_working_set(session, SCOPE)
    arc.summarize_segments(session, SCOPE, [s.id for s in live], {"text": "SUMMARY"})
    for i in range(20, 30):  # more segments than before the compaction, then read
        _step(arc, session, i)
    assert recorder.read_steps() == fresh()
    _step(arc, session, 9)
    assert recorder.read_steps() == fresh()
    first = arc.render_segments(session, SCOPE)[1]
    arc.delete_segments(session, SCOPE, [first.id])
    assert recorder.read_steps() == fresh()

    arc._segments.release(session)  # a restart of the plane: views rebuilt cold
    _step(arc, session, 10)
    assert recorder.read_steps() == fresh()


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZQAAAABJRU5ErkJggg=="
)


@pytest.mark.usefixtures("clio_core_plane")
def test_media_are_reused_until_the_file_changes(tmp_path: Path) -> None:
    from clio_agent.gact.view_image_tool import build_view_image_tool
    from clio_agent.tools.execution import tool_workspace_context

    clear_media_cache()
    (tmp_path / "plot.png").write_bytes(_PNG)
    with tool_workspace_context(tmp_path):
        descriptor = build_view_image_tool()(path="plot.png")
        first = result_part("c1", "view_image", descriptor, False)
        second = result_part("c1", "view_image", descriptor, False)
        assert first.content[0] is second.content[0]  # reused, not re-hydrated
        assert base64.b64decode(first.content[0].data) == _PNG
        snapshot = tmp_path / descriptor["snapshot"]
        snapshot.write_bytes(_PNG + b"tampered")  # changed: hydrated and checked again
        third = result_part("c1", "view_image", descriptor, False)
    [note] = third.content
    assert "media_unavailable" in note.text
