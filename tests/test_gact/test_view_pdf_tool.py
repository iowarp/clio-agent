"""Native workspace-PDF inspection and provider-wire hydration tests."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import dspy
import pytest
from dspy.adapters.types.tool import ToolCallResults, ToolCalls
from pypdf import PdfReader, PdfWriter

from clio_agent.gact.agents.declared_native_tools import resolve_declared_native_tools
from clio_agent.gact.agents.reactv2 import _RetainingReActV2
from clio_agent.gact.types import AgentDef
from clio_agent.gact.view_pdf_tool import (
    VIEW_PDF_DESCRIPTOR_TYPE,
    ViewPdfError,
    build_view_pdf_tool,
    hydrate_view_pdf_results,
)
from clio_agent.lm.adapters import _lenient_chat_adapter_cls, _strict_guided_json_adapter_cls
from clio_agent.tools.execution import tool_workspace_context
from tests._config_layer import set_config


def _make_pdf(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _write_pdf(tmp_path: Path, name: str, page_count: int) -> Path:
    pdf_path = tmp_path / name
    pdf_path.write_bytes(_make_pdf(page_count))
    return pdf_path


def _descriptor(
    tmp_path: Path, *, page_count: int = 3, pages: str = "", name: str = "doc.pdf"
) -> tuple[Any, dict[str, Any]]:
    _write_pdf(tmp_path, name, page_count)
    tool = build_view_pdf_tool()
    with tool_workspace_context(tmp_path):
        result = tool(path=name, pages=pages)
    return tool, result


def _history(result: dict[str, Any], *, pages: str = "") -> dspy.History:
    calls = ToolCalls(
        tool_calls=[
            ToolCalls.ToolCall(
                id="call_0_0", name="view_pdf", args={"path": "doc.pdf", "pages": pages}
            )
        ]
    )
    results = ToolCallResults.from_tool_calls_and_values(calls, [result], [False])
    return dspy.History(
        messages=[
            {
                "next_thought": "Inspect the document.",
                "tool_calls": calls.model_copy(update={"tool_call_results": results}),
            }
        ]
    )


def test_view_pdf_retains_only_verified_workspace_metadata(tmp_path: Path) -> None:
    whole_document = _make_pdf(3)
    (tmp_path / "doc.pdf").write_bytes(whole_document)
    tool = build_view_pdf_tool()
    with tool_workspace_context(tmp_path):
        result = tool(path="doc.pdf", pages="")

    assert result == {
        "type": VIEW_PDF_DESCRIPTOR_TYPE,
        "path": "doc.pdf",
        "pages": "",
        "page_count": 3,
        "media_type": "application/pdf",
        "size_bytes": len(whole_document),
        "sha256": result["sha256"],
    }
    assert len(result["sha256"]) == 64
    assert "base64" not in str(result).lower()


def test_view_pdf_refuses_a_file_outside_the_active_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(_make_pdf(1))
    tool = build_view_pdf_tool()

    with tool_workspace_context(workspace), pytest.raises(ViewPdfError) as exc_info:
        tool(path=str(outside), pages="")

    assert exc_info.value.reason == "view_pdf_outside_workspace"


def test_view_pdf_refuses_a_non_pdf_file(tmp_path: Path) -> None:
    text_path = tmp_path / "notes.txt"
    text_path.write_text("hello", encoding="utf-8")
    tool = build_view_pdf_tool()

    with tool_workspace_context(tmp_path), pytest.raises(ViewPdfError) as exc_info:
        tool(path="notes.txt", pages="")

    assert exc_info.value.reason == "view_pdf_not_pdf"


def test_view_pdf_refuses_an_empty_pdf(tmp_path: Path) -> None:
    """A structurally valid but 0-page PDF has nothing to attach."""

    buffer = io.BytesIO()
    PdfWriter().write(buffer)
    (tmp_path / "empty.pdf").write_bytes(buffer.getvalue())
    tool = build_view_pdf_tool()

    with tool_workspace_context(tmp_path), pytest.raises(ViewPdfError) as exc_info:
        tool(path="empty.pdf", pages="")

    assert exc_info.value.reason == "view_pdf_empty"


def test_view_pdf_refuses_an_encrypted_pdf(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt(user_password="secret", owner_password="ownersecret")
    buffer = io.BytesIO()
    writer.write(buffer)
    (tmp_path / "locked.pdf").write_bytes(buffer.getvalue())
    tool = build_view_pdf_tool()

    with tool_workspace_context(tmp_path), pytest.raises(ViewPdfError) as exc_info:
        tool(path="locked.pdf", pages="")

    assert exc_info.value.reason == "view_pdf_encrypted"


def test_view_pdf_reads_a_permission_only_encrypted_pdf(tmp_path: Path) -> None:
    """No user password: pypdf decrypts it transparently -- content is readable.

    Must NOT be refused just because ``is_encrypted`` is true; only a PDF
    pypdf genuinely cannot decrypt gets ``view_pdf_encrypted``.
    """

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt(user_password="", owner_password="ownersecret")
    buffer = io.BytesIO()
    writer.write(buffer)
    (tmp_path / "permissions-only.pdf").write_bytes(buffer.getvalue())
    tool = build_view_pdf_tool()

    with tool_workspace_context(tmp_path):
        result = tool(path="permissions-only.pdf", pages="")

    assert result["page_count"] == 1


@pytest.mark.parametrize(
    "pages",
    ["0", "-1", "3-1", "abc", "1,,2", "1-", "-3", "99"],
)
def test_view_pdf_refuses_invalid_page_syntax_or_range(tmp_path: Path, pages: str) -> None:
    _write_pdf(tmp_path, "doc.pdf", 5)
    tool = build_view_pdf_tool()

    with tool_workspace_context(tmp_path), pytest.raises(ViewPdfError) as exc_info:
        tool(path="doc.pdf", pages=pages)

    assert exc_info.value.reason == "view_pdf_invalid_pages"


def test_view_pdf_slices_the_requested_page_range(tmp_path: Path) -> None:
    _tool, result = _descriptor(tmp_path, page_count=5, pages="2-3")

    assert result["pages"] == "2-3"
    assert result["page_count"] == 5

    inputs: dict[str, Any] = {"history": _history(result, pages="2-3")}
    with tool_workspace_context(tmp_path):
        assert hydrate_view_pdf_results(inputs, "history") == 1

    hydrated_value = (
        inputs["history"].messages[0]["tool_calls"].tool_call_results.tool_call_results[0].value
    )
    assert isinstance(hydrated_value, dspy.File)
    assert hydrated_value.file_data is not None
    header, _, encoded = hydrated_value.file_data.partition(",")
    assert header.startswith("data:application/pdf;base64")
    import base64

    sliced_bytes = base64.b64decode(encoded)
    assert len(PdfReader(io.BytesIO(sliced_bytes)).pages) == 2


def test_view_pdf_refuses_whole_document_over_the_page_ceiling(tmp_path: Path) -> None:
    set_config("limits.view_pdf_max_pages", 2)
    tool = build_view_pdf_tool()
    _write_pdf(tmp_path, "doc.pdf", 5)

    with tool_workspace_context(tmp_path), pytest.raises(ViewPdfError) as exc_info:
        tool(path="doc.pdf", pages="")

    assert exc_info.value.reason == "view_pdf_too_many_pages"
    assert "5" in str(exc_info.value)


def test_view_pdf_refuses_an_explicit_range_over_the_page_ceiling(tmp_path: Path) -> None:
    set_config("limits.view_pdf_max_pages", 2)
    tool = build_view_pdf_tool()
    _write_pdf(tmp_path, "doc.pdf", 5)

    with tool_workspace_context(tmp_path), pytest.raises(ViewPdfError) as exc_info:
        tool(path="doc.pdf", pages="1-3")

    assert exc_info.value.reason == "view_pdf_too_many_pages"


def test_view_pdf_refuses_an_oversized_document(tmp_path: Path) -> None:
    set_config("resources.native_document_max_bytes", 100)
    tool = build_view_pdf_tool()
    _write_pdf(tmp_path, "doc.pdf", 5)

    with tool_workspace_context(tmp_path), pytest.raises(ViewPdfError) as exc_info:
        tool(path="doc.pdf", pages="")

    assert exc_info.value.reason == "view_pdf_too_large"


def test_view_pdf_refuses_a_source_file_over_the_preparse_ceiling(tmp_path: Path) -> None:
    _write_pdf(tmp_path, "doc.pdf", 5)
    on_disk_size = (tmp_path / "doc.pdf").stat().st_size
    set_config("limits.view_pdf_source_max_bytes", on_disk_size - 1)
    tool = build_view_pdf_tool()

    with tool_workspace_context(tmp_path), pytest.raises(ViewPdfError) as exc_info:
        tool(path="doc.pdf", pages="")

    assert exc_info.value.reason == "view_pdf_source_too_large"


def test_preparse_source_ceiling_fires_before_the_file_is_read_or_parsed(
    tmp_path: Path,
) -> None:
    """A cheap stat() guard: it must refuse even a non-PDF before content is read.

    If the guard ran AFTER media detection instead, this would fail as
    ``view_pdf_not_pdf`` rather than ``view_pdf_source_too_large``.
    """

    garbage = tmp_path / "huge.pdf"
    garbage.write_bytes(b"not a pdf" * 100)
    set_config("limits.view_pdf_source_max_bytes", garbage.stat().st_size - 1)
    tool = build_view_pdf_tool()

    with tool_workspace_context(tmp_path), pytest.raises(ViewPdfError) as exc_info:
        tool(path="huge.pdf", pages="")

    assert exc_info.value.reason == "view_pdf_source_too_large"


def test_view_pdf_hydrates_the_pdf_without_mutating_retained_history(tmp_path: Path) -> None:
    _tool, result = _descriptor(tmp_path, page_count=2)
    retained = _history(result)
    inputs: dict[str, Any] = {"history": retained}

    with tool_workspace_context(tmp_path):
        assert hydrate_view_pdf_results(inputs, "history") == 1

    original_value = retained.messages[0]["tool_calls"].tool_call_results.tool_call_results[0].value
    hydrated_value = (
        inputs["history"].messages[0]["tool_calls"].tool_call_results.tool_call_results[0].value
    )
    assert original_value == result
    assert isinstance(hydrated_value, dspy.File)
    assert hydrated_value.file_data is not None
    assert hydrated_value.file_data.startswith("data:application/pdf;base64,")


def test_view_pdf_revalidates_hash_before_provider_delivery(tmp_path: Path) -> None:
    _tool, result = _descriptor(tmp_path, page_count=2)
    (tmp_path / "doc.pdf").write_bytes(_make_pdf(2) + b"\n%changed")
    inputs: dict[str, Any] = {"history": _history(result)}

    with tool_workspace_context(tmp_path), pytest.raises(ViewPdfError) as exc_info:
        hydrate_view_pdf_results(inputs, "history")

    assert exc_info.value.reason == "view_pdf_file_changed"


@pytest.mark.parametrize(
    "adapter_class",
    [_lenient_chat_adapter_cls, _strict_guided_json_adapter_cls],
    ids=["lenient-chat", "strict-guided-json"],
)
def test_every_adapter_emits_a_real_file_content_block(tmp_path: Path, adapter_class: Any) -> None:
    tool, result = _descriptor(tmp_path, page_count=1)
    react = _RetainingReActV2(cast(Any, "question -> answer"), tools=[tool])
    adapter = adapter_class()()
    inputs = {
        "question": "What does this PDF say?",
        "history": _history(result),
        "tools": [tool],
    }

    with tool_workspace_context(tmp_path), dspy.context(adapter=adapter):
        messages = adapter.format(react.react.signature, [], inputs)

    blocks = [
        block
        for message in messages
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
        if isinstance(block, dict)
    ]
    assert any(block.get("type") == "file" for block in blocks)


def test_view_pdf_is_exposed_only_to_pdf_capable_agents() -> None:
    agent = AgentDef(id="reader", title="Reader", tools=["view_pdf"])

    requested_text, available_text, gateway_text = resolve_declared_native_tools(
        agent, {}, supports_pdf=False
    )
    requested_pdf, available_pdf, gateway_pdf = resolve_declared_native_tools(
        agent, {}, supports_pdf=True
    )

    assert requested_text == []
    assert available_text == {}
    assert gateway_text == []
    assert requested_pdf == ["view_pdf"]
    assert list(available_pdf) == ["view_pdf"]
    assert gateway_pdf == []


def test_vision_only_config_still_gets_view_image_not_view_pdf() -> None:
    agent = AgentDef(id="both", title="Both", tools=["view_image", "view_pdf"])

    requested, available, gateway = resolve_declared_native_tools(
        agent, {}, supports_vision=True, supports_pdf=False
    )

    assert requested == ["view_image"]
    assert list(available) == ["view_image"]
    assert gateway == []


def test_image_only_codexlike_config_does_not_get_view_pdf() -> None:
    agent = AgentDef(id="both", title="Both", tools=["view_image", "view_pdf"])

    requested, available, gateway = resolve_declared_native_tools(
        agent, {}, supports_vision=True, supports_pdf=False
    )

    assert "view_pdf" not in requested
    assert "view_pdf" not in available


def test_declared_pdf_capability_uses_live_model_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.agents.declared_native_tools import declared_view_pdf_capability

    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: app)
    monkeypatch.setattr(
        "clio_agent.gact.providers.config._pdf_capability",
        lambda _app, provider, model: (
            provider == "claude_code" and model == "claude-opus-5",
            "live",
        ),
    )

    assert declared_view_pdf_capability(
        SimpleNamespace(provider_id="claude_code", model="claude-opus-5", supports_pdf=False)
    )
    assert not declared_view_pdf_capability(
        SimpleNamespace(provider_id="codex", model="gpt-5.5", supports_pdf=True)
    )
