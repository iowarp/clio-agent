"""Literature MCP server for CLIO (in-process built-in, namespace ``literature``).

Two read-only tools, claimed by the ``literature`` plugin:

    - search: find papers, preprints, reports and datasets across scholarly sources.
    - lookup: one work's metadata, readable URL, references, citers and versions.

Read a work's text with ``web_fetch`` on its ``readable_url``. See
:mod:`clio_agent.search.literature`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from clio_agent.search.literature import literature_lookup, literature_search
from clio_agent.tools.builtin_annotations import _WEB_ANNOTATIONS
from clio_agent.tools.servers.search_questions import answered_run

literature_server = FastMCP("literature")

Field_ = Literal[
    "any", "physical", "life", "chemistry", "earth", "computing", "engineering", "energy"
]


@literature_server.tool(annotations=_WEB_ANNOTATIONS)
async def search(
    ctx: Context,
    query: Annotated[
        str, Field(min_length=1, max_length=300, description="Keywords, not a question")
    ],
    after: Annotated[str, Field(description="Published on or after YYYY-MM-DD")] = "",
    before: Annotated[str, Field(description="Published on or before YYYY-MM-DD")] = "",
    field: Field_ = "any",
    kind: Literal["any", "article", "preprint", "review", "dataset"] = "any",
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> Any:
    """Search scholarly literature across OpenAlex, Crossref, arXiv, Europe PMC, OSTI.gov
    and DataCite together.

    Use keyword queries (the main terms of the topic, no filler words). With the
    default ``field="any"`` every broad source is asked; a ``field`` narrows the
    search to the sources that cover it (physical/computing: arXiv; life: Europe
    PMC; energy, engineering, earth, physical: OSTI.gov); ``kind=dataset`` asks
    DataCite for datasets. A source that did not answer is named in ``notes``
    with what to do about it. Each work has ids (DOI, arXiv, PMID), authors, year, venue, a short
    abstract, ``readable_url`` (read it with web_fetch), ``found_in`` (which
    sources returned it) and ``versions`` (preprint and published versions).
    ``payload_file`` holds every work found with full abstracts.
    """

    return await answered_run(
        ctx,
        lambda: literature_search(
            query, after=after, before=before, field=field, kind=kind, limit=limit
        ),
    )


@literature_server.tool(annotations=_WEB_ANNOTATIONS)
async def lookup(
    ctx: Context,
    id: Annotated[  # noqa: A002 - the work's identifier, as the model writes it
        str,
        Field(min_length=3, max_length=300, description="DOI, arXiv id or URL, pmid:123, or W123"),
    ],
    include: list[Literal["references", "cited_by", "versions"]] = [  # noqa: B006
        "references",
        "cited_by",
        "versions",
    ],
    limit: Annotated[int, Field(ge=1, le=50)] = 25,
) -> Any:
    """Look up one work: metadata, where to read it, what it cites and who cites it.

    Use it to follow a paper's references backward and its citations forward, to
    find the published version of a preprint (or the preprint of a paper), and
    to get the best ``readable_url`` before reading the text with web_fetch.
    """

    return await answered_run(ctx, lambda: literature_lookup(id, list(include), limit))


__all__ = ["literature_server"]
