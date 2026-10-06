"""DSPy's extract, config-driven (agent-loop rebuild, phase 6).

After a loop that took more than ``agents.react_extract.after_steps`` steps (default
3), the signature outputs the loop did not produce are extracted from the trajectory
by literally DSPy's extract: ``ChainOfThought`` over the task inputs plus the
trajectory. The answer the model already wrote (and the user already saw) is never
replaced; a ``submit`` (typed outputs) never extracts; ``enabled: false`` turns it
off. Amendment to ``docs/design/react-loop-completion-2026-09.md``.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from dspy.utils.exceptions import AdapterParseError

from clio_agent.gact.agents.clio_react import ClioReAct
from tests._scripted_engine import Reply, calls, scripted_lm

pytestmark = pytest.mark.usefixtures("clio_core_plane")


def search(q: str) -> str:
    """Search."""
    return f"hits for {q}"


def _steps(n: int) -> list[Reply]:
    return [calls(("search", {"q": str(i)}), text=f"look {i}") for i in range(n)]


def _extracted(**fields: str) -> Reply:
    body = "[[ ## reasoning ## ]]\nFrom the trajectory.\n\n"
    body += "".join(f"[[ ## {name} ## ]]\n{value}\n\n" for name, value in fields.items())
    return Reply(text=body + "[[ ## completed ## ]]")


def _run(script: list[Reply], signature: str, **kwargs: Any) -> tuple[Any, Any]:
    lm, engine = scripted_lm(script)
    with dspy.context(lm=lm):
        pred = ClioReAct(signature, tools=[search], **kwargs)(question="what?")
    return pred, engine


@pytest.fixture
def extract_config(monkeypatch: pytest.MonkeyPatch) -> Any:
    def set_config(*, enabled: bool = True, after_steps: int = 3) -> None:
        monkeypatch.setenv("CLIO_REACT_EXTRACT_ENABLED", "true" if enabled else "false")
        monkeypatch.setenv("CLIO_REACT_EXTRACT_AFTER_STEPS", str(after_steps))

    return set_config


def test_outputs_the_loop_did_not_produce_are_extracted_after_enough_steps(
    extract_config: Any,
) -> None:
    extract_config()
    script = [*_steps(4), Reply(text="The answer."), _extracted(summary="Four searches.")]
    pred, engine = _run(script, "question -> answer, summary")

    assert pred.answer == "The answer.", "the answer the model wrote is never replaced"
    assert pred.summary == "Four searches."
    assert len(engine.requests) == 6
    assert "look 3" in str(engine.requests[-1].messages), "the extract sees the trajectory"


def test_a_short_loop_does_not_extract(extract_config: Any) -> None:
    extract_config()
    pred, engine = _run([*_steps(2), Reply(text="The answer.")], "question -> answer, summary")

    assert pred.answer == "The answer."
    assert len(engine.requests) == 3


def test_a_direct_answer_with_nothing_missing_does_not_extract(extract_config: Any) -> None:
    extract_config()
    pred, engine = _run([*_steps(5), Reply(text="The answer.")], "question -> answer")

    assert pred.answer == "The answer."
    assert len(engine.requests) == 6


def test_malformed_optional_extract_keeps_the_direct_answer(extract_config: Any) -> None:
    extract_config()
    pred, engine = _run(
        [
            *_steps(4),
            Reply(text="The map and its filters are ready."),
            Reply(text="not a structured extract"),
            Reply(text="still not a structured extract"),
        ],
        "question -> answer, summary",
    )

    assert pred.answer == "The map and its filters are ready."
    assert not hasattr(pred, "summary") or pred.summary is None
    assert pred.termination_reason == "direct_response"
    assert len(engine.requests) == 7


def test_malformed_required_extract_still_fails_without_an_answer(extract_config: Any) -> None:
    extract_config()
    with pytest.raises(AdapterParseError):
        _run(
            [*_steps(4), Reply(text="not structured"), Reply(text="still not structured")],
            "question -> answer",
            max_iters=4,
        )


def test_a_loop_that_ran_out_of_steps_gets_its_answer_extracted(extract_config: Any) -> None:
    extract_config()
    script = [*_steps(4), _extracted(answer="Best found: 42.")]
    pred, _engine = _run(script, "question -> answer", max_iters=4)

    assert pred.termination_reason == "max_iters"
    assert pred.answer == "Best found: 42."


def test_extract_can_be_turned_off(extract_config: Any) -> None:
    extract_config(enabled=False)
    pred, engine = _run([*_steps(4), Reply(text="The answer.")], "question -> answer, summary")

    assert not hasattr(pred, "summary") or pred.summary is None
    assert len(engine.requests) == 5


def test_a_submit_never_extracts(extract_config: Any) -> None:
    extract_config()
    script = [*_steps(4), calls(("submit", {"answer": "A", "summary": "S"}))]
    pred, engine = _run(script, "question -> answer, summary")

    assert (pred.answer, pred.summary, pred.termination_reason) == ("A", "S", "submit")
    assert len(engine.requests) == 5


def test_the_extract_reads_the_agents_context_from_clio_core(extract_config: Any) -> None:
    """The extract sees exactly what the agent saw -- clio-core's context, including what
    the harness recorded there (the tool-use note) -- not the loop's private step list."""
    from clio_agent.gact.agents.clio_react import TOOL_USE_NOTE

    extract_config()
    script = [*_steps(4), _extracted(answer="Best found: 42.")]
    _pred, engine = _run(script, "question -> answer", max_iters=4)

    assert TOOL_USE_NOTE[:60] in str(engine.requests[-1].messages)
