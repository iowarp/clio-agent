"""The hosted web search sources: one adapter each, normalized to :class:`WebResult`.

Each adapter translates CLIO's filters (dates, domains, kind) into what its source
supports and reports which filters it applied itself; :func:`post_filter` applies
the rest to the returned results so a filter is never silently ignored. Hosted
readers (:func:`read_page`) read a page when CLIO's own fetch could not.

Response shapes are pinned by recorded fixtures in ``tests/test_search/fixtures``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from clio_agent.search import http
from clio_agent.search.http import SourceError
from clio_agent.search.records import WebResult, domain_matches

KEENABLE = "https://api.keenable.ai"
EXA_MCP = "https://mcp.exa.ai/mcp?tools=web_search_advanced_exa,web_fetch_exa"
FIRECRAWL = "https://api.firecrawl.dev/v2/search"
YOU_MCP = "https://api.you.com/mcp?profile=free"
PARALLEL_MCP = "https://search.parallel.ai/mcp"

#: ``kind`` values ``web_search`` accepts.
KINDS = ("any", "research", "news", "pdf")


@dataclass
class WebQuery:
    """What the model asked for; the source is CLIO's choice, never the model's."""

    query: str
    after: str = ""
    before: str = ""
    include_domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    kind: str = "any"
    limit: int = 8

    def requested(self) -> set[str]:
        """The filters this query asks for."""

        wanted = {
            "after": bool(self.after),
            "before": bool(self.before),
            "include_domains": bool(self.include_domains),
            "exclude_domains": bool(self.exclude_domains),
            "kind": self.kind != "any",
        }
        return {name for name, on in wanted.items() if on}


@dataclass
class SourceAnswer:
    """One source's normalized answer plus its raw payload."""

    results: list[WebResult]
    raw: Any
    applied: set[str] = field(default_factory=set)


def _date(value: Any) -> str:
    return str(value or "")[:10]


def post_filter(answer: SourceAnswer, query: WebQuery) -> tuple[list[WebResult], list[str]]:
    """Apply the filters the source did not; return the results and those filters.

    A result without a date is kept under a date filter (its date is unknown, not
    out of range); ``kind`` cannot be checked on a result and is reported only.
    """

    missing = sorted(query.requested() - answer.applied)
    kept = list(answer.results)
    if "include_domains" in missing:
        kept = [row for row in kept if domain_matches(row.url, query.include_domains)]
    if "exclude_domains" in missing:
        kept = [row for row in kept if not domain_matches(row.url, query.exclude_domains)]
    if "after" in missing:
        kept = [row for row in kept if not row.date or row.date[:10] >= query.after]
    if "before" in missing:
        kept = [row for row in kept if not row.date or row.date[:10] <= query.before]
    return kept[: query.limit], missing


# ------------------------------------------------------------------ adapters


async def _keenable(query: WebQuery, key: str, timeout: float) -> SourceAnswer:
    body: dict[str, Any] = {"query": query.query, "max_results": min(query.limit, 50)}
    applied: set[str] = set()
    if query.after:
        body["published_after"] = query.after
        applied.add("after")
    if query.before:
        body["published_before"] = query.before
        applied.add("before")
    if len(query.include_domains) == 1:
        body["site"] = query.include_domains[0]
        applied.add("include_domains")
    if query.exclude_domains:
        body["exclude_domains"] = query.exclude_domains
        applied.add("exclude_domains")
    path = "/v1/search" if key else "/v1/search/public"
    headers = {"X-API-KEY": key} if key else {"X-Keenable-Title": http.APP_TITLE}
    response = await http.request(
        "keenable", "POST", KEENABLE + path, keyed=bool(key), timeout=timeout,
        json=body, headers=headers,
    )  # fmt: skip
    data = http.json_body("keenable", response)
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceError("keenable", http.FAILED, "no results list")
    results = [
        WebResult(
            title=str(row.get("title") or ""),
            url=str(row.get("url") or ""),
            date=_date(row.get("published_at")),
            snippet=str(row.get("snippet") or row.get("description") or ""),
        )
        for row in rows
        if isinstance(row, dict) and row.get("url")
    ]
    return SourceAnswer(results, data, applied)


_EXA_CATEGORY = {"research": "publication", "news": "news", "pdf": "pdf"}


async def _exa(query: WebQuery, key: str, timeout: float) -> SourceAnswer:
    args: dict[str, Any] = {
        "query": query.query,
        "numResults": min(query.limit, 25),
        "textMaxCharacters": 2000,
    }
    applied: set[str] = set()
    if query.after:
        args["startPublishedDate"] = query.after
        applied.add("after")
    if query.before:
        args["endPublishedDate"] = query.before
        applied.add("before")
    if query.include_domains:
        args["includeDomains"] = query.include_domains
        applied.add("include_domains")
    if query.exclude_domains:
        args["excludeDomains"] = query.exclude_domains
        applied.add("exclude_domains")
    # Exa refuses domain filters with category=publication (HTTP 400): the person's
    # domains are the stronger restriction, so the kind is then CLIO's to apply.
    if query.kind in _EXA_CATEGORY and not (
        _EXA_CATEGORY[query.kind] == "publication" and query.include_domains
    ):
        args["category"] = _EXA_CATEGORY[query.kind]
        applied.add("kind")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    result = await http.mcp_tool(
        "exa", EXA_MCP, "web_search_advanced_exa", args, headers=headers, keyed=bool(key)
    )
    text = http.mcp_text(result)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SourceError("exa", http.FAILED, "answer is not JSON") from exc
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceError("exa", http.FAILED, "no results list")
    results = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        highlights = row.get("highlights")
        snippet = " ".join(highlights) if isinstance(highlights, list) else ""
        results.append(
            WebResult(
                title=str(row.get("title") or row.get("url")),
                url=str(row["url"]),
                date=_date(row.get("publishedDate")),
                snippet=snippet or str(row.get("summary") or ""),
                text=str(row.get("text") or ""),
            )
        )
    return SourceAnswer(results, data, applied)


def _firecrawl_tbs(after: str, before: str) -> str:
    def mdy(value: str) -> str:
        year, month, day = value.split("-")
        return f"{int(month)}/{int(day)}/{year}"

    parts = ["cdr:1"]
    if after:
        parts.append(f"cd_min:{mdy(after)}")
    if before:
        parts.append(f"cd_max:{mdy(before)}")
    return ",".join(parts)


async def _firecrawl(query: WebQuery, key: str, timeout: float) -> SourceAnswer:
    body: dict[str, Any] = {"query": query.query, "limit": min(query.limit, 20)}
    applied: set[str] = set()
    if query.after or query.before:
        body["tbs"] = _firecrawl_tbs(query.after, query.before)
        applied |= {name for name in ("after", "before") if getattr(query, name)}
    if query.kind in ("research", "pdf"):
        body["categories"] = [query.kind]
        applied.add("kind")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    response = await http.request(
        "firecrawl", "POST", FIRECRAWL, keyed=bool(key), timeout=timeout,
        json=body, headers=headers,
    )  # fmt: skip
    data = http.json_body("firecrawl", response)
    web = data.get("data", {}).get("web") if isinstance(data, dict) else None
    if not isinstance(web, list):
        raise SourceError("firecrawl", http.FAILED, "no results list")
    results = [
        WebResult(
            title=str(row.get("title") or row.get("url")),
            url=str(row["url"]),
            snippet=str(row.get("description") or ""),
            text=str(row.get("markdown") or ""),
        )
        for row in web
        if isinstance(row, dict) and row.get("url")
    ]
    return SourceAnswer(results, data, applied)


async def _you(query: WebQuery, key: str, timeout: float) -> SourceAnswer:
    args: dict[str, Any] = {"query": query.query, "count": min(query.limit, 100)}
    applied: set[str] = set()
    if query.after or query.before:
        args["freshness"] = f"{query.after or '1990-01-01'}to{query.before or '2100-01-01'}"
    if query.exclude_domains:
        args["exclude_domains"] = query.exclude_domains
        applied.add("exclude_domains")
    if len(query.include_domains) == 1:
        args["query"] = f"{query.query} site:{query.include_domains[0]}"
    headers = {"X-API-Key": key} if key else {}
    result = await http.mcp_tool(
        "you", YOU_MCP, "you-search", args, headers=headers, keyed=bool(key)
    )
    try:
        data = json.loads(http.mcp_text(result))
    except ValueError as exc:
        raise SourceError("you", http.FAILED, "answer is not JSON") from exc
    web = data.get("results", {}).get("web") if isinstance(data, dict) else None
    if not isinstance(web, list):
        raise SourceError("you", http.FAILED, "no results list")
    results = []
    for row in web:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        contents = row.get("contents")
        highlights = contents.get("highlights") if isinstance(contents, dict) else None
        snippet = " ".join(map(str, highlights)) if isinstance(highlights, list) else ""
        results.append(
            WebResult(
                title=str(row.get("title") or row["url"]),
                url=str(row["url"]),
                date=_date(row.get("page_age")),
                snippet=snippet or str(row.get("description") or ""),
            )
        )
    return SourceAnswer(results, data, applied)


async def _parallel(query: WebQuery, key: str, timeout: float) -> SourceAnswer:
    args = {"objective": query.query, "search_queries": [query.query]}
    headers = {"x-api-key": key} if key else {}
    result = await http.mcp_tool(
        "parallel", PARALLEL_MCP, "web_search", args, headers=headers, keyed=bool(key)
    )
    try:
        data = json.loads(http.mcp_text(result))
    except ValueError as exc:
        raise SourceError("parallel", http.FAILED, "answer is not JSON") from exc
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceError("parallel", http.FAILED, "no results list")
    results = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        excerpts = row.get("excerpts")
        results.append(
            WebResult(
                title=str(row.get("title") or row["url"]),
                url=str(row["url"]),
                date=_date(row.get("publish_date")),
                snippet=" ".join(excerpts) if isinstance(excerpts, list) else "",
            )
        )
    return SourceAnswer(results, data, set())


async def _service(query: WebQuery, key: str, timeout: float, *, base_url: str) -> SourceAnswer:
    body = {
        "query": query.query,
        "after": query.after,
        "before": query.before,
        "include_domains": query.include_domains,
        "exclude_domains": query.exclude_domains,
        "kind": query.kind,
        "limit": query.limit,
    }
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    response = await http.request(
        "service", "POST", f"{base_url}/v1/search", keyed=bool(key), timeout=timeout,
        json=body, headers=headers,
    )  # fmt: skip
    data = http.json_body("service", response)
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceError("service", http.FAILED, "no results list")
    results = [
        WebResult(
            title=str(row.get("title") or row.get("url")),
            url=str(row["url"]),
            date=_date(row.get("date")),
            snippet=str(row.get("snippet") or ""),
        )
        for row in rows
        if isinstance(row, dict) and row.get("url")
    ]
    return SourceAnswer(results, data, set(query.requested()))


_ADAPTERS = {
    "keenable": _keenable,
    "exa": _exa,
    "firecrawl": _firecrawl,
    "you": _you,
    "parallel": _parallel,
}


async def search(
    source: str, query: WebQuery, *, key: str = "", timeout: float = 20.0, service_url: str = ""
) -> SourceAnswer:
    """Ask ``source`` for ``query``; a failure is a typed :class:`SourceError`."""

    if source == "service":
        return await _service(query, key, timeout, base_url=service_url)
    adapter = _ADAPTERS.get(source)
    if adapter is None:
        raise SourceError(source, http.FAILED, "unknown source")
    return await adapter(query, key, timeout)


# ------------------------------------------------------------- hosted readers

#: Web sources that can also read one page for CLIO (their order is preference).
READERS = ("keenable", "exa", "parallel")


async def read_page(
    source: str, url: str, *, key: str = "", timeout: float = 30.0
) -> tuple[str, str]:
    """Read ``url`` through ``source``'s hosted reader; return ``(title, text)``."""

    if source == "keenable":
        path = "/v1/fetch" if key else "/v1/fetch/public"
        headers = {"X-API-KEY": key} if key else {"X-Keenable-Title": http.APP_TITLE}
        response = await http.request(
            "keenable", "GET", f"{KEENABLE}{path}?url={quote(url, safe='')}",
            keyed=bool(key), timeout=timeout, headers=headers,
        )  # fmt: skip
        data = http.json_body("keenable", response)
        if not isinstance(data, dict):
            raise SourceError("keenable", http.FAILED, "answer is not an object")
        return str(data.get("title") or ""), str(data.get("content") or "")
    if source == "exa":
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        result = await http.mcp_tool(
            "exa", EXA_MCP, "web_fetch_exa", {"urls": [url], "maxCharacters": 60000},
            headers=headers, keyed=bool(key),
        )  # fmt: skip
        return "", http.mcp_text(result)
    if source == "parallel":
        headers = {"x-api-key": key} if key else {}
        result = await http.mcp_tool(
            "parallel", PARALLEL_MCP, "web_fetch",
            {"urls": [url], "objective": "Read the full page", "full_content": True},
            headers=headers, keyed=bool(key),
        )  # fmt: skip
        text = http.mcp_text(result)
        try:
            data = json.loads(text)
        except ValueError:
            return "", text
        rows = data.get("results") if isinstance(data, dict) else None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            row = rows[0]
            body = row.get("full_content") or "\n\n".join(row.get("excerpts") or [])
            return str(row.get("title") or ""), str(body or "")
        return "", text
    raise SourceError(source, http.FAILED, "no hosted reader")


__all__ = [
    "KINDS",
    "READERS",
    "SourceAnswer",
    "WebQuery",
    "post_filter",
    "read_page",
    "search",
]
