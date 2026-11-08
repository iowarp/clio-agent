"""The golden demo must use recorded tool evidence, not the parent's claims."""

import json

from scripts.clio_coder_golden import handbacks


def test_plain_answer_claiming_success_does_not_count_as_delegation() -> None:
    messages = [{"parts": [{"type": "text", "text": "run_clio_coder verified and applied it"}]}]
    assert handbacks(messages) == []


def test_handback_reader_accepts_typed_and_legacy_tool_results_only() -> None:
    row = {"outcome": "succeeded", "verified": True, "commit": "abc"}
    parts = [
        {"type": "tool_call", "tool_name": "run_clio_coder", "structured_content": row},
        {"type": "tool_result", "tool_name": "other", "structured_content": row},
        {"type": "tool_result", "tool_name": "run_clio_coder", "structured_content": row},
        {
            "type": "tool_result",
            "tool_name": "run_clio_coder",
            "content": [
                {"type": "text", "text": "unparseable"},
                {"type": "text", "text": json.dumps(row)},
            ],
        },
    ]
    assert handbacks([{"parts": parts}]) == [row, row]
