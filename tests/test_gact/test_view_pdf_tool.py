"""Native workspace-PDF inspection and provider-wire hydration tests."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import dspy
import pytest
from dspy.lm15 import DocumentPart, TextPart, ToolResultPart
from pypdf import PdfReader, PdfWriter

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.native_tool_registry import build_native_tools
from clio_agent.gact.types import AgentDef
from clio_agent.gact.view_pdf_tool import (
    VIEW_PDF_DESCRIPTOR_TYPE,
    ViewPdfError,
    build_view_pdf_tool,
)
from clio_agent.tools.execution import tool_workspace_context
from tests._config_layer import set_config
from tests._scripted_engine import Reply, calls, scripted_lm

pytestmark = pytest.mark.usefixtures("clio_core_plane")


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


def _hydrated(result: dict[str, Any]) -> DocumentPart:
    """The loop's own hydration seam: the tool result as a native document part."""
    from clio_agent.gact.agents.clio_react_record import result_part

    document, provenance = result_part("call_0_0", "view_pdf", result, False).content
    assert isinstance(document, DocumentPart)
    assert isinstance(provenance, TextPart)
    assert result["path"] in provenance.text
    assert "snapshot" not in provenance.text
    return document


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
        "snapshot": result["snapshot"],
        "snapshot_sha256": result["snapshot_sha256"],
    }
    assert len(result["sha256"]) == 64
    assert (tmp_path / result["snapshot"]).read_bytes() == whole_document
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

    with tool_workspace_context(tmp_path):
        document = _hydrated(result)

    sliced_bytes = base64.b64decode(document.data)
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


def test_view_pdf_hydrates_the_pdf_without_mutating_the_retained_descriptor(
    tmp_path: Path,
) -> None:
    _tool, result = _descriptor(tmp_path, page_count=2)
    retained = dict(result)

    with tool_workspace_context(tmp_path):
        document = _hydrated(result)

    assert result == retained  # the durable descriptor is never expanded in place
    assert document.media_type == "application/pdf"
    assert len(PdfReader(io.BytesIO(base64.b64decode(document.data))).pages) == 2


def test_a_changed_pdf_still_shows_what_the_agent_saw(tmp_path: Path) -> None:
    """History is what the agent saw: a later change to the file does not change it."""
    _tool, result = _descriptor(tmp_path, page_count=2)
    seen = (tmp_path / result["snapshot"]).read_bytes()
    (tmp_path / "doc.pdf").write_bytes(_make_pdf(2) + b"\n%changed")

    with tool_workspace_context(tmp_path):
        document = _hydrated(result)

    assert base64.b64decode(document.data) == seen


def test_the_loop_sends_a_real_document_part_for_the_tool_result(tmp_path: Path) -> None:
    tool, _result = _descriptor(tmp_path, page_count=1)
    lm, engine = scripted_lm([calls(("view_pdf", {"path": "doc.pdf"})), Reply(text="ok")])

    with tool_workspace_context(tmp_path), dspy.context(lm=lm):
        ClioReAct(cast(Any, "question -> answer"), tools=[tool])(question="What does this PDF say?")

    results = [
        part
        for message in engine.requests[1].messages
        for part in message.parts
        if isinstance(part, ToolResultPart)
    ]
    [result] = results
    media, provenance = result.content
    assert isinstance(provenance, TextPart)
    assert "doc.pdf" in provenance.text
    assert isinstance(media, DocumentPart)
    assert media.media_type == "application/pdf"
    assert PdfReader(io.BytesIO(base64.b64decode(media.data))).get_num_pages() == 1
    assert not result.is_error


def test_view_pdf_is_exposed_only_to_pdf_capable_agents() -> None:
    agent = AgentDef(id="reader", title="Reader", tools=["view_pdf"])

    available_text, gateway_text, withheld_text = build_native_tools(
        agent, agent.tools, {}, supports_pdf=False
    )
    available_pdf, gateway_pdf, withheld_pdf = build_native_tools(
        agent, agent.tools, {}, supports_pdf=True
    )

    assert available_text == {}
    assert gateway_text == []
    assert withheld_text == {"view_pdf": "model_lacks_pdf_input"}
    assert list(available_pdf) == ["view_pdf"]
    assert gateway_pdf == []
    assert withheld_pdf == {}


def test_vision_only_config_still_gets_view_image_not_view_pdf() -> None:
    agent = AgentDef(id="both", title="Both", tools=["view_image", "view_pdf"])

    available, gateway, withheld = build_native_tools(
        agent, agent.tools, {}, supports_vision=True, supports_pdf=False
    )

    assert list(available) == ["view_image"]
    assert gateway == []
    assert withheld == {"view_pdf": "model_lacks_pdf_input"}


def test_image_only_codexlike_config_does_not_get_view_pdf() -> None:
    agent = AgentDef(id="both", title="Both", tools=["view_image", "view_pdf"])

    available, _gateway, withheld = build_native_tools(
        agent, agent.tools, {}, supports_vision=True, supports_pdf=False
    )

    assert "view_pdf" not in available
    assert withheld["view_pdf"] == "model_lacks_pdf_input"


def test_view_pdf_reaches_an_agent_only_through_a_plugin_that_claims_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agents.resolution import plugin_service, resolution_agent_def
    from clio_agent.gact.app import build_app
    from clio_agent.plugins.resolve import Delta
    from tests._plugin_fixtures import custom_marketplace, tools_module

    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={"pdf": {"files": {"ai.iowarp.clio/tools/builtin.py": tools_module("view_pdf")}}},
    )
    app = build_app(sessions_path=tmp_path / "sessions.json")
    service = plugin_service(app)

    standard = resolution_agent_def(service.resolve("clio/standard", pin=False))
    with_pdf = service.resolve("clio/standard", delta=Delta(add=("pdf",)), pin=False)

    assert "view_pdf" not in standard.tools
    [row] = [row for row in with_pdf.tools if row["name"] == "view_pdf"]
    assert (row["plugin"], row["kind"]) == ("pdf", "native")
    agent = resolution_agent_def(with_pdf)
    available, _gateway, withheld = build_native_tools(
        agent, ["view_pdf"], {}, supports_vision=True, supports_pdf=True
    )
    assert list(available) == ["view_pdf"] and withheld == {}


def test_declared_pdf_capability_uses_live_model_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.agents.native_tool_registry import declared_view_pdf_capability

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
