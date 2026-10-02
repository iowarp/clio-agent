"""A context op on an id that is not live is a typed failure, never a silent no-op.

Delete, summarize and replace on the production fold store skipped any id that was not
live (no log), and ``POST /context/ops`` answered ``applied: true, tombstoned_count: 0``:
a human's or an algorithm's edit could silently not happen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.segment_ids import StaleSegmentIdError

SESSION, SCOPE = "s1", "agentA"


@pytest.fixture
def arc(tmp_path: Path) -> ARCMemory:
    return ARCMemory(data_dir=str(tmp_path / "arc"))


def _two(arc: ARCMemory) -> tuple[str, str]:
    a = arc.append_segment(SESSION, SCOPE, "user", {"text": "q"}, step=0)
    b = arc.append_segment(SESSION, SCOPE, "thought", {"text": "t"}, step=0)
    return a.id, b.id


def test_deleting_a_missing_id_fails_typed_and_deletes_nothing(arc: ARCMemory) -> None:
    a, _b = _two(arc)

    with pytest.raises(StaleSegmentIdError) as err:
        arc.delete_segments(SESSION, SCOPE, [a, "seg_gone"])

    assert err.value.error_type == "stale_segment_id"
    assert err.value.details["missing"] == ["seg_gone"]
    assert a in {s.id for s in arc.render_working_set(SESSION, SCOPE)}


def test_deleting_an_already_deleted_id_fails_typed(arc: ARCMemory) -> None:
    a, _b = _two(arc)
    assert arc.delete_segments(SESSION, SCOPE, [a]) == 1

    with pytest.raises(StaleSegmentIdError):
        arc.delete_segments(SESSION, SCOPE, [a])


def test_summarizing_a_missing_id_fails_typed_and_appends_nothing(arc: ARCMemory) -> None:
    a, b = _two(arc)

    with pytest.raises(StaleSegmentIdError):
        arc.summarize_segments(SESSION, SCOPE, [a, "seg_gone"], {"text": "S"})

    assert [s.id for s in arc.render_working_set(SESSION, SCOPE)] == [a, b]


def test_replacing_a_missing_id_fails_typed(arc: ARCMemory) -> None:
    _two(arc)

    with pytest.raises(StaleSegmentIdError):
        arc.replace_segment(SESSION, SCOPE, "seg_gone", {"text": "x"})


def test_live_ids_still_apply(arc: ARCMemory) -> None:
    a, b = _two(arc)

    assert arc.delete_segments(SESSION, SCOPE, [a]) == 1
    assert [s.id for s in arc.render_working_set(SESSION, SCOPE)] == [b]


def test_the_ops_route_answers_409_for_a_stale_id(tmp_path: Path, arc: ARCMemory) -> None:
    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "s.json", arc=arc)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        arc.append_segment(sid, SCOPE, "user", {"text": "q"}, step=0)

        response = client.post(
            f"/v1/sessions/{sid}/context/ops",
            json={"op": "delete", "scope": SCOPE, "ids": ["seg_gone"]},
        )

    assert response.status_code == 409, response.json()
    body = response.json()
    assert (body.get("detail") or body)["error"]["error"] == "stale_segment_id"
