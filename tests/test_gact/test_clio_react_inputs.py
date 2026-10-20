"""#1267: an expert whose declared task input matches no offered kwarg is refused typed.

The runtime packs a chat turn as ``question``. Before this guard, an expert declaring
``request`` instead kept none of its kwargs and ran with an empty task.
"""

from __future__ import annotations

import dspy
import pytest

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_inputs import (
    ExpertInputsUnmappableError,
    bind_task_inputs,
)
from tests._scripted_engine import Reply, scripted_lm


def test_unmatched_task_input_is_refused_before_any_lm_call() -> None:
    lm, engine = scripted_lm([Reply(text="unused")])

    with dspy.context(lm=lm), pytest.raises(ExpertInputsUnmappableError) as info:
        ClioReAct("request -> answer", tools=[])(question="plot the stations")

    assert info.value.error_type == "expert_inputs_unmappable"
    assert info.value.details == {
        "declared_inputs": ["request"],
        "offered_inputs": ["question"],
    }
    assert engine.requests == []


def test_the_offered_names_include_system_and_media_kwargs() -> None:
    signature = dspy.Signature("system_prompt, request, images -> answer")

    with pytest.raises(ExpertInputsUnmappableError) as info:
        bind_task_inputs(signature, {"question": "q", "system_prompt": "s", "images": []})

    assert info.value.details["declared_inputs"] == ["request"]
    assert info.value.details["offered_inputs"] == ["images", "question", "system_prompt"]


def test_one_matching_task_input_is_enough() -> None:
    signature = dspy.Signature("question, region -> answer")

    assert bind_task_inputs(signature, {"question": "q", "extra": 1}) == {"question": "q"}


def test_a_signature_with_no_task_inputs_binds_whatever_it_declares() -> None:
    signature = dspy.Signature("system_prompt -> answer")

    assert bind_task_inputs(signature, {"system_prompt": "s", "question": "q"}) == {
        "system_prompt": "s"
    }
