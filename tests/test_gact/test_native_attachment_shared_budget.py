"""``hydrate_view_image_results``/``hydrate_view_pdf_results`` share ONE
aggregate byte budget for a single provider request.

FIX-FIRST review finding #2: each hydration seam used to keep its own
running total and check it independently, so an image-heavy step and a PDF
in the SAME step could each stay under ``resources.native_attachment_total_max_bytes``
on their own while their SUM exceeded it -- no refusal fired at hydration;
only the claude_code transport's own later, separate check caught it, and no
other transport necessarily does. ``reactv2.prepare_history_inputs`` now
threads one shared mutable running-total box through both hydration calls.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import dspy
import pytest
from dspy.adapters.types.tool import ToolCallResults, ToolCalls
from pypdf import PdfWriter

from clio_agent.gact.agents.reactv2 import prepare_history_inputs
from clio_agent.gact.view_image_tool import build_view_image_tool
from clio_agent.gact.view_pdf_tool import build_view_pdf_tool
from clio_agent.providers.native_attachment_bounds import NativeAttachmentTooLargeError
from clio_agent.tools.execution import tool_workspace_context
from tests._config_layer import set_config

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZQAAAABJRU5ErkJggg=="
)


def _make_pdf(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _mixed_history(tmp_path: Path) -> tuple[dspy.History, int, int]:
    """Build one step whose tool_calls carry BOTH a view_image and view_pdf result."""

    (tmp_path / "page.png").write_bytes(_ONE_PIXEL_PNG)
    pdf_bytes = _make_pdf(2)
    (tmp_path / "doc.pdf").write_bytes(pdf_bytes)

    image_tool = build_view_image_tool()
    pdf_tool = build_view_pdf_tool()
    with tool_workspace_context(tmp_path):
        image_result = image_tool(path="page.png")
        pdf_result = pdf_tool(path="doc.pdf", pages="")

    calls = ToolCalls(
        tool_calls=[
            ToolCalls.ToolCall(id="call_0_0", name="view_image", args={"path": "page.png"}),
            ToolCalls.ToolCall(
                id="call_0_1", name="view_pdf", args={"path": "doc.pdf", "pages": ""}
            ),
        ]
    )
    results = ToolCallResults.from_tool_calls_and_values(
        calls, [image_result, pdf_result], [False, False]
    )
    history = dspy.History(
        messages=[
            {
                "next_thought": "Inspect both attachments.",
                "tool_calls": calls.model_copy(update={"tool_call_results": results}),
            }
        ]
    )
    return history, len(_ONE_PIXEL_PNG), len(pdf_bytes)


def test_hydration_refuses_when_the_combined_total_exceeds_the_ceiling(tmp_path: Path) -> None:
    """Each attachment fits the ceiling alone; only their SUM must be refused."""

    history, image_bytes, pdf_bytes = _mixed_history(tmp_path)
    set_config("resources.native_attachment_total_max_bytes", image_bytes + pdf_bytes - 1)
    inputs: dict[str, Any] = {"history": history}

    with (
        tool_workspace_context(tmp_path),
        pytest.raises(NativeAttachmentTooLargeError) as exc_info,
    ):
        prepare_history_inputs(inputs, "history")

    assert exc_info.value.reason == "native_attachment_total_too_large"


def test_hydration_permits_the_same_pair_under_a_wide_enough_budget(tmp_path: Path) -> None:
    history, image_bytes, pdf_bytes = _mixed_history(tmp_path)
    set_config("resources.native_attachment_total_max_bytes", image_bytes + pdf_bytes)
    inputs: dict[str, Any] = {"history": history}

    with tool_workspace_context(tmp_path):
        prepare_history_inputs(inputs, "history")

    hydrated = inputs["history"].messages[0]["tool_calls"].tool_call_results.tool_call_results
    assert isinstance(hydrated[0].value, dspy.Image)
    assert isinstance(hydrated[1].value, dspy.File)
