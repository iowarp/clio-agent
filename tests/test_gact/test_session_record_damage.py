"""A session whose stored record reads back damaged is reported, not "not found" (FN-14).

Seen live after a torn clio-core metadata record: the index listed the conversation, its
record read back as ``1`` (another blob's bytes on a block clio-core handed out twice),
every route answered ``404 session not found`` and the boot sweep removed its folder.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact import session_record_damage as damage
from clio_agent.gact.message_submission_errors import session_not_found
from clio_agent.gact.session_records import ROW_UNREADABLE_REASON
from clio_agent.gact.sessions import SessionStore
from clio_agent.runtime.clio_core_metadata_restore_health import (
    probe_clio_core_metadata_restore,
)
from clio_agent.runtime.status import IntegrationState


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    damage.reset_for_tests()
    yield
    damage.reset_for_tests()


def test_a_damaged_record_answers_a_typed_503_and_shows_in_health() -> None:
    store = SessionStore()
    sid = store.create(workspace_id="ws_default", title="kept").id
    assert session_not_found(sid).status_code == 404  # readable: an ordinary miss stays 404

    # The record now reads back as another blob's bytes (the index still lists it).
    store._records._memory.data["row/" + sid] = b"1"
    store._forget(sid)

    assert store.get(sid) is None
    assert sid in store.index.ids()
    refusal = session_not_found(sid)
    assert refusal.status_code == 503
    assert refusal.detail["error"]["error"] == ROW_UNREADABLE_REASON
    assert session_not_found("sess_never_existed").status_code == 404
    (row,) = probe_clio_core_metadata_restore(outcomes=[], failures=[])
    assert row.state == IntegrationState.DEGRADED
    assert sid in row.details["sessions_unreadable"]
