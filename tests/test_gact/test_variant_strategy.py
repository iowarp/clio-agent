"""A parent asks for a strategy when it spawns a subagent (Phase 9, use 1).

``spawn_agent_task(..., strategy={variant, n, rubric, threshold})`` wraps the CHILD in the
same BestOfN / Refine the blueprint declaration uses, validated by the same parser before
any child exists; the strategy rides the child session's metadata and the builder applies
it when it builds the child's program.
"""

from __future__ import annotations

import types
from typing import Any

import dspy
import pytest

from clio_agent.gact import context as ctx
from clio_agent.gact.agents import module_variants as mv
from clio_agent.gact.agents.module_variants import spawn_scope_with_strategy
from clio_agent.gact.turn_spawn import SpawnError
from tests.test_gact.test_module_variants_s5 import _build_variant_module


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
def test_an_invalid_strategy_is_refused_before_any_child(strategy: Any, match: str) -> None:
    with pytest.raises(SpawnError, match=match) as err:
        spawn_scope_with_strategy({"workspace": "w"}, strategy, agent_id="writer")
    assert err.value.reason == "invalid_strategy"


def test_a_user_judge_is_a_strategy_too_and_n_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_VARIANTS_MAX_N", "3")
    module = mv.strategy_module(
        {"variant": "best_of_n", "n": 9, "rubric": "x", "judge": "user"}, agent_id="writer"
    )

    assert module["judge"] == "user"
    assert (module["n"], module["n_requested"]) == (3, 9)


def test_the_strategy_rides_the_child_session_metadata() -> None:
    scope = spawn_scope_with_strategy(
        {"workspace": "w"}, {"variant": "best_of_n", "n": 2, "rubric": "x"}, agent_id="writer"
    )

    assert scope["workspace"] == "w"
    assert scope["variant_strategy"]["variant"] == "best_of_n"


def test_no_strategy_leaves_the_scope_alone() -> None:
    assert spawn_scope_with_strategy({"workspace": "w"}, None, agent_id="writer") == {
        "workspace": "w"
    }


def test_the_child_is_built_wrapped_from_its_session_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = mv.strategy_module({"variant": "best_of_n", "n": 2, "rubric": "x"}, agent_id="kid")
    row = types.SimpleNamespace(metadata={"variant_strategy": strategy})
    app = types.SimpleNamespace(state=types.SimpleNamespace(sessions={"child": row}))
    tokens = [ctx.set_app(app), ctx.set_session_id("child")]
    try:
        module = _build_variant_module(monkeypatch, {"kind": "react"}, agent_id="kid")
    finally:
        for token in reversed(tokens):
            ctx.reset(token)

    assert isinstance(module.program, dspy.BestOfN)
    assert module.program.N == 2


def test_a_user_judged_child_is_built_as_the_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.agents.variant_drafts import UserJudgedVariant

    strategy = mv.strategy_module(
        {"variant": "refine", "n": 2, "rubric": "x", "judge": "user"}, agent_id="kid"
    )
    row = types.SimpleNamespace(metadata={"variant_strategy": strategy})
    app = types.SimpleNamespace(state=types.SimpleNamespace(sessions={"child": row}))
    tokens = [ctx.set_app(app), ctx.set_session_id("child")]
    try:
        module = _build_variant_module(monkeypatch, {"kind": "react"}, agent_id="kid")
    finally:
        for token in reversed(tokens):
            ctx.reset(token)

    assert isinstance(module.program, UserJudgedVariant)
    assert (module.program.spec.variant, module.program.spec.n) == ("refine", 2)
