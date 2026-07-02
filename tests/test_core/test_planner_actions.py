"""Planner action vocabulary tests (issue #768 follow-up).

The tier-2 expert arm was deleted, so the loop executes only ``tool``,
``answer``, and ``none`` actions. These tests pin the residue removal:

- ``_parse_action_json`` rejects ``expert`` at the parse layer (the
  sanctioned format-only enum barrier).
- A planner that emits an expert action gets a structured
  ``planner_error`` observation and is re-asked — the model stays the
  decider; CLIO neither crashes nor silently drops the step.
- A planner that never recovers exhausts the step budget with a
  structured routing error instead of fabricating a route.
- The planner signature docstring advertises exactly the action kinds
  the loop can execute (no deleted vocabulary).
"""

from __future__ import annotations

import json
import re
from typing import Any

import dspy
import pytest

from clio_agent.agent import (
    SUPPORTED_PLANNER_ACTION_KINDS,
    ClioAgent,
    UnsupportedPlannerActionError,
)
from clio_agent.errors import RoutingError
from clio_agent.harness import RouteDecision, RunTrace
from clio_agent.signatures.main_agent_sig import AgentActionSignature

_EXPERT_ACTION = {
    "action": "expert",
    "expert": "data",
    "question": "",
    "reason": "delegate to the data expert",
}


def _recording_planner(actions: list[dict[str, Any]], calls: list[dict[str, Any]]):
    """Return an action_planner stub that scripts actions and records kwargs."""
    queue = list(actions)

    def planner(**kwargs: Any) -> dspy.Prediction:
        calls.append(dict(kwargs))
        return dspy.Prediction(action_json=json.dumps(queue.pop(0)))

    return planner


def _trace() -> RunTrace:
    return RunTrace(
        route=RouteDecision(
            target="chat",
            source="dspy",
            reason="test route",
            confidence=0.5,
        )
    )


@pytest.fixture
def agent(tmp_path):
    instance = ClioAgent(data_dir=str(tmp_path / "agent"))
    yield instance
    instance.shutdown()


class TestParseActionEnum:
    """The parse layer accepts only executable action kinds."""

    def test_supported_kinds_have_no_expert(self) -> None:
        assert SUPPORTED_PLANNER_ACTION_KINDS == frozenset({"tool", "answer", "none"})

    def test_expert_action_rejected_with_decoded_payload(self) -> None:
        with pytest.raises(UnsupportedPlannerActionError) as excinfo:
            ClioAgent._parse_action_json(json.dumps(_EXPERT_ACTION))
        assert excinfo.value.kind == "expert"
        assert excinfo.value.action["expert"] == "data"
        message = str(excinfo.value)
        assert "unsupported action" in message
        assert "answer, none, tool" in message

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(UnsupportedPlannerActionError):
            ClioAgent._parse_action_json('{"action":"route","reason":"x"}')

    @pytest.mark.parametrize("kind", sorted(SUPPORTED_PLANNER_ACTION_KINDS))
    def test_supported_kinds_still_parse(self, kind: str) -> None:
        decoded = ClioAgent._parse_action_json(json.dumps({"action": kind.upper(), "reason": "r"}))
        assert decoded["action"] == kind


class TestExpertActionReask:
    """An emitted expert action becomes a planner_error observation re-ask."""

    def test_expert_action_gets_planner_error_observation_then_reask(self, agent) -> None:
        final = "The staged station table lists 12 coastal GNSS stations."
        calls: list[dict[str, Any]] = []
        agent.action_planner = _recording_planner(
            [_EXPERT_ACTION, {"action": "answer", "answer": final}],
            calls,
        )

        selected, answer, _expert_result, error_info, _route = agent._run_agent_loop(
            question="Which stations are staged?",
            session_context="",
            file_context="",
            trace=_trace(),
        )

        assert answer == final
        assert error_info is None
        assert selected == "chat"
        # The re-ask happened: two planner calls, the second seeing the
        # structured planner_error observation for the rejected action.
        assert len(calls) == 2
        reask_observations = calls[1]["observations"]
        assert "planner_error" in reask_observations
        assert "unsupported action" in reask_observations
        assert "'expert'" in reask_observations
        assert "answer, none, tool" in reask_observations

    def test_persistent_expert_actions_exhaust_steps_with_structured_error(self, agent) -> None:
        calls: list[dict[str, Any]] = []

        def always_expert(**kwargs: Any) -> dspy.Prediction:
            calls.append(dict(kwargs))
            return dspy.Prediction(action_json=json.dumps(_EXPERT_ACTION))

        agent.action_planner = always_expert

        with pytest.raises(RoutingError) as excinfo:
            agent._run_agent_loop(
                question="Which stations are staged?",
                session_context="",
                file_context="",
                trace=_trace(),
            )

        # Bounded: one re-ask per step, then a structured routing error.
        assert len(calls) == agent._agent_max_steps()
        details = excinfo.value.details
        assert details["step_limit"] == agent._agent_max_steps()
        assert details["planner_observations"]
        assert all(obs["type"] == "planner_error" for obs in details["planner_observations"])


class TestSignatureVocabulary:
    """The planner prompt advertises exactly the executable action kinds."""

    def _instructions(self) -> str:
        return str(
            getattr(AgentActionSignature, "instructions", None) or AgentActionSignature.__doc__
        )

    def test_advertised_action_forms_match_supported_kinds(self) -> None:
        advertised = set(re.findall(r'"action"\s*:\s*"(\w+)"', self._instructions()))
        assert advertised == set(SUPPORTED_PLANNER_ACTION_KINDS)

    def test_no_expert_delegation_vocabulary(self) -> None:
        text = self._instructions().lower()
        assert "expert delegation" not in text
        assert "delegate" not in text
        assert "recommended_parent_actions" not in text
