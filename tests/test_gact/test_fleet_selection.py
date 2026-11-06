"""Model selection against the fleet (docs/design/sdk/model-fleet.md, "Design: selection")."""

from __future__ import annotations

import pytest

from clio_agent.gact.fleet.entries import FleetEntry
from clio_agent.gact.fleet.selection import (
    ModelSelectionRefused,
    choose_model,
    resolve_reference,
)
from clio_agent.sdk.model import Requires

HAIKU = FleetEntry(
    "claude_code",
    "claude-haiku-5-5",
    aliases=("haiku",),
    tool_calling=True,
    vision=True,
    context_tokens=200_000,
    output_tokens=64_000,
    available=True,
)
OPUS = FleetEntry(
    "claude_code",
    "claude-opus-5-5",
    aliases=("opus",),
    tool_calling=True,
    vision=True,
    context_tokens=1_000_000,
    output_tokens=128_000,
    available=True,
)
TEXT_ONLY = FleetEntry(
    "vllm", "qwen-text", tool_calling=True, vision=False, context_tokens=None, available=True
)
DOWN = FleetEntry(
    "openrouter", "x/model", available=False, unavailable_reason="account_unavailable"
)
FLEET = [HAIKU, OPUS, TEXT_ONLY, DOWN]
PARENT = {"provider_id": "claude_code", "model_id": "claude-opus-5-5"}


def _choose(**kwargs):
    return choose_model(
        FLEET,
        explicit=kwargs.pop("explicit", ""),
        prefers_chain=kwargs.pop("prefers_chain", []),
        requires=kwargs.pop("requires", Requires()),
        attribution=kwargs.pop("attribution", {}),
        inherited=kwargs.pop("inherited", PARENT),
    )


def test_a_reference_is_exact_a_bare_id_or_an_alias() -> None:
    assert resolve_reference(FLEET, "claude_code/claude-haiku-5-5", caller_account="") is HAIKU
    assert resolve_reference(FLEET, "claude-haiku-5-5", caller_account="") is HAIKU
    assert resolve_reference(FLEET, "haiku", caller_account="claude_code") is HAIKU
    assert resolve_reference(FLEET, "x/model", caller_account="") is DOWN
    with pytest.raises(ModelSelectionRefused) as refused:
        resolve_reference(FLEET, "gpt-9", caller_account="")
    assert refused.value.code == "model_unknown"
    assert "claude_code/claude-haiku-5-5" in str(refused.value)


def test_an_explicit_model_runs_and_says_so() -> None:
    choice = _choose(explicit="haiku")
    assert choice.ran == {"provider_id": "claude_code", "model_id": "claude-haiku-5-5"}
    assert (choice.source, choice.asked) == ("explicit", "haiku")


def test_an_explicit_model_without_a_required_capability_is_refused_with_alternatives() -> None:
    """M5: a spawn names a model without vision for a target whose plugins need vision."""
    with pytest.raises(ModelSelectionRefused) as refused:
        _choose(
            explicit="vllm/qwen-text",
            requires=Requires(vision=True),
            attribution={"vision": ["pdf"]},
        )
    assert refused.value.code == "model_requirement_unmet"
    message = str(refused.value)
    assert "image input (required by pdf)" in message
    assert "claude_code/claude-haiku-5-5" in message and "claude_code/claude-opus-5-5" in message
    assert refused.value.details["fitting"] == [
        "claude_code/claude-haiku-5-5",
        "claude_code/claude-opus-5-5",
    ]


def test_an_explicit_model_on_an_unhealthy_account_is_refused() -> None:
    with pytest.raises(ModelSelectionRefused) as refused:
        _choose(explicit="openrouter/x/model")
    assert refused.value.code == "model_unavailable"


def test_a_context_requirement_the_fleet_cannot_verify_is_recorded_not_refused() -> None:
    choice = _choose(explicit="vllm/qwen-text", requires=Requires(context_tokens=100_000))
    assert choice.ran["model_id"] == "qwen-text"
    assert any(
        reason.startswith("unverified: context of 100,000 tokens") for reason in choice.reasons
    )


def test_a_preference_is_used_and_names_its_source() -> None:
    choice = _choose(
        prefers_chain=[{"source": "agent:reviewer", "model": "haiku", "effort": "low"}]
    )
    assert choice.ran["model_id"] == "claude-haiku-5-5"
    assert (choice.source, choice.effort) == ("prefers", "low")
    assert "from agent:reviewer" in choice.reasons


def test_an_effort_only_preference_applies_to_the_model_that_runs() -> None:
    choice = _choose(
        explicit="haiku",
        prefers_chain=[{"source": "agent:reviewer", "effort": "high"}],
    )
    assert (choice.source, choice.effort) == ("explicit", "high")
    assert "effort high from agent:reviewer" in choice.reasons
    inherited = _choose(prefers_chain=[{"source": "pdf", "effort": "low"}])
    assert (inherited.source, inherited.effort) == ("inherited", "low")


def test_an_unmet_preference_steps_down_the_order_and_records_why() -> None:
    """M4: a preference for a model that is not connected; the child runs on the parent's."""
    choice = _choose(
        prefers_chain=[
            {"source": "agent:reviewer", "model": "gpt-9"},
            {"source": "pdf", "model": "vllm/qwen-text"},
        ],
        requires=Requires(vision=True),
    )
    assert choice.source == "inherited"
    assert choice.ran == {"provider_id": "claude_code", "model_id": "claude-opus-5-5"}
    assert len(choice.reasons) == 2
    assert "preference gpt-9 from agent:reviewer not used: model_unknown" in choice.reasons[0]
    assert "model_requirement_unmet" in choice.reasons[1]


def test_an_inherited_model_that_cannot_run_the_child_is_refused() -> None:
    with pytest.raises(ModelSelectionRefused) as refused:
        _choose(
            requires=Requires(vision=True),
            inherited={"provider_id": "vllm", "model_id": "qwen-text"},
        )
    assert refused.value.code == "model_requirement_unmet"


def test_an_inherited_model_the_fleet_does_not_list_keeps_running_and_says_so() -> None:
    choice = _choose(inherited={"provider_id": "lm_studio", "model_id": "local"})
    assert choice.ran == {"provider_id": "lm_studio", "model_id": "local"}
    assert choice.reasons and "inherited model not checked: model_unknown" in choice.reasons[0]


def test_a_model_the_person_picked_is_recorded_as_theirs() -> None:
    choice = choose_model(
        FLEET,
        explicit="haiku",
        prefers_chain=[],
        requires=Requires(),
        attribution={},
        inherited=PARENT,
        explicit_source="person",
    )
    assert choice.source == "person" and choice.asked == "haiku"
    assert choice.ran == {"provider_id": "claude_code", "model_id": "claude-haiku-5-5"}
    with pytest.raises(ModelSelectionRefused) as refused:
        choose_model(
            FLEET,
            explicit="nope",
            prefers_chain=[],
            requires=Requires(),
            attribution={},
            inherited=PARENT,
            explicit_source="person",
        )
    assert refused.value.code == "model_unknown"
