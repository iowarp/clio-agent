"""A recall reads as what was asked and the steps it brought back, never as one blob."""

from __future__ import annotations

from clio_agent.gact.recall_context_tool import recall_text
from clio_agent.gact.recall_presentation import present_recall

_ROWS = [
    {"id": "step_a", "kind": "tool_call", "compacted": True, "text": "fs_read_file notes.txt"},
    {"id": "step_b", "kind": "observation", "compacted": True, "text": "Colours: teal, amber"},
]


def test_a_query_recall_names_the_query_and_shows_each_step() -> None:
    """SABOTAGE: drop the per-step blocks -> the steps vanish -> red."""
    text = recall_text(_ROWS, left_out=1, rest=["step_c"], ranked=True)
    shown = present_recall({"query": "notes colours"}, text, None)
    assert shown["subject"] == "target"
    blocks = {block["id"]: block for block in shown["blocks"]}
    assert blocks["target"]["label"] == "notes colours"
    assert shown["summary"] == "2 of 3 steps"
    assert blocks["step-1"]["text"] == "Colours: teal, amber"
    assert blocks["step-1"]["detail"] == "step_b"
    assert blocks["step-1"]["label"] == "Tool result, compacted"
    assert "left out" in blocks["notes"]["text"] and "step_c" in blocks["notes"]["text"]
    # The raw step markers are never shown as text.
    assert not any("---" in str(block.get("text")) for block in shown["blocks"])


def test_an_ids_recall_names_what_was_asked_and_an_empty_one_says_so() -> None:
    shown = present_recall({"ids": ["cmp_1"]}, recall_text(_ROWS, left_out=0, rest=[]), None)
    assert shown["blocks"][0]["label"] == "cmp_1" and shown["summary"] == "2 of 2 steps"
    empty = present_recall({"ids": ["a", "b"]}, recall_text([], left_out=0, rest=[]), None)
    assert empty["blocks"][0]["label"] == "2 steps by id"
    assert empty["summary"] == "0 of 0 steps"
