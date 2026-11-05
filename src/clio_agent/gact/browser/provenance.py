"""A browser call's files declare the page they came from (BR15).

A screenshot or a download is a file; the page it came from is its source. Its call's
result names the page URL, so the transform record of that call gains one ``used`` edge
whose authority is the URL, which
:func:`clio_agent.gact.artifacts.ingest_edges.declare_web_domains` keys ``web:<host>`` with
``net_mechanism=tool-declared``. A navigation itself is not an artifact and adds no edge:
the call record, with its URL, is how the science log's sources and the Evidence view read
browsed pages.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["page_source_edges"]


def page_source_edges(tool: str, structured: Mapping[str, Any]) -> list[Any]:
    """The ``used`` page edge of a browser call that produced a file, else none."""
    from clio_agent.gact.artifacts.transform_types import (  # noqa: PLC0415
        EdgeEvidence,
        EdgeRole,
        ProvEdge,
    )
    from clio_agent.gact.browser.tools import BROWSER_TOOLS  # noqa: PLC0415

    if tool not in BROWSER_TOOLS:
        return []
    produced = bool(structured.get("screenshot")) or any(
        isinstance(d, Mapping) and d.get("path") for d in structured.get("downloads") or ()
    )
    page = structured.get("page")
    page = page if isinstance(page, Mapping) else {}
    url = str(page.get("url") or "")
    if not produced or not url.startswith(("http://", "https://")):
        return []
    return [
        ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.ASSERTION,
            authority=url,
            external_ref=f"external:{url}",
            name=str(page.get("host") or ""),
            arg="page",
            note="browser_page",
        )
    ]
