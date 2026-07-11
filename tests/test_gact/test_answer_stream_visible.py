"""#880 — the STRUCTURAL answer-visibility signal (``answer_stream_visible``).

``answer_stream_visible`` is the replacement for the deleted
``_looks_like_structured_answer`` content sniff: whether an expert's typed ``answer``
is the user-facing VISIBLE deliverable is decided from its DECLARED
``structured_outputs`` (``final_responder`` OR no ``workflow_state``), never by
inspecting whether the answer text looks like JSON. It is load-bearing on THREE
lanes that must agree:

* ``turn_forward`` sets ``state.answer_stream_visible`` at the source.
* ``turn_finalize`` reads it to blank a non-final ``workflow_state`` extract expert's
  BATCH answer fallback (so its structured contract value never leaks into the
  visible transcript).
* ``turn_delegation`` reads it for the streamed child ``field_stream`` fallback.

These tests pin the helper's truth table (so inverting/deleting the
``final_responder`` OR-branch goes RED), and pin that it does NOT DRIFT from the
signature builder's inline ``_answer_stream_visible`` computation
(``agents/builders.py:1797``) which it mirrors.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.runtime.type_parsing import (
    _structured_output_enabled,
    answer_stream_visible,
)
from clio_agent.gact.transcript import TurnTranscript
from clio_agent.gact.types import AgentDef

# A STRUCTURED (JSON) contract deliverable — the value a non-final workflow_state
# extract expert produces. It must NEVER reach the visible answer transcript.
_WS_EXTRACT_ANSWER = '{"status": "staged", "region": "cascadia", "count": 3}'


def _agent(structured_outputs: dict[str, object] | None) -> AgentDef:
    return AgentDef(
        id="expert",
        source="expert_pack",
        title="Expert",
        structured_outputs=structured_outputs or {},
    )


# (structured_outputs, expected visibility). The final_responder OR-branch MUST win
# over a declared workflow_state — deleting/inverting it flips the (True, both-set)
# and (False, workflow-only) rows.
_TRUTH_TABLE = [
    ({}, True),  # no structured outputs -> visible by default
    ({"final_responder": True}, True),  # explicit final responder
    ({"workflow_state": True}, False),  # extract expert, NOT final -> hidden
    ({"final_responder": True, "workflow_state": True}, True),  # OR-branch wins
    ({"final_responder": False, "workflow_state": True}, False),
    ({"final_responder": False, "workflow_state": False}, True),  # neither -> visible
    # QUOTED author-error strings are coerced by _structured_output_enabled: "no"/
    # "false" read as DISABLED (a plain bool("no") would be True and silently flip).
    ({"workflow_state": "false"}, True),  # disabled ws -> visible
    ({"final_responder": "no", "workflow_state": True}, False),  # disabled fr, real ws
    ({"final_responder": "true"}, True),
    ({"workflow_state": "yes"}, False),  # a truthy non-error string enables ws
]


@pytest.mark.parametrize(("structured_outputs", "expected"), _TRUTH_TABLE)
def test_answer_stream_visible_truth_table(
    structured_outputs: dict[str, object], expected: bool
) -> None:
    """The helper's visibility decision matches the declared-structured-outputs table."""
    assert answer_stream_visible(_agent(structured_outputs)) is expected


def test_none_agent_is_visible_by_default() -> None:
    """An unresolved / built-in responder (``None``) streams its answer visibly."""
    assert answer_stream_visible(None) is True


def test_final_responder_or_branch_is_load_bearing() -> None:
    """A final_responder that ALSO declares workflow_state stays VISIBLE.

    This is the exact regression the finding's sabotage targets: dropping the
    ``final_responder`` OR-branch would blank such an expert's batch answer.
    """
    both = _agent({"final_responder": True, "workflow_state": True})
    ws_only = _agent({"workflow_state": True})
    assert answer_stream_visible(both) is True
    assert answer_stream_visible(ws_only) is False


@pytest.mark.parametrize(("structured_outputs", "expected"), _TRUTH_TABLE)
def test_helper_does_not_drift_from_builders_inline_computation(
    structured_outputs: dict[str, object], expected: bool
) -> None:
    """Drift guard: the helper equals the signature builder's INLINE formula.

    ``agents/builders.py`` computes ``_answer_stream_visible`` inline for the streamed
    (#878 at-agent suppression) lane; ``answer_stream_visible`` mirrors it for the
    batch/finalize + delegation lanes. If the two computations diverge, the streamed
    and batch lanes would disagree SILENTLY. This recomputes the builders expression
    verbatim and asserts identity with the helper for every truth-table row.
    """
    # Verbatim mirror of builders.py:1797-1799.
    builders_inline = _structured_output_enabled(
        structured_outputs.get("final_responder") or False
    ) or not _structured_output_enabled(structured_outputs.get("workflow_state") or False)
    assert answer_stream_visible(_agent(structured_outputs)) is builders_inline
    assert builders_inline is expected


class _CapturePublisher:
    def publish(self, event_type: str, payload: Any) -> None:  # noqa: D401 - test stub
        pass


def _finalize_answer_texts(agent_def: AgentDef, answer_text: str) -> list[str]:
    """Drive the REAL finalize batch-answer seam gated on ``answer_stream_visible``.

    Reproduces ``turn_finalize.py:424`` VERBATIM: the canonical answer channel's
    batch fallback is ``state.answer_text`` when the responder's answer stream is
    visible, else ``""``. Returns the persisted visible ``answer``-field text parts.
    """
    transcript = TurnTranscript(
        session_id="sess_asv",
        turn_id="turn_asv",
        publisher=_CapturePublisher(),
    )
    visible = answer_stream_visible(agent_def)
    channel = transcript.turn_answer_stream(agent_def.id, "main")
    transcript.close_open_text()
    # turn_finalize.py:424 — the gate under test.
    channel.finish(fallback_text=(str(answer_text or "") if visible else ""))
    parts = transcript.finalize()
    return [
        part.text
        for part in parts
        if part.type == "text" and part.metadata.get("signature_field_name") == "answer"
    ]


def test_finalize_blanks_non_final_workflow_state_extract_answer() -> None:
    """A non-final ``workflow_state`` extract expert's JSON is NOT in the visible answer.

    ``answer_stream_visible`` is False for it, so the finalize batch fallback blanks
    to ``""`` and no visible answer part lands. Inverting the helper (the finding's
    sabotage) would leak ``_WS_EXTRACT_ANSWER`` into the transcript here.
    """
    ws_expert = _agent({"workflow_state": True})
    assert _finalize_answer_texts(ws_expert, _WS_EXTRACT_ANSWER) == []


def test_finalize_keeps_final_responder_answer_visible() -> None:
    """A declared final responder's answer DOES land as a visible part."""
    final_expert = _agent({"final_responder": True})
    assert _finalize_answer_texts(final_expert, _WS_EXTRACT_ANSWER) == [_WS_EXTRACT_ANSWER]


def test_finalize_keeps_final_responder_answer_even_with_workflow_state() -> None:
    """The OR-branch: a final responder that also declares workflow_state stays visible."""
    both = _agent({"final_responder": True, "workflow_state": True})
    assert _finalize_answer_texts(both, _WS_EXTRACT_ANSWER) == [_WS_EXTRACT_ANSWER]
