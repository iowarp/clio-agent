"""#880 — delegation-handoff invariant that SURVIVES the deleted settle/summary layer.

``tests/test_gact/test_delegation_contract_compaction.py`` was retired wholesale when
the server-authored ``return_summary.py`` layer was deleted, and the settle-loop resume
pins (``_dynamic_parent_resume_prompt`` / ``_latest_parent_resumed_output``) were retired
with the settle/synthesis engine itself (#948 S4). The one pin whose code path STILL
EXISTS is re-pinned here:

* :func:`_expert_handoff_fields` — the typed parent/child/stage/status extraction a
  client consumes instead of parsing a prose label.
"""

from __future__ import annotations

from clio_agent.gact.app import _expert_handoff_fields
from clio_agent.gact.types import Part


def test_expert_handoff_part_carries_structured_fields_from_row() -> None:
    """An ``expert_handoff`` Part exposes the delegation as typed fields (parent/child/
    stage/status) drawn from the structured row, so a client never parses the prose
    ``text`` label to attribute the handoff."""

    row = {
        "agent_id": "geospatial",  # the child that received the delegation
        "parent_id": "main",  # the parent that made it
        "stage": "delegate.completed",
        "status": "completed",
        "output": "staged waveform",  # the child's answer rides ``output`` verbatim
    }
    fields = _expert_handoff_fields(row)
    assert fields == {
        "parent_agent": "main",
        "child_agent": "geospatial",
        "stage": "delegate.completed",
        "status": "completed",
    }

    part = Part(
        type="expert_handoff",
        agent_id=fields["parent_agent"],
        parent_agent=fields["parent_agent"],
        child_agent=fields["child_agent"],
        stage=fields["stage"],
        status=fields["status"],
        text="",  # the UI consumes the structured fields, not the string
    )
    # The handoff is fully described without the prose ``text``.
    assert part.text == ""
    assert part.parent_agent == "main"
    assert part.child_agent == "geospatial"
    assert part.stage == "delegate.completed"
    assert part.status == "completed"
    # The generating party is the parent.
    assert part.agent_id == "main"
