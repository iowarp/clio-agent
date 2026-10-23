"""Long-term memory documents: the one parser and serializer (owner module).

A memory document is Markdown about one subject: a frontmatter block, then one fact
per line, each with a trailing annotation the server stamps::

    ---
    name: frontier-allocation
    description: The user's OLCF Frontier allocation, project id and scheduler quirks
    category: environments
    aliases: [frontier, olcf]
    updated: 2026-11-04T15:02:11Z
    ---
    - Project id is CSC123 {id=f3k9, source=user, session=sess_ab12, at=2026-10-30}
    - ~~Uses the debug QOS~~ {id=f2a0, source=user, session=sess_ef56, at=2026-09-12, superseded_by=f3m1}

A superseded fact stays in the document, struck through, and leaves the search text.
The ``profile`` document of a scope also carries ``forgotten: [ids]``, the facts the
user asked to forget, so the background pass never proposes them again.

The format is canonical: :func:`serialize` of :func:`parse` gives back the same bytes,
and :func:`parse` refuses text that would not, so an imported document either
round-trips exactly or is rejected with :class:`MemoryDocumentError`.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, get_args

from clio_agent.errors import ClioError

Category = Literal["profile", "preferences", "topics", "areas", "people", "environments"]
Source = Literal["user", "inferred", "imported", "tool"]

#: Categories in the order the memory page lists them.
CATEGORIES: tuple[str, ...] = get_args(Category)
SOURCES: tuple[str, ...] = get_args(Source)
#: Categories with one document per scope (no per-subject documents).
SINGLETON_CATEGORIES = ("profile", "preferences")
#: What each category holds, as models are told it (tools, extraction, routing).
CATEGORY_MEANINGS = (
    "profile (who the user is: name, role, institution), "
    "preferences (how they want work done), "
    "topics (subjects they study or ask about), "
    "areas (ongoing efforts: projects, papers, campaigns), "
    "people (colleagues and contacts), "
    "environments (machines, allocations, accounts, software setups)"
)

_FRONTMATTER_KEYS = ("name", "description", "category", "aliases", "updated", "forgotten")
_REQUIRED_KEYS = ("name", "description", "category", "updated")
_ANNOTATION_KEYS = ("id", "source", "session", "message", "at", "superseded_by")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_FACT_ID = re.compile(r"f[0-9a-z]{3,12}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.:+-]+\Z")
_FACT_LINE = re.compile(r"- (?P<body>.*) \{(?P<annotation>id=[^{}]*)\}\Z")
_FENCE = "---"


class MemoryDocumentError(ClioError):
    """A memory document's text is not in the canonical format (typed reason)."""

    def __init__(self, problem: str, *, line: int | None = None) -> None:
        self.problem = problem
        self.line = line
        where = f" (line {line})" if line is not None else ""
        super().__init__(
            f"invalid memory document{where}: {problem}",
            error_type="memory_document_invalid",
            details={"problem": problem, "line": line},
        )


@dataclass(frozen=True)
class Fact:
    """One remembered fact and its provenance."""

    id: str
    text: str
    source: Source
    session: str
    at: str
    message: str = ""
    superseded_by: str = ""

    @property
    def live(self) -> bool:
        """Whether the fact is current (not superseded)."""
        return not self.superseded_by


@dataclass(frozen=True)
class MemoryDocument:
    """A memory document about one subject."""

    name: str
    description: str
    category: Category
    updated: str
    aliases: tuple[str, ...] = ()
    facts: tuple[Fact, ...] = ()
    forgotten: tuple[str, ...] = field(default=())

    @property
    def live_facts(self) -> tuple[Fact, ...]:
        """Current facts, in document order."""
        return tuple(fact for fact in self.facts if fact.live)

    def fact(self, fact_id: str) -> Fact | None:
        """The fact with ``fact_id``, or ``None``."""
        return next((fact for fact in self.facts if fact.id == fact_id), None)


def now_stamp() -> str:
    """The ``updated`` stamp for a change made now (UTC, seconds)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    """The ``at`` date of a fact recorded today (UTC)."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def new_fact_id(taken: set[str] | frozenset[str] = frozenset()) -> str:
    """A fresh fact id not in ``taken``."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    while True:
        candidate = "f" + "".join(secrets.choice(alphabet) for _ in range(6))
        if candidate not in taken:
            return candidate


def check_slug(slug: str) -> str:
    """Return ``slug`` when it is a valid document name, else raise."""
    if not _SLUG.match(slug) or len(slug) > 80:
        raise MemoryDocumentError(f"name {slug!r} must be lowercase words joined by '-'")
    return slug


def _single_line(value: str, what: str) -> str:
    if "\n" in value or "\r" in value:
        raise MemoryDocumentError(f"{what} must be one line")
    if value != value.strip():
        raise MemoryDocumentError(f"{what} must not start or end with spaces")
    return value


def _list_text(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(values) + "]"


def _parse_list(raw: str, what: str, line: int) -> tuple[str, ...]:
    if not (raw.startswith("[") and raw.endswith("]")):
        raise MemoryDocumentError(f"{what} must be a [a, b] list", line=line)
    inner = raw[1:-1]
    if not inner:
        return ()
    items = tuple(inner.split(", "))
    for item in items:
        if not item or "," in item or item != item.strip() or "[" in item or "]" in item:
            raise MemoryDocumentError(f"{what} item {item!r} is not canonical", line=line)
    return items


def _annotation(fact: Fact) -> str:
    values = {
        "id": fact.id,
        "source": fact.source,
        "session": fact.session,
        "message": fact.message,
        "at": fact.at,
        "superseded_by": fact.superseded_by,
    }
    return ", ".join(f"{key}={values[key]}" for key in _ANNOTATION_KEYS if values[key])


def _check_fact(fact: Fact) -> None:
    if not _FACT_ID.match(fact.id):
        raise MemoryDocumentError(f"fact id {fact.id!r} is not a fact id")
    if fact.source not in SOURCES:
        raise MemoryDocumentError(f"fact {fact.id} has unknown source {fact.source!r}")
    for key in ("session", "message", "at", "superseded_by"):
        value = getattr(fact, key)
        if value and not _TOKEN.match(value):
            raise MemoryDocumentError(f"fact {fact.id} {key} {value!r} is not a plain token")
    if not fact.session or not fact.at:
        raise MemoryDocumentError(f"fact {fact.id} needs a session and a date")
    text = _single_line(fact.text, f"fact {fact.id}")
    if not text or "~~" in text:
        raise MemoryDocumentError(f"fact {fact.id} text is empty or contains '~~'")


def serialize(document: MemoryDocument) -> str:
    """The canonical text of ``document``."""
    check_slug(document.name)
    if document.category not in CATEGORIES:
        raise MemoryDocumentError(f"unknown category {document.category!r}")
    lines = [
        _FENCE,
        f"name: {document.name}",
        f"description: {_single_line(document.description, 'description')}",
        f"category: {document.category}",
    ]
    if document.aliases:
        lines.append(f"aliases: {_list_text(document.aliases)}")
    lines.append(f"updated: {_single_line(document.updated, 'updated')}")
    if document.forgotten:
        lines.append(f"forgotten: {_list_text(document.forgotten)}")
    lines.append(_FENCE)
    seen: set[str] = set()
    for fact in document.facts:
        _check_fact(fact)
        if fact.id in seen:
            raise MemoryDocumentError(f"fact id {fact.id} appears twice")
        seen.add(fact.id)
        body = fact.text if fact.live else f"~~{fact.text}~~"
        lines.append(f"- {body} {{{_annotation(fact)}}}")
    return "\n".join(lines) + "\n"


def _parse_annotation(raw: str, line: int) -> dict[str, str]:
    values: dict[str, str] = {}
    order: list[str] = []
    for part in raw.split(", "):
        key, sep, value = part.partition("=")
        if not sep or key not in _ANNOTATION_KEYS or key in values or not value:
            raise MemoryDocumentError(f"annotation part {part!r} is not canonical", line=line)
        values[key] = value
        order.append(key)
    if order != [key for key in _ANNOTATION_KEYS if key in values]:
        raise MemoryDocumentError("annotation keys are out of order", line=line)
    return values


def _parse_fact(text: str, line: int) -> Fact:
    match = _FACT_LINE.match(text)
    if match is None:
        raise MemoryDocumentError("a fact line is '- <fact> {id=..., ...}'", line=line)
    values = _parse_annotation(match["annotation"], line)
    body = match["body"]
    superseded = values.get("superseded_by", "")
    if superseded:
        if not (body.startswith("~~") and body.endswith("~~") and len(body) > 4):
            raise MemoryDocumentError("a superseded fact is struck through", line=line)
        body = body[2:-2]
    if "source" not in values or "session" not in values or "at" not in values:
        raise MemoryDocumentError("a fact needs id, source, session and at", line=line)
    fact = Fact(
        id=values["id"],
        text=body,
        source=values["source"],  # type: ignore[arg-type]  # checked below
        session=values["session"],
        at=values["at"],
        message=values.get("message", ""),
        superseded_by=superseded,
    )
    try:
        _check_fact(fact)
    except MemoryDocumentError as error:
        raise MemoryDocumentError(error.problem, line=line) from None
    return fact


def parse(text: str) -> MemoryDocument:
    """Parse canonical document text.

    Raises:
        MemoryDocumentError: The text is not a canonical memory document.
    """
    if not text.endswith("\n") or "\r" in text:
        raise MemoryDocumentError("a document ends with one newline and uses '\\n' only")
    lines = text[:-1].split("\n")
    if not lines or lines[0] != _FENCE or _FENCE not in lines[1:]:
        raise MemoryDocumentError("a document starts with a '---' frontmatter block")
    end = lines.index(_FENCE, 1)
    meta: dict[str, str] = {}
    keys: list[str] = []
    for number, raw in enumerate(lines[1:end], start=2):
        key, sep, value = raw.partition(": ")
        if not sep or key not in _FRONTMATTER_KEYS or key in meta:
            raise MemoryDocumentError(f"frontmatter line {raw!r} is not canonical", line=number)
        meta[key] = value
        keys.append(key)
    if keys != [key for key in _FRONTMATTER_KEYS if key in meta]:
        raise MemoryDocumentError("frontmatter keys are out of order")
    missing = [key for key in _REQUIRED_KEYS if key not in meta]
    if missing:
        raise MemoryDocumentError(f"frontmatter is missing {', '.join(missing)}")
    facts = tuple(
        _parse_fact(raw, number) for number, raw in enumerate(lines[end + 1 :], start=end + 2)
    )
    document = MemoryDocument(
        name=meta["name"],
        description=meta["description"],
        category=meta["category"],  # type: ignore[arg-type]  # serialize checks it
        updated=meta["updated"],
        aliases=_parse_list(meta["aliases"], "aliases", 0) if "aliases" in meta else (),
        facts=facts,
        forgotten=_parse_list(meta["forgotten"], "forgotten", 0) if "forgotten" in meta else (),
    )
    if serialize(document) != text:
        raise MemoryDocumentError("the document does not round-trip; write it canonically")
    return document


def search_text(document: MemoryDocument) -> str:
    """What BM25 indexes: name, description, aliases and the live facts only."""
    parts = [document.name.replace("-", " "), document.description, *document.aliases]
    parts.extend(fact.text for fact in document.live_facts)
    return "\n".join(parts)


def with_fact(document: MemoryDocument, fact: Fact, *, updated: str) -> MemoryDocument:
    """``document`` with ``fact`` appended."""
    return replace(document, facts=(*document.facts, fact), updated=updated)


def superseding(
    document: MemoryDocument, fact_id: str, replacement: Fact, *, updated: str
) -> MemoryDocument:
    """``document`` with ``fact_id`` struck through and ``replacement`` after it.

    Raises:
        MemoryDocumentError: ``fact_id`` is not a live fact of the document.
    """
    old = document.fact(fact_id)
    if old is None or not old.live:
        raise MemoryDocumentError(f"fact {fact_id} is not a live fact of {document.name}")
    facts: list[Fact] = []
    for fact in document.facts:
        if fact.id == fact_id:
            facts.extend((replace(fact, superseded_by=replacement.id), replacement))
        else:
            facts.append(fact)
    return replace(document, facts=tuple(facts), updated=updated)


def without_fact(
    document: MemoryDocument, fact_id: str, *, updated: str
) -> tuple[MemoryDocument, tuple[str, ...]]:
    """``document`` with ``fact_id`` and the earlier values it superseded deleted.

    Forget removes lines rather than striking them, and a superseded line is an
    earlier value of the same fact, so forgetting a fact takes its whole chain.
    Returns the document and the removed ids.

    Raises:
        MemoryDocumentError: ``fact_id`` is not in the document.
    """
    if document.fact(fact_id) is None:
        raise MemoryDocumentError(f"fact {fact_id} is not in {document.name}")
    chain = {fact_id}
    grew = True
    while grew:
        earlier = {fact.id for fact in document.facts if fact.superseded_by in chain}
        grew = not earlier <= chain
        chain |= earlier
    removed = tuple(fact.id for fact in document.facts if fact.id in chain)
    facts = tuple(fact for fact in document.facts if fact.id not in chain)
    return replace(document, facts=facts, updated=updated), removed


def forgetting(document: MemoryDocument, fact_ids: tuple[str, ...]) -> MemoryDocument:
    """``document`` (a scope's profile) with ``fact_ids`` added to its forgotten list."""
    merged = tuple(dict.fromkeys((*document.forgotten, *fact_ids)))
    return replace(document, forgotten=merged)


__all__ = [
    "CATEGORIES",
    "SINGLETON_CATEGORIES",
    "SOURCES",
    "Category",
    "Fact",
    "MemoryDocument",
    "MemoryDocumentError",
    "Source",
    "check_slug",
    "forgetting",
    "new_fact_id",
    "now_stamp",
    "parse",
    "search_text",
    "serialize",
    "superseding",
    "today",
    "with_fact",
    "without_fact",
]
