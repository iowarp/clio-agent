from types import SimpleNamespace

import dspy

from clio_agent.agent import ClioAgent
from clio_agent.harness import RouteDecision, RunTrace


class _Registry:
    def __init__(self) -> None:
        self._parents = {
            "data": None,
            "ndp_catalog": "data",
        }

    def get_capabilities(self, expert_id: str):
        if expert_id not in self._parents:
            return None
        return SimpleNamespace(parent_id=self._parents[expert_id])


def test_native_child_metadata_records_sync_return_and_parent_resume() -> None:
    agent = object.__new__(ClioAgent)
    agent.registry = _Registry()
    trace = RunTrace(
        route=RouteDecision(
            target="data",
            source="dspy",
            reason="test route",
            confidence=0.7,
        )
    )
    result = dspy.Prediction(
        analysis="NDP staged waveform.",
        recommendations="Continue with SAC inspection.",
        metadata={
            "expert": "ndp_catalog",
            "parent_expert": "data",
            "resource": "waveform",
        },
    )

    agent._record_expert_handoff(
        trace,
        expert_id="data",
        dispatch_target="data",
        stage="planner_dispatch",
        input_summary="Find a bounded waveform",
        result=result,
        duration_ms=42.0,
    )

    rows = [row.to_dict() for row in trace.expert_handoffs]
    assert [row["stage"] for row in rows] == [
        "planner_dispatch",
        "planner_dispatch_child",
        "delegate.completed",
        "parent.resumed",
    ]
    child_return = rows[2]
    assert child_return["agent_id"] == "ndp_catalog"
    assert child_return["parent_id"] == "data"
    assert child_return["metadata"]["delegation_lifecycle"] == "sync"
    assert child_return["metadata"]["return_to"] == "data"
    assert child_return["metadata"]["return_payload"] == "compact_result"
    parent_resume = rows[3]
    assert parent_resume["agent_id"] == "data"
    assert parent_resume["metadata"]["resumed_from"] == "ndp_catalog"
    assert parent_resume["metadata"]["return_payload"] == "compact_result"
