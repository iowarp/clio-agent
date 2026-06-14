"""Tests for S4 trajectory retention + prediction-summary enrichment.

These guard the load-bearing pieces of the canonical-trace capture: the
dspy.ReAct subclass that publishes its trajectory BEFORE the final extract
(so a failed extract still exposes it for capture + re-extract repair), and
the enrichment of llm.response.completed payloads with trajectory + reasoning.
"""

import dspy
import pytest

from clio_agent.gact import app as gact_app


class _AttrDict(dict):
    """dict whose keys are also attribute-accessible (mimics dspy.Prediction)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def test_retaining_react_is_react_subclass():
    cls = gact_app._retaining_react_cls()
    assert issubclass(cls, dspy.ReAct)
    # Cached: same class object on repeat calls.
    assert gact_app._retaining_react_cls() is cls


def test_retaining_react_publishes_trajectory_before_failed_extract():
    cls = gact_app._retaining_react_cls()
    inst = object.__new__(cls)  # bypass dspy.ReAct.__init__ (no LM needed)
    inst.max_iters = 3
    inst.react = object()
    inst.extract = object()
    inst.tools = {"finish": lambda: "Completed."}

    def fake_trunc(module, trajectory, **input_args):
        if module is inst.react:
            return _AttrDict(next_thought="t", next_tool_name="finish", next_tool_args={})
        # extract step fails the way a dropped-required-field model does
        raise ValueError("missing required field: station_ids")

    inst._call_with_potential_trajectory_truncation = fake_trunc

    token = gact_app._ACTIVE_REACT_TRAJECTORY.set(None)
    try:
        with pytest.raises(ValueError):
            inst.forward(question="find stations near San Diego")

        retained = gact_app._ACTIVE_REACT_TRAJECTORY.get()
        assert retained is not None
        # input_args are retained so extract can be re-run over the trajectory.
        assert retained["input_args"]["question"] == "find stations near San Diego"
        # the finish step was recorded in the trajectory before extract ran
        assert any(k.startswith("tool_name_") for k in retained["trajectory"])
    finally:
        gact_app._ACTIVE_REACT_TRAJECTORY.reset(token)


def test_retaining_react_success_returns_trajectory_in_prediction():
    cls = gact_app._retaining_react_cls()
    inst = object.__new__(cls)
    inst.max_iters = 3
    inst.react = object()
    inst.extract = object()
    inst.tools = {"finish": lambda: "Completed."}

    def fake_trunc(module, trajectory, **input_args):
        if module is inst.react:
            return _AttrDict(next_thought="t", next_tool_name="finish", next_tool_args={})
        return _AttrDict(answer="done", reasoning="because")

    inst._call_with_potential_trajectory_truncation = fake_trunc

    token = gact_app._ACTIVE_REACT_TRAJECTORY.set(None)
    try:
        result = inst.forward(question="q")
        assert result.answer == "done"
        assert result.trajectory  # trajectory threaded into the Prediction
    finally:
        gact_app._ACTIVE_REACT_TRAJECTORY.reset(token)


def test_prediction_summary_attaches_trajectory_and_reasoning():
    pred = _AttrDict(
        answer="A",
        trajectory={"thought_0": "t", "tool_name_0": "finish"},
        reasoning="chain of thought",
    )
    summary = gact_app._prediction_summary(pred)
    assert summary["answer"] == "A"
    assert summary["trajectory"]  # full capture present
    assert summary["reasoning"] == "chain of thought"


def test_prediction_summary_omits_absent_trajectory_and_reasoning():
    pred = _AttrDict(answer="A")
    summary = gact_app._prediction_summary(pred)
    # Lean payload for routing/predict: no empty trajectory/reasoning keys.
    assert "trajectory" not in summary
    assert "reasoning" not in summary


class _FakeProgram:
    """Stands in for a dspy.ReAct: only extract + _format_trajectory used."""

    def __init__(self, *, fail=False):
        self.extract_calls = []
        self._fail = fail

    def _format_trajectory(self, trajectory):
        return f"FMT:{sorted(trajectory)}"

    def extract(self, **kwargs):
        self.extract_calls.append(kwargs)
        if self._fail:
            raise ValueError("still missing required field: station_ids")
        return _AttrDict(answer="fixed", station_ids=["SIO5"])


def test_reextract_reruns_only_extract_over_retained_trajectory():
    prog = _FakeProgram()
    token = gact_app._ACTIVE_REACT_TRAJECTORY.set(
        {"trajectory": {"tool_name_0": "shell_bash"}, "input_args": {"question": "find stations"}}
    )
    try:
        result = gact_app._reextract_over_retained_trajectory(prog, "FILL station_ids")
        assert result is not None
        assert result.answer == "fixed"
        # extract ran exactly once (NOT the whole tool loop)
        assert len(prog.extract_calls) == 1
        call = prog.extract_calls[0]
        assert "FILL station_ids" in call["question"]  # hint steered the re-extract
        assert call["trajectory"].startswith("FMT:")  # retained trajectory formatted + passed
        # original trajectory threaded back into the Prediction
        assert result.trajectory == {"tool_name_0": "shell_bash"}
    finally:
        gact_app._ACTIVE_REACT_TRAJECTORY.reset(token)


def test_reextract_returns_none_without_retained_trajectory():
    prog = _FakeProgram()
    token = gact_app._ACTIVE_REACT_TRAJECTORY.set(None)
    try:
        assert gact_app._reextract_over_retained_trajectory(prog, "hint") is None
        assert prog.extract_calls == []
    finally:
        gact_app._ACTIVE_REACT_TRAJECTORY.reset(token)


def test_reextract_returns_none_when_extract_fails():
    prog = _FakeProgram(fail=True)
    token = gact_app._ACTIVE_REACT_TRAJECTORY.set(
        {"trajectory": {"tool_name_0": "x"}, "input_args": {"question": "q"}}
    )
    try:
        # extract raises again -> None, so the caller falls back to full re-ask.
        assert gact_app._reextract_over_retained_trajectory(prog, "hint") is None
    finally:
        gact_app._ACTIVE_REACT_TRAJECTORY.reset(token)
