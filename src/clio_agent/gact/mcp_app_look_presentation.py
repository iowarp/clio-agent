"""How ``look_at_view`` reads in the transcript (CM1).

The row is "Look at view" on the view's title. Expansion shows the image the agent saw
(a link to the kept image), what the view last reported, and what the snapshot could not
paint. A look that did not happen (no such view, a view not on screen, a refused
snapshot) is a failed row that says why. Exact arguments stay in Technical details.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["present"]

_FAILED = {
    "mcp_app_view_none": "No such view",
    "mcp_app_view_ambiguous": "Several views are open",
    "mcp_app_view_not_shown": "The view is not on screen",
    "mcp_app_view_closed": "The view closed",
}


def _text(key: str, label: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"id": key, "type": "text", "label": label, "text": text, **extra}


def present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """The finished look: the image, the view's report, and the gaps."""
    from clio_agent.gact.mcp_app_look import coverage_lines  # noqa: PLC0415

    row = structured if isinstance(structured, Mapping) else result
    row = row if isinstance(row, Mapping) else {}
    view = row.get("view") if isinstance(row.get("view"), Mapping) else {}
    title = str(view.get("title") or args.get("view") or "view")
    blocks: list[dict[str, Any]] = []
    if row.get("ok") is False or not row.get("sha256"):
        reason = str(row.get("reason") or "")
        blocks.append(
            _text("outcome", "Not looked at", str(row.get("text") or reason), severity="warning")
        )
        views = row.get("views")
        if isinstance(views, list) and views:
            names = [
                f"{item.get('title')} ({item.get('server')}, {item.get('app_instance_id')})"
                for item in views
                if isinstance(item, Mapping)
            ]
            blocks.append(_text("views", "Open views", "\n".join(names)))
        return {
            "action": "Look at view",
            "subject": title,
            "blocks": blocks,
            "summary": _FAILED.get(reason, "The view could not be looked at"),
            "status": "failed",
        }
    if row.get("artifact_id"):
        blocks.append(
            {
                "id": "image",
                "type": "link",
                "target": "artifact",
                "uri": row["artifact_id"],
                "label": f"What {title} showed",
            }
        )
    if view.get("server"):
        blocks.append(
            _text(
                "view",
                "View",
                f"{title}, {view.get('server')} view opened by {view.get('tool')}",
            )
        )
    context = row.get("context_lines")
    if isinstance(context, list) and context:
        blocks.append(_text("context", "Last reported", "\n".join(str(x) for x in context)))
    coverage = row.get("coverage") if isinstance(row.get("coverage"), Mapping) else {}
    gaps = [line for line in coverage_lines(coverage) if line.startswith("Not in the image")]
    if gaps:
        blocks.append(_text("gaps", "Snapshot gaps", "\n".join(gaps), severity="warning"))
    pixels = row.get("pixels")
    size = f"{pixels[0]}×{pixels[1]} image" if isinstance(pixels, list) and len(pixels) == 2 else ""
    seen = "shown to the model" if row.get("attached_to_model") else ""
    return {
        "action": "Look at view",
        "subject": title,
        "blocks": blocks,
        "summary": ", ".join(item for item in (size, seen) if item) or "Looked",
    }
