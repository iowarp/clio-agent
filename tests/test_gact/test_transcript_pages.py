"""R4: a conversation's page is read from its message lane by range.

* the newest page and every older page (``before``) together are the whole
  transcript, in order, with no message twice;
* a cold page read leaves no lane chunk loaded in the segment store;
* an unknown cursor is refused; system messages can be left out;
* a resident ledger is paged as is.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent import conf
from clio_agent.arc.lane_chunking import chunk_scope, lane_scopes
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE, mint_message_part_atoms
from clio_agent.gact.transcript_pages import PageCursorNotFound, page_of, read_lane_page
from clio_agent.gact.transcript_projection import assemble_session_messages
from clio_agent.gact.types import Message, Part

SID = "sess-pages"


@pytest.fixture()
def arc(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> ARCMemory:
    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=tmp_path, cwd=tmp_path))
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "5")
    memory = ARCMemory(data_dir=str(tmp_path / "arc"))
    for i in range(23):
        role = "system" if i % 7 == 3 else ("user" if i % 2 == 0 else "assistant")
        mint_message_part_atoms(memory, SID, _message(i, role))
    return memory


def _message(i: int, role: str) -> Message:
    return Message(
        id=f"m{i:02d}",
        turn_id=f"turn_{i:02d}",
        session_id=SID,
        role=role,
        created_at=f"2026-07-12T10:00:{i:02d}+00:00",
        updated_at=f"2026-07-12T10:00:{i:02d}+00:00",
        parts=[Part(id=f"m{i:02d}_p{k}", type="text", text=f"{i}.{k}") for k in range(2)],
    )


def test_pages_cover_the_transcript_once_in_order(arc: ARCMemory) -> None:
    assert len(lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE)) > 3  # several chunks
    expected = [m.id for m in assemble_session_messages(arc, SID)]
    collected: list[str] = []
    before = None
    while True:
        page = read_lane_page(arc, SID, limit=4, before=before, include_system=True)
        assert len(page.messages) <= 4
        collected = [m.id for m in page.messages] + collected
        if not page.has_more:
            break
        before = page.messages[0].id
    assert collected == expected


def test_newest_page_is_the_tail(arc: ARCMemory) -> None:
    page = read_lane_page(arc, SID, limit=3, before=None, include_system=True)
    assert [m.id for m in page.messages] == ["m20", "m21", "m22"]
    assert page.has_more and page.source == "lane"
    assert [p.text for p in page.messages[-1].parts] == ["22.0", "22.1"]


def test_cold_read_installs_no_lane_chunk(arc: ARCMemory) -> None:
    arc._segments.release(SID)
    read_lane_page(arc, SID, limit=3, before="m10", include_system=True)
    chunks = {chunk_scope(MESSAGE_PART_SCOPE, i) for i in range(1, 20)}
    assert not {key for key in arc._segments._loaded if key[0] == SID and key[1] in chunks}


def test_unknown_cursor_and_system_filter(arc: ARCMemory) -> None:
    with pytest.raises(PageCursorNotFound):
        read_lane_page(arc, SID, limit=3, before="nope", include_system=True)
    page = read_lane_page(arc, SID, limit=6, before="m12", include_system=False)
    assert [m.id for m in page.messages] == ["m05", "m06", "m07", "m08", "m09", "m11"]


def test_resident_ledger_is_paged_as_is() -> None:
    rows = [_message(i, "user") for i in range(5)]
    page = page_of(rows, limit=2, before="m03", include_system=True)
    assert [m.id for m in page.messages] == ["m01", "m02"]
    assert page.has_more and page.source == "resident"
    with pytest.raises(PageCursorNotFound):
        page_of(rows, limit=2, before="zz", include_system=True)
