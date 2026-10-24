"""A parent asks for a strategy when it spawns a subagent (Phase 9, use 1).

``spawn_agent_task(..., strategy={variant, n, rubric, threshold})`` wraps the CHILD in a
BestOfN / Refine, validated by the shared variant parser before any child exists; the
strategy rides the child session's metadata and the builder applies it when it builds the
child's program.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agents import module_variants as mv
from tests.test_gact.task_records import active_turn_fixture, record_assignment_fixture
from tests.test_gact.test_module_variants_s5 import _build_variant_module


def _strategy_child(
    world: tuple[Any, TestClient, str], strategy: dict[str, Any]
) -> tuple[Any, str]:
    """Record a real child and its strategy before declaration-only assembly."""
    app, _client, parent_sid = world
    parent = app.state.sessions.get(parent_sid)
    child = app.state.sessions.create(
        workspace_id=parent.workspace_id,
        parent_session_id=parent_sid,
        metadata={"variant_strategy": strategy},
    )
    record_assignment_fixture(app, child.id, parent_sid=parent_sid)
    return app, child.id


def test_a_strategy_becomes_the_validated_module_variant() -> None:
    module = mv.strategy_module(
        {"variant": "refine", "n": 3, "rubric": "clear and short", "threshold": 0.8},
        agent_id="writer",
    )

    assert module["variant"] == "refine"
    assert module["n"] == 3
    assert module["reward"]["instructions"] == "clear and short"


@pytest.mark.parametrize(
    ("strategy", "match"),
    [
        ({"variant": "best_of_n", "n": 0, "rubric": "x"}, "n >= 1"),
        ({"variant": "vote", "n": 2, "rubric": "x"}, "unsupported module.variant"),
        ({"variant": "best_of_n", "n": 2}, "rubric"),
        ({"variant": "best_of_n", "n": 2, "rubric": "x", "judge": "crowd"}, "judge"),
    ],
)
def test_an_invalid_strategy_is_rejected_by_the_shared_parser(strategy: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        mv.strategy_module(strategy, agent_id="writer")


def test_a_user_judge_is_a_strategy_too_and_n_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_VARIANTS_MAX_N", "3")
    module = mv.strategy_module(
        {"variant": "best_of_n", "n": 9, "rubric": "x", "judge": "user"}, agent_id="writer"
    )

    assert module["judge"] == "user"
    assert (module["n"], module["n_requested"]) == (3, 9)


def test_the_child_is_built_wrapped_from_its_session_strategy(
    monkeypatch: pytest.MonkeyPatch,
    world: tuple[Any, TestClient, str],
) -> None:
    strategy = mv.strategy_module({"variant": "best_of_n", "n": 2, "rubric": "x"}, agent_id="kid")
    app, child_sid = _strategy_child(world, strategy)
    with active_turn_fixture(app, child_sid):
        module = _build_variant_module(monkeypatch, {"kind": "react"}, agent_id="kid")

    assert isinstance(module.program, dspy.BestOfN)
    assert module.program.N == 2


def test_a_user_judged_child_is_built_as_the_pause(
    monkeypatch: pytest.MonkeyPatch, world: tuple[Any, TestClient, str]
) -> None:
    from clio_agent.gact.agents.variant_drafts import UserJudgedVariant

    strategy = mv.strategy_module(
        {"variant": "refine", "n": 2, "rubric": "x", "judge": "user"}, agent_id="kid"
    )
    app, child_sid = _strategy_child(world, strategy)
    with active_turn_fixture(app, child_sid):
        module = _build_variant_module(monkeypatch, {"kind": "react"}, agent_id="kid")

    assert isinstance(module.program, UserJudgedVariant)
    assert (module.program.spec.variant, module.program.spec.n) == ("refine", 2)
