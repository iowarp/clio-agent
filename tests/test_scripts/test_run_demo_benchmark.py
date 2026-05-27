from scripts.run_demo_benchmark import DemoCase, DemoResult, _case_row, _message_text


def test_message_text_ignores_evidence_part_text() -> None:
    message = {
        "parts": [
            {"type": "routing_decision", "text": ""},
            {"type": "expert_handoff", "text": "data | failure | direct_tool"},
            {"type": "text", "text": "Final answer"},
        ]
    }

    assert _message_text(message) == "Final answer"


def test_expected_error_case_row_records_intent() -> None:
    case = DemoCase(
        case_id="missing_file",
        title="Missing file",
        category="hardening",
        prompt="Inspect missing file",
        why="Surface real error",
        expected="Structured error",
        session_group="errors",
        expects_error=True,
    )
    result = DemoResult(
        case=case,
        session_id="sess_1",
        elapsed_s=0.1,
        provider={},
        message={
            "parts": [
                {"type": "expert_handoff", "text": "data | failure | direct_tool"},
            ],
            "metadata": {},
            "error_info": {"error": "tool_error"},
        },
    )

    row = _case_row(result)

    assert result.passed is True
    assert result.outcome == "expected_error"
    assert row["expects_error"] is True
    assert row["answer_excerpt"] == ""
