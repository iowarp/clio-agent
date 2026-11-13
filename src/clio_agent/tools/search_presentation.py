"""Presenters for CLIO's search tools: web search/fetch and literature search/lookup.

Each turns a tool's structured result into the shared presentation contract: the
query or page as the subject, the found pages or works as links (with their
snippet or citation line), the source that answered, and every typed note
(another source used, a filter CLIO applied, a hosted reader) as a stated warning.
Importing :mod:`clio_agent.tools.tool_presentation` registers them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.search.settings import DISPLAY_NAMES
from clio_agent.tools.result_errors import structured_tool_result_error
from clio_agent.tools.tool_presentation import (
    PresentationAdapter,
    _structured,
    register_presentation_adapter,
    standard_mcp_presentation,
)

__all__: list[str] = []


def _name(source: str) -> str:
    return DISPLAY_NAMES.get(source, source)


def _notes(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    blocks = []
    for index, item in enumerate(row.get("notes") or []):
        if not isinstance(item, Mapping):
            continue
        source = f"{_name(str(item['source']))}: " if item.get("source") else ""
        detail = f" ({item['detail']})" if item.get("detail") else ""
        until = f" Until {item['until']}." if item.get("until") else ""
        blocks.append(
            {
                "id": f"note-{index}",
                "type": "text",
                "severity": "warning",
                "text": (
                    f"{source}{item['advice']}"
                    if item.get("advice")
                    else f"{source}{item.get('message', item.get('code', ''))}{detail}{until}"
                ),
            }
        )
    return blocks


def _payload(row: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
    path = row.get("payload_file")
    if not isinstance(path, str) or not path:
        return []
    return [{"id": "payload", "type": "link", "target": "resource", "uri": path, "label": label}]


def _subject_text(query: str) -> dict[str, Any]:
    return {"id": "subject", "type": "text", "text": query}


def _advice(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A failed search's explanation and next step, in words."""

    error = row.get("error")
    advice = error.get("advice") if isinstance(error, Mapping) else None
    if not isinstance(advice, str) or not advice:
        return []
    return [{"id": "advice", "type": "text", "severity": "warning", "text": advice}]


def _web_search(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del snapshot
    row = _structured(result)
    if structured_tool_result_error(result) is not None:
        failed = standard_mcp_presentation(result)
        subject = _subject_text(str(args.get("query") or row.get("query") or ""))
        failed["blocks"] = [subject, *_advice(row), *_notes(row), *failed.get("blocks", [])]
        return failed
    hits = [h for h in row.get("results") or [] if isinstance(h, Mapping) and h.get("url")]
    blocks: list[dict[str, Any]] = [_subject_text(str(args.get("query") or row.get("query") or ""))]
    blocks += _notes(row)
    for index, hit in enumerate(hits):
        blocks.append(
            {
                "id": f"result-{index}",
                "type": "link",
                "target": "url",
                "uri": str(hit["url"]),
                "label": str(hit.get("title") or hit["url"]),
            }
        )
        line = " ".join(filter(None, (str(hit.get("date") or ""), str(hit.get("snippet") or ""))))
        if line:
            blocks.append({"id": f"result-{index}-snippet", "type": "text", "text": line})
    blocks += _payload(row, "Full answer from the search source")
    count = f"{len(hits)} {'result' if len(hits) == 1 else 'results'}"
    source = _name(str(row.get("source") or ""))
    return {
        "subject": "subject",
        "summary": f"{count} from {source}" if source else count,
        "blocks": blocks,
    }


def _web_fetch(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del snapshot
    row = _structured(result)
    url = str(row.get("url") or args.get("url") or "")
    subject = {
        "id": "subject",
        "type": "link",
        "target": "url",
        "uri": url,
        "label": str(row.get("title") or url),
    }
    if structured_tool_result_error(result) is not None:
        failed = standard_mcp_presentation(result)
        failed["blocks"] = [subject, *_notes(row), *failed.get("blocks", [])]
        if row.get("pdf_path"):
            failed["blocks"].append(
                {
                    "id": "pdf",
                    "type": "link",
                    "target": "resource",
                    "uri": str(row["pdf_path"]),
                    "label": "Saved PDF",
                }
            )
        failed["subject"] = "subject"
        return failed
    blocks = [subject, *_notes(row)]
    if isinstance(row.get("content"), str):
        blocks.append({"id": "content", "type": "markdown", "text": row["content"]})
    if row.get("truncated"):
        blocks.append(
            {
                "id": "truncated",
                "type": "text",
                "severity": "warning",
                "text": f"Showing the first part of a {int(row.get('chars') or 0):,}-character page.",
            }
        )
    reader = str(row.get("reader") or "local")
    summary = "Read on this computer" if reader == "local" else f"Read through {_name(reader)}"
    return {"subject": "subject", "summary": summary, "blocks": blocks}


def _citation(work: Mapping[str, Any]) -> str:
    authors = work.get("authors") or []
    lead = f"{authors[0]}{' et al.' if len(authors) > 1 else ''}" if authors else ""
    ids = ids_value if isinstance(ids_value := work.get("ids"), Mapping) else {}
    ident = (
        f"doi:{ids['doi']}"
        if ids.get("doi")
        else f"arXiv:{ids['arxiv']}"
        if ids.get("arxiv")
        else ""
    )
    parts = [lead, str(work.get("venue") or ""), str(work.get("year") or ""), ident]
    return ", ".join(part for part in parts if part)


def _work_blocks(prefix: str, works: list[Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for index, work in enumerate(works):
        if not isinstance(work, Mapping):
            continue
        uri = str(work.get("readable_url") or "")
        ids = ids_value if isinstance(ids_value := work.get("ids"), Mapping) else {}
        label = str(work.get("title") or ids.get("doi") or ids.get("openalex") or "Untitled work")
        if uri:
            blocks.append(
                {
                    "id": f"{prefix}-{index}",
                    "type": "link",
                    "target": "url",
                    "uri": uri,
                    "label": label,
                }
            )
        else:
            blocks.append({"id": f"{prefix}-{index}", "type": "text", "text": label})
        line = _citation(work)
        found = work.get("found_in")
        if isinstance(found, list) and len(found) > 1:
            line += f" (found in {', '.join(_name(str(s)) for s in found)})"
        if line:
            blocks.append({"id": f"{prefix}-{index}-cite", "type": "text", "text": line})
    return blocks


def _literature_search(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del snapshot
    if structured_tool_result_error(result) is not None:
        return standard_mcp_presentation(result)
    row = _structured(result)
    works = list(row.get("works") or [])
    blocks = [_subject_text(str(args.get("query") or row.get("query") or "")), *_notes(row)]
    blocks += _work_blocks("work", works)
    blocks += _payload(row, "Every work found, with full abstracts")
    sources = ", ".join(_name(str(s)) for s in row.get("sources") or [])
    count = f"{len(works)} {'work' if len(works) == 1 else 'works'}"
    return {
        "subject": "subject",
        "summary": f"{count} from {sources}" if sources else count,
        "blocks": blocks,
    }


def _literature_lookup(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del snapshot
    if structured_tool_result_error(result) is not None:
        return standard_mcp_presentation(result)
    row = _structured(result)
    work = work_value if isinstance(work_value := row.get("work"), Mapping) else {}
    blocks = _work_blocks("work", [work])
    if blocks:
        blocks[0]["id"] = "subject"
    if work.get("abstract"):
        blocks.append({"id": "abstract", "type": "markdown", "text": str(work["abstract"])})
    blocks += _notes(row)
    sections = (
        ("references", "References"),
        ("cited_by", "Cited by"),
        ("versions", "Other versions"),
    )
    counts = []
    for key, title in sections:
        items = row.get(key)
        if not isinstance(items, list):
            continue
        counts.append(f"{len(items)} {title.lower()}")
        blocks.append({"id": f"{key}-heading", "type": "text", "text": f"{title} ({len(items)})"})
        if key == "versions":
            for index, version in enumerate(items):
                if isinstance(version, Mapping) and version.get("doi"):
                    blocks.append(
                        {
                            "id": f"version-{index}",
                            "type": "link",
                            "target": "url",
                            "uri": f"https://doi.org/{version['doi']}",
                            "label": f"{str(version.get('relation') or 'version').replace('-', ' ')}: {version['doi']}",
                        }
                    )
        else:
            blocks += _work_blocks(key, items)
    blocks += _payload(row, "Full lookup")
    return {"subject": "subject" if blocks else "", "summary": ", ".join(counts), "blocks": blocks}


for _tool, _present, _action in (
    ("web_search", _web_search, "Search"),
    ("web_fetch", _web_fetch, "Read"),
    ("literature_search", _literature_search, "Search literature"),
    ("literature_lookup", _literature_lookup, "Look up"),
):
    register_presentation_adapter(_tool, PresentationAdapter(_present, action=_action))
