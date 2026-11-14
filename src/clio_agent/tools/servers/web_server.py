"""Web MCP server for CLIO (in-process built-in, namespace ``web``).

Two read-only tools, claimed by the ``web`` plugin:

    - search: find pages through the person's hosted web sources, in their order.
    - fetch: read one page as Markdown (locally first, a hosted reader if needed).

Sources, their order, approvals and keys are the person's (Settings → Web search,
``search.*`` config); the model chooses only *what* to find. See
:mod:`clio_agent.search.web` and :mod:`clio_agent.search.fetch`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from clio_agent.search.fetch import MAX_CHARS
from clio_agent.search.fetch import web_fetch as _web_fetch
from clio_agent.search.web import web_search as _web_search
from clio_agent.search.web_sources import WebQuery
from clio_agent.tools.builtin_annotations import _WEB_ANNOTATIONS
from clio_agent.tools.servers.search_questions import answered_run

web_server = FastMCP("web")


@web_server.tool(annotations=_WEB_ANNOTATIONS)
async def search(
    ctx: Context,
    query: Annotated[str, Field(min_length=1, max_length=500)],
    after: Annotated[str, Field(description="Only pages published on or after YYYY-MM-DD")] = "",
    before: Annotated[str, Field(description="Only pages published on or before YYYY-MM-DD")] = "",
    include_domains: Annotated[
        list[str], Field(description="Only these sites, e.g. ['nasa.gov', 'arxiv.org']")
    ] = [],  # noqa: B006 - pydantic copies defaults
    exclude_domains: Annotated[list[str], Field(description="Never these sites")] = [],  # noqa: B006
    kind: Literal["any", "research", "news", "pdf"] = "any",
    limit: Annotated[int, Field(ge=1, le=50)] = 8,
) -> Any:
    """Search the web; returns a compact list of pages to read with web_fetch.

    Write a specific query with the key terms and context. Put dates, sites and
    the kind of page in the filters, not in the query text. Each result has a
    title, URL, date when known and a short snippet; ``payload_file`` holds
    everything the source returned (longer excerpts), readable with the file
    tools. Snippets are leads, not evidence: fetch a page before relying on it.
    ``notes`` say when CLIO used the next source, applied a filter itself
    (``filters_applied_by_clio``) or could not reach a source.
    """

    query_ = WebQuery(
        query=query,
        after=after,
        before=before,
        include_domains=[d.strip().lower() for d in include_domains if d.strip()],
        exclude_domains=[d.strip().lower() for d in exclude_domains if d.strip()],
        kind=kind,
        limit=limit,
    )
    return await answered_run(ctx, lambda: _web_search(query_))


@web_server.tool(annotations=_WEB_ANNOTATIONS)
async def fetch(
    ctx: Context,
    url: Annotated[str, Field(min_length=8, max_length=4000)],
    max_chars: Annotated[int, Field(ge=1000, le=MAX_CHARS)] = MAX_CHARS,
) -> Any:
    """Read one web page and return its text as Markdown.

    Returns ``ok``, ``status``, the final ``url`` after redirects, ``title`` and
    ``content``. Quote from ``content`` exactly when you cite it. A page CLIO
    cannot read itself (blocked, script-only, a PDF) may be read through an
    approved search source's reader; ``reader`` and ``notes`` say so. A PDF is
    also saved locally (``pdf_path``) for ``view_pdf``. Private addresses never
    go to a hosted reader.
    """

    return await answered_run(ctx, lambda: _web_fetch(url, max_chars))


__all__ = ["web_server"]
