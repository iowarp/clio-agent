"""Scholarly sources, one adapter each, normalized to :class:`PaperRecord`.

Every source here answers without a key (Semantic Scholar only with the person's
key). Field paths are pinned by recorded fixtures in ``tests/test_search/fixtures``.
The ``readable_url`` each record carries is where its text can be read with
``web_fetch``: an arXiv abstract page, PMC/Europe PMC full text, an open-access
landing page, an OSTI full-text link, else the DOI resolver.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any

from clio_agent.search import http
from clio_agent.search.http import SourceError
from clio_agent.search.records import PaperRecord

OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"
ARXIV = "https://export.arxiv.org/api/query"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OSTI = "https://www.osti.gov/api/v1/records"
DATACITE = "https://api.datacite.org/dois"
OPENCITATIONS = "https://api.opencitations.net/index/v2"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper/search"

_TAGS = re.compile(r"<[^>]+>")
_ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$")
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)


def plain(text: Any) -> str:
    """Markup-free text: JATS/HTML tags removed and entities decoded."""

    return html.unescape(_TAGS.sub(" ", str(text or ""))).strip()


def norm_doi(value: Any) -> str:
    """A bare lower-case DOI (``10.x/...``) from a DOI or DOI URL, else ``""``."""

    text = str(value or "").strip()
    lowered = text.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.lower() if text.startswith("10.") else ""


def arxiv_id_of(value: Any) -> str:
    """The arXiv identifier (no version) in an arXiv URL, id or 10.48550 DOI."""

    text = str(value or "").strip()
    doi = _ARXIV_DOI.match(text)
    if doi:
        text = doi.group(1)
    for marker in ("/abs/", "/pdf/", "/html/"):
        if marker in text:
            text = text.split(marker, 1)[1]
    text = text.removesuffix(".pdf")
    match = _ARXIV_ID.search(text)
    return match.group(1) if match else ""


def doi_url(doi: str) -> str:
    """The resolver URL for ``doi``."""

    return f"https://doi.org/{doi}" if doi else ""


def _year(value: Any) -> int | None:
    match = re.match(r"(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def _ids(**values: str) -> dict[str, str]:
    return {name: value for name, value in values.items() if value}


# ------------------------------------------------------------------- OpenAlex


def _openalex_abstract(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    slots: dict[int, str] = {}
    for word, places in index.items():
        for place in places if isinstance(places, list) else []:
            slots[int(place)] = str(word)
    return " ".join(slots[i] for i in sorted(slots))


def openalex_record(row: dict[str, Any]) -> PaperRecord:
    """One OpenAlex work as a record."""

    ids = row.get("ids") if isinstance(row.get("ids"), dict) else {}
    doi = norm_doi(row.get("doi") or ids.get("doi"))
    arxiv = arxiv_id_of(doi) if doi.startswith("10.48550") else ""
    pmid = str(ids.get("pmid") or "").rsplit("/", 1)[-1]
    pmcid = str(ids.get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1]
    primary = row.get("primary_location") if isinstance(row.get("primary_location"), dict) else {}
    best = row.get("best_oa_location") if isinstance(row.get("best_oa_location"), dict) else {}
    for location in row.get("locations") or []:
        if isinstance(location, dict) and not arxiv:
            arxiv = arxiv_id_of(location.get("landing_page_url"))
    source = primary.get("source") if isinstance(primary.get("source"), dict) else {}
    authors = [
        str(a.get("author", {}).get("display_name"))
        for a in row.get("authorships") or []
        if isinstance(a, dict) and isinstance(a.get("author"), dict)
    ]
    record = PaperRecord(
        title=plain(row.get("display_name") or row.get("title")),
        ids=_ids(doi=doi, arxiv=arxiv, pmid=pmid, pmcid=pmcid, openalex=str(row.get("id") or "")),
        authors=authors,
        year=row.get("publication_year") if isinstance(row.get("publication_year"), int) else None,
        venue=str(source.get("display_name") or ""),
        kind=str(row.get("type") or ""),
        abstract=_openalex_abstract(row.get("abstract_inverted_index")),
        pdf_url=str(best.get("pdf_url") or primary.get("pdf_url") or ""),
        citation_count=row.get("cited_by_count")
        if isinstance(row.get("cited_by_count"), int)
        else None,
        found_in=["openalex"],
    )
    record.readable_url = readable_url(record, oa_landing=str(best.get("landing_page_url") or ""))
    return record


def readable_url(record: PaperRecord, *, oa_landing: str = "") -> str:
    """Where the work's text can be read: arXiv, PMC, open-access page, else DOI."""

    if record.ids.get("arxiv"):
        return f"https://arxiv.org/abs/{record.ids['arxiv']}"
    if record.ids.get("pmcid"):
        return f"https://europepmc.org/article/PMC/{record.ids['pmcid']}"
    if oa_landing:
        return oa_landing
    return doi_url(record.ids.get("doi", ""))


async def openalex_search(
    query: str, limit: int, *, key: str, after: str, before: str, kind: str
) -> list[PaperRecord]:
    """OpenAlex works matching ``query`` (search costs from the per-IP daily budget)."""

    filters = []
    if after:
        filters.append(f"from_publication_date:{after}")
    if before:
        filters.append(f"to_publication_date:{before}")
    if kind == "preprint":
        filters.append("type:preprint")
    elif kind == "review":
        filters.append("type:review")
    elif kind == "dataset":
        filters.append("type:dataset")
    params: dict[str, Any] = {"search": query, "per_page": min(limit, 50)}
    if filters:
        params["filter"] = ",".join(filters)
    if key:
        params["api_key"] = key
    response = await http.request(
        "openalex", "GET", f"{OPENALEX}/works", keyed=bool(key), params=params
    )
    data = http.json_body("openalex", response)
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceError("openalex", http.FAILED, "no results list")
    return [openalex_record(row) for row in rows if isinstance(row, dict)]


async def openalex_work(ref: str, *, key: str) -> dict[str, Any]:
    """One OpenAlex work by ``doi:…``, ``pmid:…`` or ``W…`` (singleton lookups are free)."""

    params = {"api_key": key} if key else {}
    response = await http.request(
        "openalex", "GET", f"{OPENALEX}/works/{ref}", keyed=bool(key), params=params
    )
    data = http.json_body("openalex", response)
    if not isinstance(data, dict):
        raise SourceError("openalex", http.FAILED, "work is not an object")
    return data


async def openalex_list(filter_expr: str, limit: int, *, key: str) -> list[PaperRecord]:
    """OpenAlex works for a filter (``cites:W…`` or ``openalex_id:W1|W2``)."""

    params: dict[str, Any] = {"filter": filter_expr, "per_page": min(limit, 50)}
    if key:
        params["api_key"] = key
    response = await http.request(
        "openalex", "GET", f"{OPENALEX}/works", keyed=bool(key), params=params
    )
    data = http.json_body("openalex", response)
    rows = data.get("results") if isinstance(data, dict) else []
    return [openalex_record(row) for row in rows if isinstance(row, dict)]


# ------------------------------------------------------------------- Crossref


def crossref_record(row: dict[str, Any]) -> PaperRecord:
    """One Crossref work as a record (``relation`` becomes ``versions``)."""

    doi = norm_doi(row.get("DOI"))
    issued = (
        row.get("issued", {}).get("date-parts") if isinstance(row.get("issued"), dict) else None
    )
    year = issued[0][0] if issued and issued[0] and isinstance(issued[0][0], int) else None
    titles = row.get("title") or [""]
    venues = row.get("container-title") or [""]
    authors = [
        " ".join(filter(None, (a.get("given"), a.get("family")))) or str(a.get("name") or "")
        for a in row.get("author") or []
        if isinstance(a, dict)
    ]
    versions = []
    relation = row.get("relation") if isinstance(row.get("relation"), dict) else {}
    for name in ("is-preprint-of", "has-preprint", "is-version-of", "has-version"):
        for item in relation.get(name) or []:
            if isinstance(item, dict) and item.get("id-type") == "doi":
                versions.append({"relation": name, "doi": norm_doi(item.get("id"))})
    pdf = next(
        (
            str(link.get("URL"))
            for link in row.get("link") or []
            if isinstance(link, dict) and "pdf" in str(link.get("content-type", ""))
        ),
        "",
    )
    kind = str(row.get("subtype") or row.get("type") or "")
    record = PaperRecord(
        title=plain(titles[0] if titles else ""),
        ids=_ids(doi=doi, arxiv=arxiv_id_of(doi) if doi.startswith("10.48550") else ""),
        authors=authors,
        year=year,
        venue=plain(venues[0] if venues else ""),
        kind=kind,
        abstract=plain(row.get("abstract")),
        pdf_url=pdf,
        citation_count=row.get("is-referenced-by-count")
        if isinstance(row.get("is-referenced-by-count"), int)
        else None,
        found_in=["crossref"],
        versions=versions,
    )
    record.readable_url = readable_url(record)
    return record


def _crossref_params(email: str) -> dict[str, str]:
    return {"mailto": email} if email else {}


async def crossref_search(
    query: str, limit: int, *, email: str, after: str, before: str, kind: str
) -> list[PaperRecord]:
    """Crossref works matching ``query`` (the polite pool when an email is configured)."""

    filters = []
    if after:
        filters.append(f"from-pub-date:{after}")
    if before:
        filters.append(f"until-pub-date:{before}")
    if kind == "preprint":
        filters.append("type:posted-content")
    params: dict[str, Any] = {"query": query, "rows": min(limit, 50), **_crossref_params(email)}
    if filters:
        params["filter"] = ",".join(filters)
    response = await http.request("crossref", "GET", f"{CROSSREF}/works", params=params)
    data = http.json_body("crossref", response)
    items = data.get("message", {}).get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SourceError("crossref", http.FAILED, "no items list")
    return [crossref_record(row) for row in items if isinstance(row, dict)]


async def crossref_work(doi: str, *, email: str) -> dict[str, Any]:
    """The raw Crossref message for one DOI (references and relations included)."""

    response = await http.request(
        "crossref", "GET", f"{CROSSREF}/works/{doi}", params=_crossref_params(email)
    )
    data = http.json_body("crossref", response)
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        raise SourceError("crossref", http.FAILED, "no message")
    return message


# ---------------------------------------------------------------------- arXiv

_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def arxiv_records(text: str) -> list[PaperRecord]:
    """Records from an arXiv Atom feed."""

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SourceError("arxiv", http.FAILED, "feed is not XML") from exc
    records = []
    for entry in root.findall("a:entry", _ATOM):
        ident = arxiv_id_of(entry.findtext("a:id", default="", namespaces=_ATOM))
        if not ident:
            continue
        doi = norm_doi(entry.findtext("arxiv:doi", default="", namespaces=_ATOM))
        record = PaperRecord(
            title=plain(entry.findtext("a:title", default="", namespaces=_ATOM)),
            ids=_ids(arxiv=ident, doi=doi),
            authors=[
                plain(a.findtext("a:name", default="", namespaces=_ATOM))
                for a in entry.findall("a:author", _ATOM)
            ],
            year=_year(entry.findtext("a:published", default="", namespaces=_ATOM)),
            venue=plain(entry.findtext("arxiv:journal_ref", default="", namespaces=_ATOM))
            or "arXiv",
            kind="preprint",
            abstract=plain(entry.findtext("a:summary", default="", namespaces=_ATOM)),
            pdf_url=f"https://arxiv.org/pdf/{ident}",
            found_in=["arxiv"],
        )
        record.readable_url = readable_url(record)
        records.append(record)
    return records


async def arxiv_search(query: str, limit: int) -> list[PaperRecord]:
    """arXiv preprints with every query word in any field."""

    words = [w for w in re.findall(r"[\w\-]+", query) if len(w) > 1][:8]
    expr = " AND ".join(f"all:{w}" for w in words) or f"all:{query}"
    params = {"search_query": expr, "max_results": min(limit, 50)}
    response = await http.request("arxiv", "GET", ARXIV, params=params)
    return arxiv_records(response.text)


# ----------------------------------------------------------------- Europe PMC


def europepmc_record(row: dict[str, Any]) -> PaperRecord:
    """One Europe PMC result (``resultType=core``) as a record."""

    journal = (
        row.get("journalInfo", {}).get("journal", {})
        if isinstance(row.get("journalInfo"), dict)
        else {}
    )
    record = PaperRecord(
        title=plain(row.get("title")),
        ids=_ids(
            doi=norm_doi(row.get("doi")),
            pmid=str(row.get("pmid") or ""),
            pmcid=str(row.get("pmcid") or ""),
        ),
        authors=[
            s.strip()
            for s in str(row.get("authorString") or "").rstrip(".").split(",")
            if s.strip()
        ],
        year=_year(row.get("pubYear") or row.get("firstPublicationDate")),
        venue=str(journal.get("title") or row.get("journalTitle") or ""),
        kind="preprint" if row.get("source") == "PPR" else "article",
        abstract=plain(row.get("abstractText")),
        citation_count=row.get("citedByCount")
        if isinstance(row.get("citedByCount"), int)
        else None,
        found_in=["europepmc"],
    )
    record.readable_url = readable_url(record)
    return record


async def europepmc_search(
    query: str, limit: int, *, after: str, before: str, kind: str
) -> list[PaperRecord]:
    """Europe PMC (life sciences, incl. preprints) matching ``query``."""

    expr = query
    if after or before:
        expr += f" AND FIRST_PDATE:[{after or '1900-01-01'} TO {before or '2100-12-31'}]"
    if kind == "preprint":
        expr += " AND SRC:PPR"
    params = {"query": expr, "format": "json", "pageSize": min(limit, 50), "resultType": "core"}
    response = await http.request("europepmc", "GET", EUROPEPMC, params=params)
    data = http.json_body("europepmc", response)
    rows = data.get("resultList", {}).get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceError("europepmc", http.FAILED, "no result list")
    return [europepmc_record(row) for row in rows if isinstance(row, dict)]


# ----------------------------------------------------------------------- OSTI


def osti_record(row: dict[str, Any]) -> PaperRecord:
    """One OSTI.gov record (DOE reports and papers) as a record."""

    links = {
        str(link.get("rel")): str(link.get("href"))
        for link in row.get("links") or []
        if isinstance(link, dict)
    }
    authors = [re.sub(r"\s*\[.*$", "", str(a)).strip() for a in row.get("authors") or []]
    record = PaperRecord(
        title=plain(row.get("title")),
        ids=_ids(doi=norm_doi(row.get("doi")), osti=str(row.get("osti_id") or "")),
        authors=[a for a in authors if a],
        year=_year(row.get("publication_date")),
        venue=str(row.get("journal_name") or "OSTI.gov"),
        kind=str(row.get("product_type") or ""),
        abstract=plain(row.get("description")),
        pdf_url=links.get("fulltext", ""),
        found_in=["osti"],
    )
    record.readable_url = links.get("citation") or readable_url(record)
    return record


async def osti_search(query: str, limit: int, *, after: str, before: str) -> list[PaperRecord]:
    """OSTI.gov records (DOE-funded research) matching ``query``."""

    params: dict[str, Any] = {"q": query, "rows": min(limit, 50)}
    if after:
        params["publication_date_start"] = after.replace("-", "/")
    if before:
        params["publication_date_end"] = before.replace("-", "/")
    response = await http.request(
        "osti", "GET", OSTI, params=params, headers={"accept": "application/json"}
    )
    data = http.json_body("osti", response)
    if not isinstance(data, list):
        raise SourceError("osti", http.FAILED, "answer is not a list")
    return [osti_record(row) for row in data if isinstance(row, dict)]


# ------------------------------------------------------------------- DataCite


def datacite_record(row: dict[str, Any]) -> PaperRecord:
    """One DataCite DOI (datasets, software) as a record."""

    attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else row
    titles = attrs.get("titles") or [{}]
    descriptions = attrs.get("descriptions") or [{}]
    types = attrs.get("types") if isinstance(attrs.get("types"), dict) else {}
    doi = norm_doi(attrs.get("doi"))
    record = PaperRecord(
        title=plain(titles[0].get("title") if titles and isinstance(titles[0], dict) else ""),
        ids=_ids(doi=doi),
        authors=[str(c.get("name")) for c in attrs.get("creators") or [] if isinstance(c, dict)],
        year=attrs.get("publicationYear")
        if isinstance(attrs.get("publicationYear"), int)
        else None,
        venue=str(attrs.get("publisher") or ""),
        kind=str(types.get("resourceTypeGeneral") or "dataset").lower(),
        abstract=plain(
            descriptions[0].get("description")
            if descriptions and isinstance(descriptions[0], dict)
            else ""
        ),
        found_in=["datacite"],
    )
    record.readable_url = str(attrs.get("url") or doi_url(doi))
    return record


async def datacite_search(query: str, limit: int) -> list[PaperRecord]:
    """DataCite DOIs (datasets and software) matching ``query``."""

    params = {"query": query, "page[size]": min(limit, 50)}
    response = await http.request("datacite", "GET", DATACITE, params=params)
    data = http.json_body("datacite", response)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceError("datacite", http.FAILED, "no data list")
    return [datacite_record(row) for row in rows if isinstance(row, dict)]


# ------------------------------------------------------------- Semantic Scholar


async def semantic_scholar_search(query: str, limit: int, *, key: str) -> list[PaperRecord]:
    """Semantic Scholar papers (only with the person's key)."""

    fields = "title,year,externalIds,venue,authors,abstract,citationCount,openAccessPdf"
    response = await http.request(
        "semantic_scholar", "GET", SEMANTIC_SCHOLAR, keyed=True,
        params={"query": query, "limit": min(limit, 50), "fields": fields},
        headers={"x-api-key": key},
    )  # fmt: skip
    data = http.json_body("semantic_scholar", response)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceError("semantic_scholar", http.FAILED, "no data list")
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ext = row.get("externalIds") if isinstance(row.get("externalIds"), dict) else {}
        pdf = row.get("openAccessPdf") if isinstance(row.get("openAccessPdf"), dict) else {}
        record = PaperRecord(
            title=plain(row.get("title")),
            ids=_ids(
                doi=norm_doi(ext.get("DOI")),
                arxiv=str(ext.get("ArXiv") or ""),
                pmid=str(ext.get("PubMed") or ""),
            ),
            authors=[str(a.get("name")) for a in row.get("authors") or [] if isinstance(a, dict)],
            year=row.get("year") if isinstance(row.get("year"), int) else None,
            venue=str(row.get("venue") or ""),
            abstract=plain(row.get("abstract")),
            pdf_url=str(pdf.get("url") or ""),
            citation_count=row.get("citationCount")
            if isinstance(row.get("citationCount"), int)
            else None,
            found_in=["semantic_scholar"],
        )
        record.readable_url = readable_url(record)
        records.append(record)
    return records


# --------------------------------------------------------------- OpenCitations


def opencitations_dois(rows: Any, side: str) -> list[str]:
    """The DOIs on one side (``citing``/``cited``) of OpenCitations index rows."""

    found: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        for token in str(row.get(side) or "").split() if isinstance(row, dict) else []:
            if token.startswith("doi:"):
                doi = norm_doi(token[4:])
                if doi and doi not in found:
                    found.append(doi)
    return found


async def opencitations(doi: str, relation: str) -> list[str]:
    """DOIs a work cites (``references``) or that cite it (``citations``)."""

    response = await http.request("opencitations", "GET", f"{OPENCITATIONS}/{relation}/doi:{doi}")
    rows = http.json_body("opencitations", response)
    return opencitations_dois(rows, "cited" if relation == "references" else "citing")


__all__ = [
    "arxiv_id_of",
    "arxiv_records",
    "arxiv_search",
    "crossref_record",
    "crossref_search",
    "crossref_work",
    "datacite_record",
    "datacite_search",
    "doi_url",
    "europepmc_record",
    "europepmc_search",
    "norm_doi",
    "openalex_list",
    "openalex_record",
    "openalex_search",
    "openalex_work",
    "opencitations",
    "opencitations_dois",
    "osti_record",
    "osti_search",
    "plain",
    "readable_url",
    "semantic_scholar_search",
]
