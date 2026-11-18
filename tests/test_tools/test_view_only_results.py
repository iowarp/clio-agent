"""A view's call to a tool the model never sees is recorded as a size and a hash (MC-F3)."""

from __future__ import annotations

from types import SimpleNamespace

import mcp_types
from fastmcp.client.client import CallToolResult

from clio_agent.tools import view_only_results as results

VIEW_ONLY = SimpleNamespace(meta={"ui": {"visibility": ["app"]}})
DEFAULT = SimpleNamespace(meta={"ui": {"resourceUri": "ui://pdf/view"}})


def test_outside_a_view_only_call_everything_is_recorded_as_is() -> None:
    reply = {"content": [{"type": "text", "text": "x" * 10_000}]}
    big = {"data": "y" * 10_000}
    for definition in (None, DEFAULT):  # no visibility: the model sees it
        with results.scope("pdf_display_pdf", definition):
            assert results.active() is None
            assert results.observed_result(reply) is reply
            assert results.recorded_arguments(big) is big
    assert results.active() is None


def test_a_view_only_call_keeps_small_arguments_and_compacts_the_rest() -> None:
    with results.scope("pdf_read_pdf_bytes", VIEW_ONLY):
        assert results.active() == "pdf_read_pdf_bytes"
        small = {"url": "https://arxiv.org/pdf/2611.15421", "offset": 0}
        assert results.recorded_arguments(small) is small
        image = {"viewUUID": "v1", "pages": [{"page": 1, "image": "P" * 5_000}]}
        kept = results.recorded_arguments(image)["view_only_arguments"]
    assert kept["reason"] == results.VIEW_ONLY_CONTENT_NOT_STORED
    assert kept["keys"] == ["pages", "viewUUID"] and kept["bytes"] > 5_000
    assert len(kept["sha256"]) == 64
    assert results.active() is None


def test_a_failed_view_only_call_keeps_its_error_text_bounded() -> None:
    failure = {"content": [{"type": "text", "text": "PDF not found " * 100}], "isError": True}
    with results.scope("pdf_read_pdf_bytes", VIEW_ONLY):
        observed = results.observed_result(failure)
        assert results.observed_result(None) is None
    record = observed["structuredContent"]["view_only_result"]
    assert observed["isError"] is True and record["outcome"] == "error"
    assert record["error_text"].startswith("PDF not found")
    assert len(record["error_text"]) == results.ERROR_TEXT_KEPT_CHARS
    assert "was not stored" in observed["content"][0]["text"]


def test_the_ledger_copy_of_a_reply_keeps_its_type_and_drops_its_content() -> None:
    chunk = "QUJD" * 100_000
    client_reply = CallToolResult(
        content=[mcp_types.TextContent(type="text", text=chunk)],
        structured_content={"bytes": chunk},
        meta=None,
        data={"bytes": chunk},
        is_error=False,
    )
    model_reply = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=chunk)], is_error=False
    )
    assert results.stored_reply(client_reply) is client_reply  # not a view-only call
    with results.scope("pdf_read_pdf_bytes", VIEW_ONLY):
        stored = results.stored_reply(client_reply)
        stored_model = results.stored_reply(model_reply)
        assert results.stored_reply("not a reply") == "not a reply"
    assert isinstance(stored, CallToolResult) and stored.data is None
    assert stored.structured_content["view_only_result"]["bytes"] > len(chunk)
    assert chunk not in str(stored) and chunk not in str(stored_model)
    assert isinstance(stored_model, mcp_types.CallToolResult)
    assert client_reply.structured_content == {"bytes": chunk}  # the view's copy is untouched
