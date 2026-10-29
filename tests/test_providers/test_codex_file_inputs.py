"""Native-file recovery through real Responses parsing and a scripted network boundary."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import dspy
import pytest
from dspy.lm15 import (
    DocumentPart,
    ImagePart,
    InvalidRequestError,
    Message,
    Request,
    Response,
    TextPart,
    ToolResultPart,
    Usage,
    response_to_events,
)
from pypdf import PdfWriter

from clio_agent.gact import context as gact_context
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.view_pdf_tool import build_view_pdf_tool
from clio_agent.providers.codex.file_inputs import (
    FILE_PREPARATION_REFUSAL,
    FILE_RECOVERY_NOTE,
    DocumentInputRecovery,
)
from clio_agent.tools.execution import tool_workspace_context
from tests.test_providers import test_codex_direct_engine as base

harness = base.harness
loop_scope = base.loop_scope


def _make_pdf(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _document(content: bytes = b"test PDF") -> DocumentPart:
    return DocumentPart(data=base64.b64encode(content).decode(), media_type="application/pdf")


def _request(document: DocumentPart | None = None) -> Request:
    return base._request(
        Message(
            role="user", parts=(TextPart(text="Summarize these files"), document or _document())
        )
    )


def _refusal(kind: str = "error") -> list[dict[str, Any]]:
    return base._failure(
        {"code": "invalid_prompt", "message": FILE_PREPARATION_REFUSAL + "."}, kind=kind
    )


@pytest.mark.parametrize("kind", ["error", "response.failed"])
def test_refusal_changes_the_input_once_and_retains_other_context(
    harness: base.Harness, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    notifications: list[str] = []
    monkeypatch.setattr(
        "clio_agent.providers.codex.direct_engine.report_file_recovery",
        lambda: notifications.append(FILE_RECOVERY_NOTE),
    )
    request = _request()
    harness.script[:] = [_refusal(kind), "continued"]

    response = base._run(base._engine(), request)

    assert response.message.parts == (TextPart(text="continued"),)
    assert len(harness.sockets) == 2
    assert harness.sockets[0].closed
    initial, fallback = [socket.frames[0] for socket in harness.sockets]
    assert "input_file" in json.dumps(initial)
    assert "input_file" not in json.dumps(fallback)
    assert "Summarize these files" in json.dumps(fallback)
    assert FILE_RECOVERY_NOTE in json.dumps(fallback)
    assert isinstance(request.messages[0].parts[1], DocumentPart)
    assert notifications == [FILE_RECOVERY_NOTE]


def test_rejected_history_is_not_reattached_on_the_next_step(harness: base.Harness) -> None:
    engine = base._engine()
    harness.script[:] = [_refusal(), "first", "second"]
    request = _request()
    base._run(engine, request)
    base._run(engine, request)
    assert len(harness.sockets) == 3
    assert "input_file" not in json.dumps(harness.sockets[-1].frames)


def test_unrelated_documents_and_images_keep_their_native_path() -> None:
    refused, other = _document(), _document(b"another PDF")
    image = ImagePart(data=base64.b64encode(b"image bytes").decode(), media_type="image/png")
    tool = ToolResultPart(
        id="pdf", name="view_pdf", content=(refused, image, TextPart(text="path"))
    )
    request = base._request(
        Message(role="tool", parts=(tool,)), Message(role="user", parts=(other,))
    )
    recovery = DocumentInputRecovery()
    recovery.reject(_request(refused))
    prepared = recovery.prepare(request)
    result = prepared.messages[0].parts[0]
    assert isinstance(result, ToolResultPart)
    assert result.content == (TextPart(text=FILE_RECOVERY_NOTE), image, TextPart(text="path"))
    assert prepared.messages[1].parts == (other,)
    assert DocumentInputRecovery().prepare(request) is request


def test_refusal_cache_is_bounded_and_validates_capacity() -> None:
    recovery = DocumentInputRecovery(capacity=1)
    first, second = _request(), _request(_document(b"other"))
    recovery.reject(first)
    recovery.reject(second)
    assert isinstance(recovery.prepare(first).messages[0].parts[1], DocumentPart)
    assert isinstance(recovery.prepare(second).messages[0].parts[1], TextPart)
    with pytest.raises(ValueError, match="positive"):
        DocumentInputRecovery(capacity=0)


def test_a_failure_after_output_is_not_replayed(harness: base.Harness) -> None:
    harness.script[:] = [base._answer("partial", "Already shown")[:-1] + _refusal()]
    with pytest.raises(InvalidRequestError, match="requires preparation"):
        base._run(base._engine(), _request())
    assert len(harness.sockets) == 1


def test_the_refusal_without_an_attached_document_is_not_hidden(harness: base.Harness) -> None:
    harness.script[:] = [_refusal()]
    with pytest.raises(InvalidRequestError, match="requires preparation"):
        base._run(base._engine(), base._request(base.HEAD))
    assert len(harness.sockets) == 1


def test_unrelated_input_errors_are_not_hidden(harness: base.Harness) -> None:
    harness.script[:] = [base._failure({"code": "invalid_prompt", "message": "Invalid schema"})]
    with pytest.raises(InvalidRequestError, match="Invalid schema"):
        base._run(base._engine(), _request())
    assert len(harness.sockets) == 1


def test_a_second_failure_surfaces_without_another_recovery(harness: base.Harness) -> None:
    harness.script[:] = [_refusal(), _refusal()]
    with pytest.raises(InvalidRequestError, match="requires preparation"):
        base._run(base._engine(), _request())
    assert len(harness.sockets) == 2


class RefusingHttpWire:
    """Scripted HTTP boundary: one file refusal, then a successful text response."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.requests: list[Request] = []

    def stream(self, request: Request) -> Iterator[Any]:
        self.requests.append(request)
        if len(self.requests) == 1:
            if self.mode == "exception":
                raise InvalidRequestError(FILE_PREPARATION_REFUSAL)
            from types import SimpleNamespace

            frame = _refusal()[0]
            yield from base._wire().parse_stream_events(
                request, SimpleNamespace(event="error", data=json.dumps(frame))
            )
            return
        yield from response_to_events(
            Response(
                id="http-recovered",
                model=base.MODEL,
                message=Message.assistant([TextPart(text="continued")]),
                finish_reason="stop",
                usage=Usage(),
            )
        )


@pytest.mark.parametrize("mode", ["event", "exception"])
def test_http_mode_recovers_explicit_file_refusals(harness: base.Harness, mode: str) -> None:
    wire = RefusingHttpWire(mode)
    response = base._run(base._engine(wire=wire, http=True), _request())
    assert response.message.parts == (TextPart(text="continued"),)
    assert len(wire.requests) == 2
    assert isinstance(wire.requests[0].messages[0].parts[1], DocumentPart)
    assert isinstance(wire.requests[1].messages[0].parts[1], TextPart)
    assert harness.sockets == []


@pytest.fixture
def session(clio_core_plane: Any) -> Iterator[None]:
    """Use the real fixture session and its confirmed assignment for model input."""
    sid = gact_context.active_session_id()
    assignment = clio_core_plane.state.task_history.latest_assignment(sid)
    assert assignment is not None
    assert gact_context.active_assignment_handle() == assignment.handle
    yield


def _call_tool(rid: str, call_id: str, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    events = base._calling(rid, call_id)
    arguments = json.dumps(args)
    for event in events:
        if "item" in event:
            event["item"]["name"] = name
            if event["item"].get("arguments"):
                event["item"]["arguments"] = arguments
        if event["type"] == "response.function_call_arguments.delta":
            event["delta"] = arguments
        if "arguments" in event:
            event["arguments"] = arguments
        if event["type"] == "response.completed":
            for item in event["response"].get("output", []):
                item.update(name=name, arguments=arguments)
    return events


@pytest.mark.usefixtures("session")
def test_workspace_summary_continues_to_local_inspection_after_pdf_refusal(
    harness: base.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clio_core_plane: Any
) -> None:
    """Actual loop, native PDF descriptor and wire; only the provider socket is scripted."""
    from pypdf import PdfReader

    source = tmp_path / "document.pdf"
    original = _make_pdf(1)
    source.write_bytes(original)
    inspected: list[str] = []

    def inspect_pdf(path: str) -> str:
        """Inspect a workspace PDF locally after native delivery was refused."""
        inspected.append(path)
        return f"Local PDF inspection: {len(PdfReader(tmp_path / path).pages)} page"

    harness.script[:] = [
        _call_tool("view", "pdf", "view_pdf", {"path": source.name}),
        _refusal(),
        _call_tool("inspect", "local", "inspect_pdf", {"path": source.name}),
        "The workspace contains a one-page PDF; I inspected it locally.",
    ]
    lm = base._codex_lm(monkeypatch)
    with tool_workspace_context(tmp_path), dspy.context(lm=lm):
        result = ClioReAct(
            cast(Any, "question -> answer"), tools=[build_view_pdf_tool(), inspect_pdf]
        )(question="Can you check and summarize the files in this local workspace?")
    assert "inspected it locally" in result.answer
    assert inspected == [source.name]
    assert source.read_bytes() == original
    frames = [frame for socket in harness.sockets for frame in socket.frames]
    assert "document.pdf" in json.dumps(frames[2])
    assert FILE_RECOVERY_NOTE in json.dumps(frames[2])
    assert "input_file" not in json.dumps(frames[2:])
