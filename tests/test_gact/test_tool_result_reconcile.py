"""A retry must not rewrite the earlier failed or successful observation."""

from typing import Any

import pytest

from clio_agent.gact.tool_result_reconcile import reconcile_tool_result_text
from clio_agent.gact.types import Part


def _attempt(identifier: str, *, failed: bool = False) -> list[Part]:
    return [
        Part(
            id=f"{identifier}-call",
            type="tool_call",
            call_id=identifier,
            tool_name="endpoint_request",
            input={"operation": "resume"},
        ),
        Part(
            id=f"{identifier}-result",
            type="tool_result",
            call_id=identifier,
            tool_name="endpoint_request",
            is_error=failed,
            content=[
                Part(
                    id=f"{identifier}-text",
                    type="text",
                    text="original error" if failed else "live output",
                )
            ],
        ),
    ]


def _row(identifier: str | None, result: str) -> dict[str, Any]:
    return {
        "call_id": identifier,
        "name": "endpoint_request",
        "args": {"operation": "resume"},
        "result": result,
        "ok": True,
    }


def test_retry_keeps_failure_and_correct_success_output() -> None:
    parts = _attempt("first", failed=True) + _attempt("retry")
    reconcile_tool_result_text(parts, [_row("retry", "resumed successfully")])
    assert parts[1].is_error is True
    assert parts[1].content[0].text == "original error"
    assert parts[3].content[0].text == "resumed successfully"


def test_identical_reads_keep_their_own_results() -> None:
    parts = _attempt("first") + _attempt("second")
    reconcile_tool_result_text(
        parts, [_row("second", "second receipt"), _row("first", "first receipt")]
    )
    assert parts[1].content[0].text == "first receipt"
    assert parts[3].content[0].text == "second receipt"


@pytest.mark.parametrize(
    "rows",
    [
        [_row("other", "unrelated")],
        [_row(None, "ambiguous")],
        [_row(None, "one"), _row(None, "two")],
    ],
)
def test_ambiguous_or_identified_other_result_does_not_replace_text(
    rows: list[dict[str, Any]],
) -> None:
    parts = _attempt("first") + _attempt("second")
    reconcile_tool_result_text(parts, rows)
    assert parts[1].content[0].text == parts[3].content[0].text == "live output"


def test_unique_legacy_result_remains_supported() -> None:
    parts = _attempt("only")
    reconcile_tool_result_text(parts, [_row(None, "legacy receipt")])
    assert parts[1].content[0].text == "legacy receipt"
