"""Harmony wire tokens a model leaks are not replayed to the gateway (request only)."""

from __future__ import annotations

import json
from pathlib import Path

import dspy
from dspy.lm15 import Message, TextPart, ThinkingPart, ToolCallPart

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_wire import strip_wire_tags, without_wire_tags
from clio_agent.lm.engines.lm_loop import run_on_lm_loop
from tests.test_gact.test_context_projection import _plane
from tests.test_gact.test_model_stream_recovery import Attempt, endpoint, lm_for

# Verbatim shapes beam-1.0-fp8 leaked into reasoning_content on the ALCF gateway.
LEAKED = [
    "Read the table first.<|end|><|start|>]]:\nNeed example.py.",
    "Plan.<|end|>|assistant to=shell_bash<|channel|>analysis <|constrain|>xml<|message|>"
    '<rfl:parameter name="command">ls',
    "<|start|>assistant<|channel|>analysis<|message|>Check the KDE.<|end|>",
]


def test_strip_wire_tags_leaves_no_control_token_and_keeps_the_prose():
    for text in LEAKED:
        assert "<|" not in strip_wire_tags(text)
    assert strip_wire_tags(LEAKED[2]).strip() == "Check the KDE."
    assert strip_wire_tags("plain <b>html</b> and a|pipe") == "plain <b>html</b> and a|pipe"


def test_only_assistant_text_and_thinking_are_rewritten():
    call = ToolCallPart(id="c1", name="shell_bash", input={"command": "echo '<|end|>'"})
    messages = [
        Message(role="user", parts=(TextPart(text="literal <|end|> in a question"),)),
        Message(role="assistant", parts=(ThinkingPart(text=LEAKED[0]), call)),
    ]
    out = without_wire_tags(messages)
    assert out[0] is messages[0]  # user words are the user's
    assert "<|" not in out[1].parts[0].text
    assert out[1].parts[1] is call  # tool arguments are data
    assert messages[1].parts[0].text == LEAKED[0]  # the record is not touched


def test_leaked_reasoning_is_not_replayed_in_the_next_request(tmp_path: Path) -> None:
    """Live: the gateway answered HTTP 400 "Pass plain text in API fields, not Harmony
    wire tags" to the replay, and the turn ended on that error text."""
    ran: list[str] = []

    def note(text: str) -> str:
        """Record a note."""
        ran.append(text)
        return "ok"

    call = {
        "index": 0,
        "id": "n1",
        "type": "function",
        "function": {"name": "note", "arguments": json.dumps({"text": "x"})},
    }
    script = [
        Attempt([{"reasoning_content": LEAKED[1]}, {"tool_calls": [call]}]),
        Attempt([{"content": "Done."}]),
    ]
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    with endpoint(script) as (base, requests), _plane(arc):
        lm = lm_for(base, provider="vllm")
        try:
            with dspy.context(lm=lm):
                result = ClioReAct("question -> answer", tools=[note])(question="Note it")
        finally:
            run_on_lm_loop(lm.aclose)
    assert result.answer == "Done."
    assert ran == ["x"]
    assert len(requests) == 2
    assert "<|" not in json.dumps(requests[1]["messages"])
