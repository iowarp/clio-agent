"""``web_fetch``: read one page locally, falling back to a hosted reader when it helps.

The result is the shape the deep-research evidence binding reads: ``ok``, an HTTP
``status``, the final ``url`` and the page ``content`` (Markdown for HTML pages).
The content is complete up to a hard cap; CLIO's tool-result bound decides how
much of it the model sees and spills the rest to a file, while the task record
keeps the whole page for evidence checks.

A PDF is saved to the session's tool-output folder (read it with ``view_pdf``);
when the URL is public an approved hosted reader also returns its text.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from clio_agent.search import http
from clio_agent.search.reasons import SearchNote, note
from clio_agent.search.records import payload_directory
from clio_agent.search.settings import load_search_settings
from clio_agent.search.web import hosted_read

#: Bytes CLIO downloads for one page.
MAX_BYTES = 15 * 1024 * 1024
#: Characters of page text a fetch returns at most.
MAX_CHARS = 400_000
#: Below this much extracted text an HTML page counts as script-only or empty.
MIN_TEXT_CHARS = 200
#: Local answers a hosted reader may fix (blocked, rate-limited, unavailable).
READER_STATUSES = frozenset({401, 403, 406, 429, 451, 500, 502, 503, 504})

_TEXT_TYPES = (
    "text/plain",
    "text/markdown",
    "application/json",
    "text/csv",
    "application/xml",
    "text/xml",
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def html_to_markdown(document: str, url: str) -> tuple[str, str]:
    """Main text of an HTML page as Markdown, plus its title."""

    import trafilatura  # noqa: PLC0415 - heavy import, only when a page is read

    text = trafilatura.extract(
        document,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        favor_recall=True,
    )
    meta = trafilatura.extract_metadata(document)
    title = (meta.title if meta is not None and meta.title else "") or ""
    if not title:
        match = _TITLE.search(document)
        title = re.sub(r"\s+", " ", match.group(1)).strip() if match else ""
    return text or "", title


def _save_pdf(body: bytes, url: str) -> str:
    import hashlib  # noqa: PLC0415

    name = hashlib.sha256(url.encode()).hexdigest()[:16] + ".pdf"
    try:
        target = payload_directory() / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    except OSError:
        return ""
    return str(target)


async def _download(url: str, timeout: float) -> tuple[httpx.Response | None, bytes, str]:
    try:
        async with http.client().stream("GET", url, timeout=timeout) as response:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    return response, b"".join(chunks), "page larger than the download limit"
                chunks.append(chunk)
            return response, b"".join(chunks), ""
    except httpx.HTTPError as exc:
        return None, b"", f"{type(exc).__name__}: {exc}"[:300]


async def web_fetch(url: str, max_chars: int = MAX_CHARS) -> dict[str, Any]:
    """Read ``url``; see the module docstring for the result shape."""

    if not url.startswith(("http://", "https://")):
        return {
            "ok": False,
            "url": url,
            "error": note("fetch_failed", detail="url must be http(s)").as_dict(),
        }
    limit = max(1000, min(int(max_chars), MAX_CHARS))
    settings = load_search_settings()
    response, body, problem = await _download(url, settings.request_timeout_s)
    notes: list[SearchNote] = []
    status = response.status_code if response is not None else 0
    final_url = str(response.url) if response is not None else url
    ctype = (
        response.headers.get("content-type", "").split(";")[0].strip().lower()
        if response is not None
        else ""
    )
    title, content, why, pdf_path = "", "", "", ""
    if problem and response is None:
        why = problem
    elif status in READER_STATUSES:
        why = f"HTTP {status}"
    elif 200 <= status < 300:
        is_pdf = ctype == "application/pdf" or body[:5] == b"%PDF-"
        if is_pdf:
            pdf_path = _save_pdf(body, final_url)
            why = "PDF"
        elif ctype.startswith(_TEXT_TYPES):
            content = (
                body.decode(response.encoding or "utf-8", errors="replace") if response else ""
            )
        else:
            encoding = response.encoding if response is not None else None
            content, title = html_to_markdown(
                body.decode(encoding or "utf-8", errors="replace"), final_url
            )
            if len(content.strip()) < MIN_TEXT_CHARS:
                why = "page has no readable text (script-only or empty)"
    if why:
        hosted = await hosted_read(final_url, settings, why)
        if hosted is not None and "text" in hosted:
            notes.append(hosted["note"])
            return _ok(
                final_url,
                url,
                hosted["title"] or title,
                hosted["text"],
                limit,
                "text/markdown",
                hosted["reader"],
                notes,
                pdf_path,
            )
        if hosted is not None:
            notes.append(hosted["note"])
        if content.strip():
            return _ok(final_url, url, title, content, limit, ctype, "local", notes, pdf_path)
        detail = why if not pdf_path else "PDF saved; read it with view_pdf"
        result = {
            "ok": False,
            "url": final_url,
            "requested_url": url,
            "status": status or None,
            "error": note("fetch_failed", detail=detail).as_dict(),
            "notes": [n.as_dict() for n in notes],
        }
        if pdf_path:
            result["pdf_path"] = pdf_path
        return result
    if not 200 <= status < 300:
        return {
            "ok": False,
            "url": final_url,
            "requested_url": url,
            "status": status,
            "error": note("fetch_failed", detail=f"HTTP {status}").as_dict(),
            "notes": [],
        }
    return _ok(final_url, url, title, content, limit, ctype, "local", notes, pdf_path)


def _ok(
    final_url: str,
    requested: str,
    title: str,
    content: str,
    limit: int,
    ctype: str,
    reader: str,
    notes: list[SearchNote],
    pdf_path: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ok": True,
        "status": 200,
        "url": final_url,
        "requested_url": requested,
        "title": title,
        "content": content[:limit],
        "content_type": ctype or "text/html",
        "chars": len(content),
        "truncated": len(content) > limit,
        "reader": reader,
        "notes": [n.as_dict() for n in notes],
    }
    if pdf_path:
        row["pdf_path"] = pdf_path
    return row


__all__ = ["MAX_CHARS", "html_to_markdown", "web_fetch"]
