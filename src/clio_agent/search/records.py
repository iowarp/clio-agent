"""Normalized search records, their compact in-context form and the payload file.

A source's full answer can be tens of thousands of characters. The model gets a
compact list (title, URL, date, a short snippet per item) and the path of a file
holding everything the source returned, which it can read when it wants more.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from clio_agent.runtime import trace

_SPACE = re.compile(r"\s+")


def clip(text: str, limit: int) -> str:
    """``text`` with whitespace collapsed, cut to ``limit`` characters at a word."""

    flat = _SPACE.sub(" ", text or "").strip()
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0]
    return f"{cut}…"


def host_of(url: str) -> str:
    """The lower-cased host of ``url`` without a leading ``www.``."""

    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def domain_matches(url: str, domains: list[str]) -> bool:
    """Whether ``url``'s host is one of ``domains`` or a subdomain of one."""

    host = host_of(url)
    return any(
        host == d or host.endswith("." + d) for d in (x.lower().lstrip(".") for x in domains)
    )


@dataclass
class WebResult:
    """One web search hit, normalized across sources."""

    title: str
    url: str
    date: str = ""
    snippet: str = ""
    text: str = ""

    def compact(self, snippet_chars: int) -> dict[str, Any]:
        """The in-context form: no full text, a bounded snippet."""

        row: dict[str, Any] = {"title": clip(self.title, 200), "url": self.url}
        if self.date:
            row["date"] = self.date[:10]
        snippet = self.snippet or self.text
        if snippet:
            row["snippet"] = clip(snippet, snippet_chars)
        return row


@dataclass
class PaperRecord:
    """One scholarly work, merged across the literature sources that returned it."""

    title: str
    ids: dict[str, str] = field(default_factory=dict)
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    kind: str = ""
    abstract: str = ""
    readable_url: str = ""
    pdf_url: str = ""
    citation_count: int | None = None
    found_in: list[str] = field(default_factory=list)
    versions: list[dict[str, str]] = field(default_factory=list)
    rank: int = 0

    def compact(self, snippet_chars: int) -> dict[str, Any]:
        """The in-context form: identity, a short abstract and where to read it."""

        row: dict[str, Any] = {"title": clip(self.title, 300), "ids": dict(self.ids)}
        if self.authors:
            names = self.authors[:5]
            row["authors"] = names + (["et al."] if len(self.authors) > 5 else [])
        for key in ("year", "venue", "kind", "citation_count"):
            value = getattr(self, key)
            if value not in (None, ""):
                row[key] = value
        if self.abstract:
            row["abstract"] = clip(self.abstract, snippet_chars)
        if self.readable_url:
            row["readable_url"] = self.readable_url
        if self.pdf_url and self.pdf_url != self.readable_url:
            row["pdf_url"] = self.pdf_url
        row["found_in"] = list(self.found_in)
        if self.versions:
            row["versions"] = list(self.versions)
        return row

    def full(self) -> dict[str, Any]:
        """Everything known about the work (for the payload file)."""

        return asdict(self)


def payload_directory() -> Path:
    """Where this call's full source answers are saved (the session's tool output)."""

    from clio_agent.tools.servers.shell_spill_store import (  # noqa: PLC0415 - leaf tools
        active_session_id,
        spill_directory,
        spill_root,
    )

    return spill_directory(spill_root(Path.cwd()), session_id=active_session_id()) / "search"


def save_payload(kind: str, source: str, payload: Any, *, directory: Path | None = None) -> str:
    """Write ``payload`` as JSON and return its path, or ``""`` when it cannot be saved.

    The caller attaches the typed ``search_payload_not_saved`` note when the path
    is empty; the compact result is still complete without the file.
    """

    stamp = time.strftime("%Y%m%dT%H%M%S")
    name = f"{stamp}-{kind}-{source}-{uuid.uuid4().hex[:8]}.json"
    try:
        target = (directory or payload_directory()) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
    except OSError as exc:
        trace.event("SEARCH", "search_payload_not_saved %s", exc)
        return ""
    return str(target)


__all__ = [
    "PaperRecord",
    "WebResult",
    "clip",
    "domain_matches",
    "host_of",
    "payload_directory",
    "save_payload",
]
