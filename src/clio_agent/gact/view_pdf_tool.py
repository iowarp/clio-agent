"""Bounded workspace-PDF inspection for PDF-capable CLIO agents.

The PDF twin of :mod:`clio_agent.gact.view_image_tool` — same architecture,
same reasons it exists. The native tool returns a small, durable descriptor
instead of PDF bytes, because ReAct stores tool observations in ARC. The
adapter-side hydration seam (:func:`hydrate_view_pdf_results` /
:func:`promote_view_pdf_tool_messages`) revalidates the exact workspace file
immediately before the next model call and replaces the descriptor with a
native ``dspy.File`` PDF input only in the ephemeral provider request — the
SAME representation :func:`clio_agent.gact.messaging._resource_ref_file` uses
for a native PDF resource attachment, so both delivery paths reach the
provider transport (``claude_code_multimodal._document_block``) identically.

A page range narrows a long document to the pages that matter for one call
(``limits.view_pdf_max_pages`` bounds how many pages ride a single request).
The descriptor records the REQUESTED range, never the sliced bytes; hydration
re-reads the verified workspace file and re-slices it with :mod:`pypdf`, so a
retained descriptor can never smuggle stale page content past a file change.
"""

from __future__ import annotations

import hashlib
import hmac
import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.gact.resource_mime import detect_media_type
from clio_agent.providers.native_attachment_bounds import (
    NativeAttachmentTooLargeError,
    check_block_bytes,
    check_total_bytes,
)
from clio_agent.tools.execution import get_active_tool_workspace_root
from clio_agent.tools.file_policy import FileAccessPolicy

VIEW_PDF_DESCRIPTOR_TYPE = "clio.workspace_pdf.v1"
VIEW_PDF_MEDIA_TYPE = "application/pdf"

#: Default per-call page ceiling: Claude's documented per-request page limit
#: for 200K-context models. A longer document must be read in ranges.
_DEFAULT_VIEW_PDF_MAX_PAGES = 100

#: Default pre-parse ceiling on the SOURCE file, checked via a cheap ``stat()``
#: before any bytes are read or handed to pypdf. Deliberately far above
#: ``resources.native_document_max_bytes`` (the SENT-bytes ceiling that bounds
#: the SLICE :func:`_workspace_pdf` returns): a long, legitimate PDF read a
#: few pages at a time is exactly the point of the ``pages`` argument, so the
#: source file may be much bigger than any one call's output. This guard only
#: catches a pathologically large source before pypdf pays the cost of
#: reading and fully parsing it.
_DEFAULT_VIEW_PDF_SOURCE_MAX_BYTES = 512 * 1024 * 1024


class ViewPdfError(ValueError):
    """A typed refusal raised when a workspace PDF cannot be safely viewed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def view_pdf_max_pages() -> int:
    """Per-call page ceiling for ``view_pdf`` (default 100)."""

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return max(
        1,
        conf.resolve(
            "limits.view_pdf_max_pages",
            env="CLIO_VIEW_PDF_MAX_PAGES",
            default=_DEFAULT_VIEW_PDF_MAX_PAGES,
            cast=conf.as_int,
        ),
    )


def view_pdf_source_max_bytes() -> int:
    """Pre-parse byte ceiling on the SOURCE PDF file (default 512 MiB)."""

    from clio_agent import conf  # noqa: PLC0415

    return max(
        1,
        conf.resolve(
            "limits.view_pdf_source_max_bytes",
            env="CLIO_VIEW_PDF_SOURCE_MAX_BYTES",
            default=_DEFAULT_VIEW_PDF_SOURCE_MAX_BYTES,
            cast=conf.as_int,
        ),
    )


def _workspace_root() -> Path:
    raw = get_active_tool_workspace_root().strip()
    if not raw:
        raise ViewPdfError(
            "view_pdf_workspace_unavailable",
            "view_pdf requires an active CLIO workspace.",
        )
    try:
        return Path(raw).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ViewPdfError(
            "view_pdf_workspace_unavailable",
            f"The active CLIO workspace does not exist: {raw}",
        ) from exc


def _parse_page_bound(text: str, pages: str) -> int:
    stripped = text.strip()
    if not stripped.isdigit():
        raise ViewPdfError(
            "view_pdf_invalid_pages",
            f"view_pdf pages={pages!r} must use 1-based integers or ranges "
            "(e.g. '3', '1-5', '1-3,7,10-12').",
        )
    value = int(stripped)
    if value < 1:
        raise ViewPdfError(
            "view_pdf_invalid_pages",
            f"view_pdf pages={pages!r} must use 1-based page numbers.",
        )
    return value


def _parse_pages(pages: str, page_count: int) -> list[int]:
    """Parse a 1-based page-range string into a sorted, de-duplicated page list.

    An empty string selects every page. Each comma-separated segment is either
    a single page (``"3"``) or an inclusive range (``"1-5"``); every resolved
    page number must fall within ``1..page_count``.
    """

    text = pages.strip()
    if not text:
        return list(range(1, page_count + 1))
    seen: set[int] = set()
    for raw_segment in text.split(","):
        segment = raw_segment.strip()
        if not segment:
            raise ViewPdfError(
                "view_pdf_invalid_pages", f"view_pdf pages={pages!r} contains an empty segment."
            )
        if "-" in segment:
            start_text, _, end_text = segment.partition("-")
            start = _parse_page_bound(start_text, pages)
            end = _parse_page_bound(end_text, pages)
            if start > end:
                raise ViewPdfError(
                    "view_pdf_invalid_pages",
                    f"view_pdf pages={pages!r} has a descending range {segment!r}.",
                )
            span = range(start, end + 1)
        else:
            value = _parse_page_bound(segment, pages)
            span = range(value, value + 1)
        for page in span:
            if page < 1 or page > page_count:
                raise ViewPdfError(
                    "view_pdf_invalid_pages",
                    f"view_pdf pages={pages!r} requests page {page}, outside the "
                    f"document's 1-{page_count} page range.",
                )
            seen.add(page)
    return sorted(seen)


def _pdf_page_count(data: bytes, *, label: str) -> int:
    """Return the page count, refusing an unreadable, encrypted, or empty PDF.

    A permission-only encrypted PDF (no user password; pypdf decrypts it
    transparently) reads normally here. ``FileNotDecryptedError`` fires only
    when pypdf genuinely could not decrypt the content -- that gets its OWN
    typed reason (``view_pdf_encrypted``) rather than folding into the generic
    ``view_pdf_not_pdf``, since it is caught first (it subclasses
    ``PdfReadError``, so ordering here is load-bearing).
    """

    from pypdf import PdfReader  # noqa: PLC0415 - keep UI/bootstrap imports light
    from pypdf.errors import FileNotDecryptedError, PdfReadError  # noqa: PLC0415

    try:
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
    except FileNotDecryptedError as exc:
        raise ViewPdfError(
            "view_pdf_encrypted",
            f"{label} is encrypted/password-protected; view_pdf cannot read its pages "
            "without the password. Decrypt the PDF before attaching it.",
        ) from exc
    except (PdfReadError, ValueError) as exc:
        raise ViewPdfError(
            "view_pdf_not_pdf", f"{label} could not be parsed as a PDF: {exc}"
        ) from exc
    if page_count == 0:
        raise ViewPdfError("view_pdf_empty", f"{label} has 0 pages; there is nothing to attach.")
    return page_count


def _slice_pdf_pages(data: bytes, page_numbers: list[int]) -> bytes:
    """Return a new PDF's bytes containing exactly ``page_numbers`` (1-based)."""

    from pypdf import PdfReader, PdfWriter  # noqa: PLC0415

    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    for number in page_numbers:
        writer.add_page(reader.pages[number - 1])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _resolve_pages(data: bytes, pages: str, *, label: str) -> tuple[list[int], int, bytes]:
    """Return ``(requested_pages, page_count, bytes_to_send)`` for one PDF."""

    page_count = _pdf_page_count(data, label=label)
    requested = _parse_pages(pages, page_count)
    max_pages = view_pdf_max_pages()
    if len(requested) > max_pages:
        whole_document = not pages.strip()
        detail = (
            f"{label} has {page_count} pages, over the {max_pages}-page view_pdf limit"
            if whole_document
            else f"pages={pages!r} selects {len(requested)} pages, over the "
            f"{max_pages}-page view_pdf limit"
        )
        raise ViewPdfError(
            "view_pdf_too_many_pages",
            f'{detail}. Pass pages="1-{max_pages}" (or another range) to read it in parts.',
        )
    if requested == list(range(1, page_count + 1)):
        return requested, page_count, data
    return requested, page_count, _slice_pdf_pages(data, requested)


def _workspace_pdf(path: str, pages: str) -> tuple[Path, Path, bytes, int, bytes]:
    root = _workspace_root()
    requested_path = Path(path)
    candidate = requested_path if requested_path.is_absolute() else root / requested_path
    resolved = FileAccessPolicy.from_env().validate_read(str(candidate), field="path")
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ViewPdfError(
            "view_pdf_outside_workspace",
            "view_pdf can only inspect files inside the active CLIO workspace.",
        ) from exc

    # Cheap stat() before any bytes are read or parsed -- refuses a
    # pathologically large source without paying for the read + pypdf parse,
    # even though only the (usually much smaller) requested-page SLICE is
    # checked against the sent-bytes ceiling below.
    source_size = resolved.stat().st_size
    source_max = view_pdf_source_max_bytes()
    if source_size > source_max:
        raise ViewPdfError(
            "view_pdf_source_too_large",
            f"{relative.as_posix()} is {source_size} bytes, over the {source_max}-byte "
            "pre-parse source-file ceiling; view_pdf refuses to read and parse a source "
            "this large. Split or otherwise shrink the file before attaching it.",
        )

    data = resolved.read_bytes()
    media_type, _source = detect_media_type(resolved.name, data[:4096])
    if media_type != VIEW_PDF_MEDIA_TYPE:
        raise ViewPdfError(
            "view_pdf_not_pdf",
            f"view_pdf supports {VIEW_PDF_MEDIA_TYPE}; detected {media_type!r} for "
            f"{relative.as_posix()}.",
        )
    requested_pages, page_count, sliced = _resolve_pages(data, pages, label=relative.as_posix())
    try:
        check_block_bytes("document", len(sliced), label=relative.as_posix())
    except NativeAttachmentTooLargeError as exc:
        raise ViewPdfError("view_pdf_too_large", str(exc)) from exc
    return resolved, relative, data, page_count, sliced


def _descriptor(path: str, pages: str) -> dict[str, Any]:
    _resolved, relative, data, page_count, sliced = _workspace_pdf(path, pages)
    return {
        "type": VIEW_PDF_DESCRIPTOR_TYPE,
        "path": relative.as_posix(),
        "pages": pages.strip(),
        "page_count": page_count,
        "media_type": VIEW_PDF_MEDIA_TYPE,
        "size_bytes": len(sliced),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _is_descriptor(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == VIEW_PDF_DESCRIPTOR_TYPE


def _hydrate_descriptor(value: Mapping[str, Any]) -> tuple[Any, int]:
    path = str(value.get("path") or "").strip()
    if not path or Path(path).is_absolute():
        raise ViewPdfError(
            "view_pdf_descriptor_invalid",
            "The retained view_pdf result does not contain a workspace-relative path.",
        )
    pages = str(value.get("pages") or "")
    _resolved, relative, data, page_count, sliced = _workspace_pdf(path, pages)
    expected_page_count = value.get("page_count")
    expected_size = value.get("size_bytes")
    expected_sha256 = str(value.get("sha256") or "")
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if (
        relative.as_posix() != path.replace("\\", "/")
        or expected_page_count != page_count
        or expected_size != len(sliced)
        or not expected_sha256
        or not hmac.compare_digest(expected_sha256, actual_sha256)
    ):
        raise ViewPdfError(
            "view_pdf_file_changed",
            f"The workspace PDF changed after view_pdf inspected it: {relative.as_posix()}",
        )

    import dspy  # noqa: PLC0415 - keep UI/bootstrap imports light

    filename = relative.name
    return dspy.File.from_bytes(sliced, filename=filename, mime_type=VIEW_PDF_MEDIA_TYPE), len(
        sliced
    )


def hydrate_view_pdf_results(
    inputs: dict[str, Any],
    history_field_name: str,
    *,
    running_total_bytes: list[int] | None = None,
) -> int:
    """Hydrate retained view-pdf descriptors in one DSPy History input.

    The source ``dspy.History`` is replaced rather than mutated, mirroring
    :func:`clio_agent.gact.view_image_tool.hydrate_view_image_results`. Returns
    the number of hydrated PDFs; unrelated history values are byte-for-byte
    equivalent.

    ``running_total_bytes`` is a one-element mutable box shared with
    :func:`clio_agent.gact.view_image_tool.hydrate_view_image_results` for the
    SAME provider request -- see that function's docstring for why the two
    kinds must share one aggregate counter rather than each checking its own.
    """

    import dspy  # noqa: PLC0415
    from dspy.adapters.types.tool import ToolCallResults, ToolCalls  # noqa: PLC0415

    history = inputs.get(history_field_name)
    if not isinstance(history, dspy.History):
        return 0

    pdf_count = 0
    total_bytes = running_total_bytes if running_total_bytes is not None else [0]
    messages: list[dict[str, Any]] = []
    for original in history.messages:
        message = dict(original)
        tool_calls = message.get("tool_calls")
        results = tool_calls.tool_call_results if isinstance(tool_calls, ToolCalls) else None
        if not isinstance(results, ToolCallResults):
            messages.append(message)
            continue

        hydrated_results: list[ToolCallResults.ToolCallResult] = []
        changed = False
        for result in results.tool_call_results:
            if result.name != "view_pdf" or result.is_error or not _is_descriptor(result.value):
                hydrated_results.append(result)
                continue
            pdf_file, byte_length = _hydrate_descriptor(result.value)
            total_bytes[0] += byte_length
            check_total_bytes(total_bytes[0])
            hydrated_results.append(result.model_copy(update={"value": pdf_file}))
            pdf_count += 1
            changed = True
        if changed:
            assert isinstance(tool_calls, ToolCalls)
            hydrated = results.model_copy(update={"tool_call_results": hydrated_results})
            message["tool_calls"] = tool_calls.model_copy(update={"tool_call_results": hydrated})
        messages.append(message)

    if pdf_count:
        inputs[history_field_name] = dspy.History(messages=messages)
    return pdf_count


def promote_view_pdf_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Promote JSONAdapter tool-file markers into real user file blocks.

    Mirrors :func:`clio_agent.gact.view_image_tool.promote_view_image_tool_messages`
    for the ``"file"`` content-part shape ``dspy.File`` formats to.
    """

    from dspy.adapters.types.base_type import (  # noqa: PLC0415
        CUSTOM_TYPE_END_IDENTIFIER,
        CUSTOM_TYPE_START_IDENTIFIER,
        split_message_content_for_custom_types,
    )

    promoted: list[dict[str, Any]] = []
    for original in messages:
        message = dict(original)
        content = message.get("content")
        if not (
            message.get("role") == "tool"
            and message.get("name") == "view_pdf"
            and isinstance(content, str)
            and CUSTOM_TYPE_START_IDENTIFIER in content
            and CUSTOM_TYPE_END_IDENTIFIER in content
        ):
            promoted.append(message)
            continue

        file_message = {"role": "user", "content": content}
        split_message_content_for_custom_types([file_message])
        blocks = file_message.get("content")
        if not (
            isinstance(blocks, list)
            and any(isinstance(block, Mapping) and block.get("type") == "file" for block in blocks)
        ):
            promoted.append(message)
            continue
        message["content"] = "Workspace PDF attached in the following user message."
        promoted.extend([message, file_message])
    return promoted


def build_view_pdf_tool() -> Any:
    """Build the declared native tool that inspects one workspace PDF."""

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def view_pdf(path: str, pages: str = "") -> dict[str, Any]:
        """Attach a workspace PDF (optionally a page range) to the next model step.

        Use this to read or verify a PDF natively — for a long document, read it
        a page range at a time (``pages="1-100"``) rather than the whole file in
        one call; both the text and page images reach the model. ``pages`` accepts
        1-based page numbers/ranges (e.g. ``"3"``, ``"1-5"``, ``"1-3,7,10-12"``);
        empty means the whole document, bounded by the configured page ceiling.
        The file is size-bounded, media-sniffed, hashed, and revalidated before
        its pages reach the model.
        """

        return _descriptor(path, pages)

    return native_tool(
        view_pdf,
        name="view_pdf",
        presentation="fields:path,pages,page_count,size_bytes",
        domain="workspace",
        desc=view_pdf.__doc__,
        title="View PDF",
        args={
            "path": {
                "type": "string",
                "description": "Workspace-relative or absolute path to a PDF file.",
            },
            "pages": {
                "type": "string",
                "default": "",
                "description": (
                    "1-based page numbers/ranges to attach (e.g. '3', '1-5', '1-3,7,10-12'); "
                    "empty attaches the whole document, bounded by the page ceiling."
                ),
            },
        },
    )


__all__ = [
    "VIEW_PDF_DESCRIPTOR_TYPE",
    "VIEW_PDF_MEDIA_TYPE",
    "ViewPdfError",
    "build_view_pdf_tool",
    "hydrate_view_pdf_results",
    "promote_view_pdf_tool_messages",
    "view_pdf_max_pages",
    "view_pdf_source_max_bytes",
]
