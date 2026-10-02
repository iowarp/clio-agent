"""One ``semantic.event`` projector serves both typed families on the v3 wire.

Found while combining the rebuild phases (2026-10-01): Phase 11b (compaction) and the
Phase 9 remainder (variant runs) each registered their own projector under the same
``semantic.event`` key; in the dict literal the later one silently replaced the other
and the compaction events would have reached clients as generic ``semantic.event``.
"""

from __future__ import annotations

from clio_agent.gact.events import Event
from clio_agent.gact.protocol.v3.event import event_to_v3


def _semantic(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return event_to_v3(
        Event(
            type="semantic.event",
            session_id="sess_t",
            payload={"event_type": event_type, "payload": payload},
        )
    )


def test_a_compaction_event_is_its_own_v3_event() -> None:
    frame = _semantic(
        "compaction.started",
        {"session_id": "sess_t", "compaction_id": "cmp_1", "scope": "main", "trigger": "auto"},
    )
    assert frame["type"] == "compaction.started"


def test_a_variant_try_is_its_own_v3_frame() -> None:
    frame = _semantic(
        "variant.try",
        {
            "variants_id": "var_1",
            "session_id": "sess_t",
            "agent_id": "main",
            "origin": "draft_alternatives",
            "strategy": "best_of_n",
            "judge": "user",
            "n": 2,
            "try_index": 0,
            "scope": "main#run0",
            "state": "running",
        },
    )
    assert frame["type"] == "variant.try.upserted"


def test_any_other_semantic_event_keeps_the_generic_envelope() -> None:
    frame = _semantic("react.step.completed", {"step": 1})
    assert frame["type"] == "semantic.event"
