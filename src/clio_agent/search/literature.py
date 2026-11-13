"""``literature_search`` and ``literature_lookup``: scholarly discovery and following.

**Search.** The model writes a keyword query and may name a ``field`` (a topic,
not a source) and a ``kind``. CLIO always asks OpenAlex and Crossref, adds the
sources the declared :data:`COVERAGE` table lists for that field and kind,
queries them together, merges the same work found by several sources (DOI, arXiv
id, PMID; title + year last) and orders works found by more sources first.

**Lookup.** One work by DOI, arXiv id, PMID or OpenAlex id: its metadata, the
best readable URL, its references, the works citing it and its other versions
(preprint ↔ published). OpenAlex single-work lookups are free; OpenCitations and
Crossref answer when OpenAlex's free budget is used up.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from clio_agent.search import broker, http
from clio_agent.search import literature_sources as src
from clio_agent.search.http import SourceError
from clio_agent.search.reasons import SearchNote, note
from clio_agent.search.records import PaperRecord, save_payload
from clio_agent.search.scheduler import (
    LITERATURE_PATIENCE_S,
    SCHEDULER,
    CallEvents,
    caller_id,
    queue_notes,
)
from clio_agent.search.settings import (
    DISPLAY_NAMES,
    LITERATURE_APPROVAL,
    SearchSettings,
    load_search_settings,
)
from clio_agent.search.terms import OPENALEX_COST, terms_of
from clio_agent.search.web import SearchFailure, source_key
from clio_agent.tools.servers.web_limits import SEARCH_LIMITS

FIELDS = ("any", "physical", "life", "chemistry", "earth", "computing", "engineering", "energy")
KINDS = ("any", "article", "preprint", "review", "dataset")
#: Sources every search asks.
ALWAYS = ("openalex", "crossref")
#: Declared coverage: which further sources a field or kind adds. ``field="any"`` (the
#: default) asks every broad source; naming a field narrows the search to its sources.
COVERAGE: dict[str, tuple[str, ...]] = {
    "field:any": ("arxiv", "chemrxiv", "europepmc", "osti"),
    "field:physical": ("arxiv", "osti"),
    "field:chemistry": ("chemrxiv",),
    "field:computing": ("arxiv",),
    "field:engineering": ("osti",),
    "field:energy": ("osti",),
    "field:earth": ("osti",),
    "field:life": ("europepmc",),
    "kind:preprint": ("arxiv", "chemrxiv", "europepmc"),
    "kind:any": ("datacite",),
    "kind:dataset": ("datacite",),
}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def chosen_sources(
    field: str, kind: str, settings: SearchSettings, keyed: dict[str, bool]
) -> list[str]:
    """The sources one search asks, in rank-tiebreak order."""

    wanted = list(ALWAYS)
    for extra in (*COVERAGE.get(f"field:{field}", ()), *COVERAGE.get(f"kind:{kind}", ())):
        if extra not in wanted:
            wanted.append(extra)
    if keyed.get("semantic_scholar"):
        wanted.append("semantic_scholar")
    enabled = set(settings.literature_sources(has_key=keyed))
    return [name for name in wanted if name in enabled]


def _keys(names: tuple[str, ...]) -> dict[str, str]:
    return {name: source_key(name) for name in names}


async def _approved(settings: SearchSettings) -> None:
    if settings.is_approved(LITERATURE_APPROVAL):
        return
    if SEARCH_LIMITS.declined(LITERATURE_APPROVAL):
        raise SearchFailure("search_not_approved", "literature sources", [])
    answer = await broker.gate(broker.approval(LITERATURE_APPROVAL))
    if answer != broker.APPROVE:
        raise SearchFailure("search_not_approved", "literature sources", [])


def _failure_code(kind: str) -> str:
    return (
        "literature_source_budget_used"
        if kind == http.ALLOWANCE_USED
        else "literature_source_failed"
    )


async def _call(
    source: str,
    run: Callable[[], Awaitable[Any]],
    notes: list[SearchNote],
    *,
    cost_usd: float = 0.0,
) -> Any:
    """Ask one source through CLIO's request queue; a failure becomes a note."""

    settings = load_search_settings()
    events = CallEvents()
    try:
        lane = _LANE.get(source, source)
        return await SCHEDULER.call(
            lane,
            run,
            terms=terms_of(lane, settings, has_key=bool(source_key(lane))),
            caller=caller_id(),
            cost_usd=cost_usd,
            patience_s=LITERATURE_PATIENCE_S,
            events=events,
        )
    except SourceError as exc:
        notes.append(
            note(_failure_code(exc.kind), source, exc.detail, advice=_missing(source, exc))
        )
        return None
    finally:
        notes.extend(queue_notes(source, events))


#: Sources reached through another source's API share its request queue and terms.
_LANE = {"chemrxiv": "crossref"}
#: What one search request costs per source (only OpenAlex charges).
_SEARCH_COST = {"openalex": OPENALEX_COST["search"]}


def _missing(source: str, exc: SourceError) -> str:
    """Why ``source`` is missing from these results, and what to do about it."""

    name = DISPLAY_NAMES.get(source, source)
    if exc.kind == http.ALLOWANCE_USED:
        return (
            f"{name}'s free allowance for today is used ({exc.detail}); the other sources "
            f"answered. A free {name} key in Settings → Web search raises it."
        )
    if exc.kind == http.RATE_LIMITED:
        return (
            f"{name} was busy or asked CLIO to slow down ({exc.detail}); the other sources "
            f"answered. Searching again in a minute may include {name}."
        )
    if exc.kind == http.REFUSED:
        return (
            f"{name} refused this search as invalid ({exc.detail}); the other sources "
            f"answered. Simpler keywords may let {name} answer."
        )
    if exc.kind == http.KEY_REJECTED:
        return f"{name} rejected the saved key; fix it in Settings → Web search."
    return (
        f"{name} did not answer ({exc.detail}). That points to a temporary problem at {name}, "
        f"not to it having nothing; the other sources answered. Searching again in a minute "
        f"may include {name}."
    )


def _title_key(record: PaperRecord) -> str:
    return re.sub(r"[^a-z0-9]+", " ", record.title.lower()).strip()


def merge(batches: list[list[PaperRecord]]) -> list[PaperRecord]:
    """Merge the same work across sources and rank works found by more sources first."""

    merged: list[PaperRecord] = []
    by_id: dict[str, PaperRecord] = {}
    for batch in batches:
        for position, record in enumerate(batch):
            record.rank = position
            keys = [
                f"{k}:{v.lower()}"
                for k, v in record.ids.items()
                if k in ("doi", "arxiv", "pmid") and v
            ]
            target = next((by_id[k] for k in keys if k in by_id), None)
            if target is None:
                title = _title_key(record)
                target = next(
                    (
                        other
                        for other in merged
                        if title
                        and _title_key(other) == title
                        and (
                            other.year is None
                            or record.year is None
                            or abs(other.year - record.year) <= 1
                        )
                        and not (other.ids.get("doi") and record.ids.get("doi"))
                    ),
                    None,
                )
            if target is None:
                merged.append(record)
                target = record
            else:
                _absorb(target, record)
            for k in keys:
                by_id[k] = target
    merged.sort(key=lambda r: (-len(r.found_in), r.rank))
    return merged


def _absorb(target: PaperRecord, other: PaperRecord) -> None:
    for name, value in other.ids.items():
        target.ids.setdefault(name, value)
    for name in ("venue", "kind", "abstract", "pdf_url"):
        if not getattr(target, name) and getattr(other, name):
            setattr(target, name, getattr(other, name))
    if not target.authors:
        target.authors = other.authors
    if target.year is None:
        target.year = other.year
    if other.citation_count is not None:
        target.citation_count = max(target.citation_count or 0, other.citation_count)
    for source in other.found_in:
        if source not in target.found_in:
            target.found_in.append(source)
    for version in other.versions:
        if version not in target.versions:
            target.versions.append(version)
    preferred = ("arxiv.org/abs", "europepmc.org/article/PMC")
    if any(p in other.readable_url for p in preferred) or not target.readable_url:
        target.readable_url = other.readable_url
    target.rank = min(target.rank, other.rank)


def check_arguments(query: str, after: str, before: str, field: str, kind: str, limit: int) -> None:
    """Refuse malformed arguments with a message naming the fix."""

    if not query.strip():
        raise ValueError("query must not be empty")
    for name, value in (("after", after), ("before", before)):
        if value and not _DATE.match(value):
            raise ValueError(f"{name} must be a date written YYYY-MM-DD")
    if field not in FIELDS:
        raise ValueError(f"field must be one of {', '.join(FIELDS)}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")


async def literature_search(
    query: str,
    *,
    after: str = "",
    before: str = "",
    field: str = "any",
    kind: str = "any",
    limit: int = 10,
) -> dict[str, Any]:
    """Search the scholarly sources together and return merged works."""

    check_arguments(query, after, before, field, kind, limit)
    settings = load_search_settings()
    if not settings.enabled:
        raise SearchFailure("search_disabled", "", [])
    await _approved(settings)
    keys = _keys(("openalex", "semantic_scholar"))
    keyed = {name: bool(value) for name, value in keys.items()}
    sources = chosen_sources(field, kind, settings, keyed)
    email = settings.contact_email
    per = min(limit, 25)
    runners: dict[str, Callable[[], Awaitable[Any]]] = {
        "openalex": lambda: src.openalex_search(
            query, per, key=keys["openalex"], after=after, before=before, kind=kind
        ),
        "crossref": lambda: src.crossref_search(
            query, per, email=email, after=after, before=before, kind=kind
        ),
        "arxiv": lambda: src.arxiv_search(query, per),
        "chemrxiv": lambda: src.chemrxiv_search(
            query, per, email=email, after=after, before=before
        ),
        "europepmc": lambda: src.europepmc_search(
            query, per, after=after, before=before, kind=kind
        ),
        "osti": lambda: src.osti_search(query, per, after=after, before=before),
        "datacite": lambda: src.datacite_search(query, per),
        "semantic_scholar": lambda: src.semantic_scholar_search(
            query, per, key=keys["semantic_scholar"]
        ),
    }
    notes: list[SearchNote] = []
    batches = await asyncio.gather(
        *(
            _call(name, runners[name], notes, cost_usd=_SEARCH_COST.get(name, 0.0))
            for name in sources
        )
    )
    answered = [name for name, batch in zip(sources, batches, strict=True) if batch is not None]
    if not answered:
        raise SearchFailure("search_all_sources_unavailable", "literature sources", notes)
    works = merge([batch for batch in batches if batch])
    payload = save_payload(
        "literature",
        "-".join(answered),
        {"query": query, "sources": answered, "works": [w.full() for w in works]},
    )
    if not payload:
        notes.append(note("search_payload_not_saved", "literature"))
    row: dict[str, Any] = {
        "ok": True,
        "query": query,
        "sources": answered,
        "works": [w.compact(settings.snippet_chars) for w in works[:limit]],
        "total_found": len(works),
        "notes": [n.as_dict() for n in notes],
    }
    if payload:
        row["payload_file"] = payload
    return row


# --------------------------------------------------------------------- lookup


def parse_identifier(value: str) -> dict[str, str]:
    """``{"doi"|"arxiv"|"pmid"|"openalex": id}`` for a DOI, arXiv id/URL, PMID or W-id."""

    text = value.strip()
    doi = src.norm_doi(text)
    if doi:
        return {"arxiv": src.arxiv_id_of(doi)} if doi.startswith("10.48550") else {"doi": doi}
    lowered = text.lower()
    if lowered.startswith("pmid:") or text.isdigit():
        return {"pmid": text.split(":", 1)[-1]}
    if re.fullmatch(r"(https://openalex\.org/)?W\d+", text, re.IGNORECASE):
        return {"openalex": text.rsplit("/", 1)[-1].upper()}
    arxiv = src.arxiv_id_of(text)
    if arxiv:
        return {"arxiv": arxiv}
    raise ValueError(
        "id must be a DOI, an arXiv id or URL, a PMID (pmid:123) or an OpenAlex id (W123)"
    )


def _openalex_ref(ident: dict[str, str]) -> str:
    if "doi" in ident:
        return f"doi:{ident['doi']}"
    if "arxiv" in ident:
        return f"doi:10.48550/arxiv.{ident['arxiv']}"
    if "pmid" in ident:
        return f"pmid:{ident['pmid']}"
    return ident["openalex"]


async def literature_lookup(
    identifier: str, include: list[str] | None = None, limit: int = 25
) -> dict[str, Any]:
    """One work, its readable URL, and on request its references, citers and versions."""

    wanted = set(include or ["references", "cited_by", "versions"])
    unknown = wanted - {"references", "cited_by", "versions"}
    if unknown:
        raise ValueError("include may list references, cited_by and versions")
    ident = parse_identifier(identifier)
    settings = load_search_settings()
    if not settings.enabled:
        raise SearchFailure("search_disabled", "", [])
    await _approved(settings)
    key = source_key("openalex")
    email = settings.contact_email
    notes: list[SearchNote] = []
    work = await _call("openalex", lambda: src.openalex_work(_openalex_ref(ident), key=key), notes)
    record: PaperRecord | None = src.openalex_record(work) if work else None
    doi = (record.ids.get("doi") if record else "") or ident.get("doi", "")
    crossref = None
    if doi and (record is None or "versions" in wanted or "references" in wanted):
        crossref = await _call("crossref", lambda: src.crossref_work(doi, email=email), notes)
    if record is None and crossref:
        record = src.crossref_record(crossref)
    if record is None and "arxiv" in ident:
        found = await _call("arxiv", lambda: _arxiv_one(ident["arxiv"]), notes)
        record = found[0] if found else None
    if record is None:
        raise SearchFailure(
            "search_all_sources_unavailable", f"no source knows {identifier}", notes
        )
    if crossref:
        for version in src.crossref_record(crossref).versions:
            if version not in record.versions:
                record.versions.append(version)
    row: dict[str, Any] = {"ok": True, "work": record.compact(4000)}
    full: dict[str, Any] = {"work": record.full()}
    if "references" in wanted:
        row["references"], full["references"] = await _references(work, doi, key, limit, notes)
    if "cited_by" in wanted:
        row["cited_by"], full["cited_by"] = await _citers(record, doi, key, limit, notes)
    if "versions" in wanted:
        row["versions"] = record.versions
    payload = save_payload("lookup", "literature", full)
    if payload:
        row["payload_file"] = payload
    row["notes"] = [n.as_dict() for n in notes]
    return row


async def _arxiv_one(arxiv_id: str) -> list[PaperRecord]:
    response = await http.request("arxiv", "GET", src.ARXIV, params={"id_list": arxiv_id})
    return src.arxiv_records(response.text)


def _brief(record: PaperRecord) -> dict[str, Any]:
    row: dict[str, Any] = {"title": record.title, "ids": record.ids}
    if record.year:
        row["year"] = record.year
    if record.readable_url:
        row["readable_url"] = record.readable_url
    return row


async def _references(
    work: Any, doi: str, key: str, limit: int, notes: list[SearchNote]
) -> tuple[list[Any], list[Any]]:
    ids = [str(w).rsplit("/", 1)[-1] for w in (work or {}).get("referenced_works") or []]
    if ids:
        batch = ids[:limit]
        found = await _call(
            "openalex",
            lambda: src.openalex_list("openalex_id:" + "|".join(batch), len(batch), key=key),
            notes,
            cost_usd=OPENALEX_COST["list"],
        )
        if found:
            rows = [_brief(r) for r in found]
            return rows, rows + [{"openalex": w} for w in ids[limit:]]
    if doi:
        dois = await _call("opencitations", lambda: src.opencitations(doi, "references"), notes)
        if dois:
            return [{"ids": {"doi": d}} for d in dois[:limit]], [{"ids": {"doi": d}} for d in dois]
    return [], []


async def _citers(
    record: PaperRecord, doi: str, key: str, limit: int, notes: list[SearchNote]
) -> tuple[list[Any], list[Any]]:
    openalex_id = record.ids.get("openalex", "").rsplit("/", 1)[-1]
    if openalex_id:
        found = await _call(
            "openalex",
            lambda: src.openalex_list(f"cites:{openalex_id}", limit, key=key),
            notes,
            cost_usd=OPENALEX_COST["list"],
        )
        if found:
            rows = [_brief(r) for r in found]
            return rows, rows
    if doi:
        dois = await _call("opencitations", lambda: src.opencitations(doi, "citations"), notes)
        if dois:
            return [{"ids": {"doi": d}} for d in dois[:limit]], [{"ids": {"doi": d}} for d in dois]
    return [], []


__all__ = [
    "ALWAYS",
    "COVERAGE",
    "FIELDS",
    "KINDS",
    "chosen_sources",
    "literature_lookup",
    "literature_search",
    "merge",
    "parse_identifier",
]
