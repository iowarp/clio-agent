"""S4 pins for the ReActV2 retention + bounded submit-repair hooks (#901 S4).

These exercise the V2 analogs of the classic retention/repair path (design §7):

* **droppable-with-default** ``_make_submit_tool`` override — a declared Pydantic
  default makes an omitted output field droppable (the sanctioned dspy way, resolving
  the S3 pinned limitation); a field WITHOUT a declared default stays required.
* **retention** — ``forward`` publishes the retained ``History`` + pending inputs to the
  active trajectory cell (the V2 analog of ``publish_trajectory``).
* **bounded repair** — when the loop ends without every declared output field, the parent
  is RE-ASKED via a forced submit carrying a schema-derived hint, BOUNDED, and the model
  decides. clio never fabricates the outputs.

Sabotage tripwires (restored after each in the report):
  (a-bound)      ``test_repair_is_bounded`` — breaking ``range(bound)`` changes the call
                 count and turns it red.
  (a-fabricate)  ``test_repair_never_fabricates_missing_outputs`` — deterministically
                 injecting a value for the missing field turns the "field stays absent"
                 assertion red.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from dspy.adapters.types.tool import ToolCalls
from dspy.utils.dummies import DummyLM

from clio_agent.arc.prompt_recorder import PromptRecorder
from clio_agent.gact import context as ctx
from clio_agent.gact.agents import reactv2
from clio_agent.gact.agents.reactv2 import _RetainingReActV2, retaining_reactv2_cls


def _search(q: str) -> str:
    return "SEARCH_RESULT"


class _WsSig(dspy.Signature):
    """Two REQUIRED outputs (no declared default) — an omission is a real rejection."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
    workflow_state: dict[str, Any] = dspy.OutputField()


class _DefaultedSig(dspy.Signature):
    """``workflow_state`` carries a declared Pydantic default — droppable-with-default."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
    workflow_state: dict[str, Any] = dspy.OutputField(default_factory=dict)


def _build(signature: Any = "question -> answer", max_iters: int = 4) -> _RetainingReActV2:
    cls = retaining_reactv2_cls()
    return cls(signature, tools=[dspy.Tool(_search)], max_iters=max_iters)


def _capture_reasons(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def _sink(stage: str, **fields: Any) -> None:
        records.append({"stage": stage, **fields})

    monkeypatch.setattr("clio_agent.runtime.stream_audit.stream_audit", _sink)
    return records


def _reasons(records: list[dict[str, Any]]) -> list[str]:
    return [r.get("duplicate_reason") for r in records]


def _submit_call(**args: Any) -> ToolCalls:
    return ToolCalls(tool_calls=[ToolCalls.ToolCall(id="call_0_0", name="submit", args=args)])


def _non_submit_response() -> dict[str, Any]:
    return {
        "next_thought": "t",
        "tool_calls": {"tool_calls": [{"name": "search", "args": {"q": "x"}}]},
    }


# --- 1. droppable-with-default submit tool (resolves the S3 pinned limitation) ---


def test_declared_default_field_is_droppable_and_flows_default(monkeypatch) -> None:
    """A field the author declared with a Pydantic default is droppable: omitting it
    flows the DECLARED default (not rejected). This is the sanctioned dspy way, not
    clio fabricating a value — the value is the author's declared default."""
    records = _capture_reasons(monkeypatch)
    agent = _build(_DefaultedSig)
    results, final_outputs = agent._execute_tool_calls(_submit_call(answer="ONLY_ANSWER"))
    assert final_outputs == {"answer": "ONLY_ANSWER", "workflow_state": {}}
    assert results.tool_call_results[0].is_error is False
    # It succeeded (value routed to contract), NOT a rejection.
    assert reactv2.REACT_SUBMIT_INVALID_OUTPUT not in _reasons(records)
    assert reactv2.REACT_SUBMIT_FIELD_SUPPRESSED in _reasons(records)


def test_required_field_without_default_still_rejected(monkeypatch) -> None:
    """A field WITHOUT a declared default is still required: omitting it is the recorded
    ``react_submit_invalid_output`` rejection (the value does NOT flow) — droppable-with-
    default did not weaken the required-field guarantee."""
    records = _capture_reasons(monkeypatch)
    agent = _build(_WsSig)
    results, final_outputs = agent._execute_tool_calls(_submit_call(answer="ONLY_ANSWER"))
    assert final_outputs is None
    assert results.tool_call_results[0].is_error is True
    assert reactv2.REACT_SUBMIT_INVALID_OUTPUT in _reasons(records)


def test_default_field_arg_schema_unchanged_from_stock() -> None:
    """The submit tool's wire ``args``/``arg_types`` still declare every output field
    (the model is still ASKED for it) — droppable only relaxes the executor, not the
    advertised schema."""
    agent = _build(_DefaultedSig)
    submit = agent.tools["submit"]
    assert set(submit.arg_types) == {"answer", "workflow_state"}
    assert submit.arg_types["workflow_state"] == dict[str, Any]


def test_droppable_default_end_to_end_forward(monkeypatch) -> None:
    """End-to-end: a model that omits the defaulted field still terminates on ``submit``
    with the declared default filled — no rejection, no repair needed."""
    _capture_reasons(monkeypatch)
    agent = _build(_DefaultedSig)
    lm = DummyLM(
        [
            {
                "next_thought": "submit now",
                "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "DONE"}}]},
            }
        ]
    )
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        pred = agent(question="q")
    assert pred.answer == "DONE"
    assert pred.workflow_state == {}
    assert pred.termination_reason == "submit"


# --- 2. retention: forward publishes the retained History + inputs -------------


def test_forward_publishes_retained_history_and_inputs() -> None:
    """``forward`` retains the append-only ``history.messages`` + pending inputs on the
    active trajectory cell — the V2 analog of ``publish_trajectory`` the trace/repair
    consumers read."""
    ctx.install_trajectory_cell()
    agent = _build(_WsSig)
    lm = DummyLM(
        [
            _non_submit_response(),
            {
                "next_thought": "done",
                "tool_calls": {
                    "tool_calls": [
                        {"name": "submit", "args": {"answer": "A", "workflow_state": {"k": 1}}}
                    ]
                },
            },
        ]
    )
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        agent(question="find alpha")
    retained = ctx.active_trajectory()
    assert isinstance(retained, dict)
    assert retained["input_args"] == {"question": "find alpha"}
    # The retained History carries the loop's committed events (append-only).
    assert isinstance(retained["history"], list)
    assert len(retained["history"]) >= 1
    assert retained["termination_reason"] == "submit"


# --- 3. bounded repair re-asks the parent; the model decides -------------------


def test_repair_succeeds_when_reask_provides_outputs(monkeypatch) -> None:
    """When the loop ends without outputs, the bounded re-ask re-drives a forced submit;
    if the model then provides the fields, they flow to the final Prediction (the model
    decided — clio did not fabricate)."""
    monkeypatch.setenv("CLIO_SUBMIT_REPAIR_ATTEMPTS", "2")
    from clio_agent import conf

    conf.reload()
    records = _capture_reasons(monkeypatch)
    agent = _build(_WsSig, max_iters=1)
    lm = DummyLM(
        [
            _non_submit_response(),  # iter 0: a tool call (loop ends at max_iters=1)
            _non_submit_response(),  # stock forced submit: no submit -> outputs missing
            {  # repair re-ask #1: the model NOW submits the outputs
                "next_thought": "fixed",
                "tool_calls": {
                    "tool_calls": [
                        {"name": "submit", "args": {"answer": "FIXED", "workflow_state": {"s": 1}}}
                    ]
                },
            },
        ]
    )
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        pred = agent(question="q")
    assert pred.answer == "FIXED"
    assert pred.workflow_state == {"s": 1}
    assert reactv2.REACT_SUBMIT_REPAIR_ATTEMPTED in _reasons(records)
    assert reactv2.REACT_SUBMIT_REPAIR_EXHAUSTED not in _reasons(records)


def test_repair_hint_names_missing_fields_and_reaches_the_wire(monkeypatch) -> None:
    """The re-ask hint is SCHEMA-derived (names the missing declared outputs) and is
    steered onto the wire via the question input — the model can self-correct."""
    monkeypatch.setenv("CLIO_SUBMIT_REPAIR_ATTEMPTS", "1")
    from clio_agent import conf

    conf.reload()
    agent = _build(_WsSig, max_iters=1)
    recorder = PromptRecorder()
    lm = DummyLM(
        [
            _non_submit_response(),
            _non_submit_response(),
            {
                "next_thought": "fixed",
                "tool_calls": {
                    "tool_calls": [
                        {"name": "submit", "args": {"answer": "F", "workflow_state": {}}}
                    ]
                },
            },
        ]
    )
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter(), callbacks=[recorder]):
        agent(question="q")
    wire = "\n".join(c.text() for c in recorder.calls())
    assert "SUBMIT-REPAIR" in wire
    assert "`answer`" in wire and "`workflow_state`" in wire


def test_repair_is_bounded(monkeypatch) -> None:
    """SABOTAGE PIN (a-bound): the re-ask loop runs EXACTLY the configured budget of
    times and no more. Breaking the ``range(bound)`` bound changes this call count."""
    monkeypatch.setenv("CLIO_SUBMIT_REPAIR_ATTEMPTS", "2")
    from clio_agent import conf

    conf.reload()
    _capture_reasons(monkeypatch)
    agent = _build(_WsSig, max_iters=1)

    calls = {"n": 0}

    def _counting(program: Any, hint: str) -> Any:
        calls["n"] += 1
        # The model keeps omitting the outputs -> an incomplete Prediction each time.
        return dspy.Prediction(
            history=dspy.History(messages=[]), termination_reason="submit_repair"
        )

    monkeypatch.setattr(reactv2, "reforce_submit_over_retained_history", _counting)
    lm = DummyLM([_non_submit_response(), _non_submit_response()])
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        agent(question="q")
    assert calls["n"] == 2, "the bounded re-ask must run exactly the configured budget"


def test_repair_never_fabricates_missing_outputs(monkeypatch) -> None:
    """SABOTAGE PIN (a-fabricate): after the bounded budget is spent and the model STILL
    omits the outputs, the missing declared fields stay ABSENT on the returned Prediction
    (no deterministic fabrication) and ``react_submit_repair_exhausted`` is recorded.
    Injecting a fabricated value for the missing field turns the "absent" assertion red."""
    monkeypatch.setenv("CLIO_SUBMIT_REPAIR_ATTEMPTS", "2")
    from clio_agent import conf

    conf.reload()
    records = _capture_reasons(monkeypatch)
    agent = _build(_WsSig, max_iters=1)

    def _always_incomplete(program: Any, hint: str) -> Any:
        return dspy.Prediction(
            history=dspy.History(messages=[]), termination_reason="submit_repair"
        )

    monkeypatch.setattr(reactv2, "reforce_submit_over_retained_history", _always_incomplete)
    lm = DummyLM([_non_submit_response(), _non_submit_response()])
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        pred = agent(question="q")
    # The value NEVER flowed and was NEVER fabricated.
    assert "answer" not in pred.keys()
    assert "workflow_state" not in pred.keys()
    assert reactv2.REACT_SUBMIT_REPAIR_EXHAUSTED in _reasons(records)


def test_no_repair_when_outputs_present(monkeypatch) -> None:
    """A normal submit (all declared outputs present) triggers NO re-ask — zero overhead."""
    monkeypatch.setenv("CLIO_SUBMIT_REPAIR_ATTEMPTS", "3")
    from clio_agent import conf

    conf.reload()
    records = _capture_reasons(monkeypatch)
    agent = _build(_WsSig)
    lm = DummyLM(
        [
            {
                "next_thought": "done",
                "tool_calls": {
                    "tool_calls": [
                        {"name": "submit", "args": {"answer": "A", "workflow_state": {"k": 1}}}
                    ]
                },
            }
        ]
    )
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        pred = agent(question="q")
    assert pred.answer == "A"
    assert reactv2.REACT_SUBMIT_REPAIR_ATTEMPTED not in _reasons(records)


def test_forced_submit_exposes_only_submit_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forced turn cannot advertise unrelated tools to a bare-model transport."""
    agent = _build(_WsSig)
    observed: dict[str, Any] = {}

    def _react(**kwargs: Any) -> dspy.Prediction:
        observed.update(kwargs)
        return dspy.Prediction(
            next_thought="done",
            tool_calls={
                "tool_calls": [
                    {
                        "name": "submit",
                        "args": {"answer": "A", "workflow_state": {"k": 1}},
                    }
                ]
            },
        )

    monkeypatch.setattr(agent, "react", _react)
    pred = agent._forced_submit(dspy.History(messages=[]), {"question": "q"}, "max_iters", 1)

    assert [tool.name for tool in observed["tools"]] == ["submit"]
    assert observed["config"]["tool_choice"]["function"]["name"] == "submit"
    assert pred.answer == "A"
    assert pred.termination_reason == "forced_submit"


def test_forced_submit_after_empty_tool_exhaustion_is_a_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered partial answer cannot make malformed provider output successful."""
    agent = retaining_reactv2_cls()(
        "question -> answer",
        tools=[dspy.Tool(lambda q: q, name="search")],
        max_iters=1,
    )

    def react(**kwargs: Any) -> dspy.Prediction:
        del kwargs
        return dspy.Prediction(
            next_thought="I can only provide a partial answer.",
            tool_calls={
                "tool_calls": [
                    {
                        "name": "submit",
                        "args": {"answer": "partial evidence"},
                    }
                ]
            },
        )

    monkeypatch.setattr(agent, "react", react)
    pred = agent._forced_submit(dspy.History(messages=[]), {"question": "q"}, "empty_tool_calls", 1)

    assert pred.answer == "partial evidence"
    assert pred.termination_reason == "forced_submit"
    assert pred.error_info == {
        "error": "provider_protocol_error",
        "message": (
            "The provider repeatedly returned an agent step without a structured "
            "tool call. Any partial response is preserved; retry the turn."
        ),
        "details": {
            "partial": True,
            "termination_reason": "empty_tool_calls",
            "recovery_actions": ["retry_turn"],
        },
        "recoverable": True,
    }


def test_forced_submit_non_submit_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider ignoring the submit-only contract is recorded, never silently filtered."""
    records = _capture_reasons(monkeypatch)
    agent = _build(_WsSig)

    def _react(**_kwargs: Any) -> dspy.Prediction:
        return dspy.Prediction(
            next_thought="make another artifact",
            tool_calls={"tool_calls": [{"name": "create_artifact", "args": {}}]},
        )

    monkeypatch.setattr(agent, "react", _react)
    pred = agent._forced_submit(dspy.History(messages=[]), {}, "empty_tool_calls", 1)

    assert pred.termination_reason == "empty_tool_calls"
    assert reactv2.REACT_FORCED_SUBMIT_REJECTED in _reasons(records)
    row = next(
        record
        for record in records
        if record.get("duplicate_reason") == reactv2.REACT_FORCED_SUBMIT_REJECTED
    )
    assert row["full_text"] == "create_artifact"


# --- 4. reforce_submit_over_retained_history (the repair entry, tested directly) --


def test_reforce_returns_none_without_retained_history() -> None:
    """No retained History -> None, so the bounded caller STOPS (no unbounded loop)."""
    agent = _build(_WsSig)
    ctx.install_trajectory_cell()  # cell present but value=None
    assert reactv2.reforce_submit_over_retained_history(agent, "hint") is None


def test_reforce_redrives_submit_over_retained_history() -> None:
    """The repair entry re-drives a forced submit over the RETAINED History and steers the
    hint via the question input — the tool loop is NOT restarted."""
    agent = _build(_WsSig)
    ctx.install_trajectory(
        {"history": [{"next_thought": "prior"}], "input_args": {"question": "orig"}}
    )
    recorder = PromptRecorder()
    lm = DummyLM(
        [
            {
                "next_thought": "ok",
                "tool_calls": {
                    "tool_calls": [
                        {"name": "submit", "args": {"answer": "A", "workflow_state": {"k": 1}}}
                    ]
                },
            }
        ]
    )
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter(), callbacks=[recorder]):
        pred = reactv2.reforce_submit_over_retained_history(agent, "FILL station_ids")
    assert pred is not None
    assert pred.answer == "A"
    assert pred.workflow_state == {"k": 1}
    wire = "\n".join(c.text() for c in recorder.calls())
    assert "FILL station_ids" in wire  # the hint steered the re-ask
    assert "orig" in wire  # the retained question survived
