"""Presenter for the shared ``workspace_file`` block (U3).

``view_image``/``view_pdf`` (:mod:`clio_agent.gact.view_image_tool` /
:mod:`clio_agent.gact.view_pdf_tool`) return a small, verified descriptor —
never bytes — for exactly the reason each module's docstring gives: ReAct
retains tool observations in ARC, so the actual pixels must stay out of the
durable trajectory. The owner's ask (U3) is that the transcript still show
the human "what the agent saw" — the same image/PDF page the model was
shown, as an artifact-style preview, not the ``fields:path,...`` text rows
those two tools used to declare.

This presenter turns that descriptor into ONE ``workspace_file`` block. It
never reads the file itself; the web client fetches and previews the file
independently through the ordinary workspace-file APIs, keyed by
``workspace_id`` + ``path`` and verified against ``sha256`` (the identical
hash the tool's own hydration re-check already computes) — the same
surface-reality discipline as the rest of clio's presentation layer:
:func:`native_presenters_memory._resource_link` resolves a link the same
call-time way rather than trusting a stale label.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _active_workspace_id() -> str:
    """The observing session's workspace id, or ``""`` outside a live session."""

    from clio_agent.gact import context
    from clio_agent.gact.artifacts.minting import _session_workspace_id

    app = context.active_app()
    session_id = context.active_session_id()
    if app is None or not session_id:
        return ""
    return _session_workspace_id(app, session_id)


def _resolved_pages(row: Mapping[str, Any]) -> list[int]:
    """The 1-based page numbers a ``view_pdf`` descriptor's range resolves to."""

    page_count = row.get("page_count")
    if not isinstance(page_count, int) or page_count <= 0:
        return []
    from clio_agent.gact.view_pdf_tool import resolved_view_pdf_pages

    return resolved_view_pdf_pages(str(row.get("pages") or ""), page_count)


def workspace_file_presentation(
    args: Mapping[str, Any], result: Any, structured: Any
) -> dict[str, Any]:
    """Render one ``workspace_file`` block from a view_image/view_pdf descriptor.

    ``result``/``structured`` ARE the tool's own descriptor dict on success. A
    raised ``ViewImageError``/``ViewPdfError`` reaches this presenter as
    ``result=None`` (the observer calls it with the completed call's verbatim
    result, and :mod:`clio_agent.gact.presentation_observer` appends its own
    typed error block on top) — an incomplete or absent row degrades to no
    blocks rather than fabricating one.
    """

    row: Mapping[str, Any] = (
        structured
        if isinstance(structured, Mapping)
        else result
        if isinstance(result, Mapping)
        else {}
    )
    path = str(row.get("path") or "")
    media_type = str(row.get("media_type") or "")
    sha256 = str(row.get("sha256") or "")
    if not path or not media_type or not sha256:
        return {"summary": "", "blocks": []}
    block: dict[str, Any] = {
        "id": "workspace-file",
        "type": "workspace_file",
        "workspace_id": _active_workspace_id(),
        "path": path,
        "media_type": media_type,
        "sha256": sha256,
    }
    pages = _resolved_pages(row)
    if pages:
        block["pages"] = pages
    return {"summary": "", "blocks": [block]}


__all__ = ["workspace_file_presentation"]
