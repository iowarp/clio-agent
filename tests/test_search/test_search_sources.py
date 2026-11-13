"""Source adapters parse their recorded answers; failures classify by status only."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from clio_agent.search import http, literature_sources, web_sources
from clio_agent.search.http import SourceError
from clio_agent.search.literature import chosen_sources, merge, parse_identifier
from clio_agent.search.records import PaperRecord, WebResult
from clio_agent.search.settings import (
    SearchConfigurationError,
    build_settings,
)
from clio_agent.search.web_sources import SourceAnswer, WebQuery, post_filter
from tests.test_search._fakes import fixture, install, response_of

# ------------------------------------------------------------------ settings


def test_default_order_puts_filtering_sources_first() -> None:
    settings = build_settings()
    assert settings.web_sources() == ("keenable", "exa", "firecrawl", "you", "parallel")


def test_partial_order_keeps_unlisted_sources_after_listed_ones() -> None:
    settings = build_settings(web_order=["firecrawl"], web_disabled=["parallel"])
    assert settings.web_sources() == ("firecrawl", "keenable", "exa", "you")


def test_service_url_goes_first_and_needs_no_approval() -> None:
    settings = build_settings(service_url="https://search.example.org/")
    assert settings.web_sources()[0] == "service"
    assert settings.service_url == "https://search.example.org"
    assert settings.is_approved("service")
    assert not settings.is_approved("exa")


def test_unknown_source_is_a_typed_configuration_error() -> None:
    with pytest.raises(SearchConfigurationError, match="unknown source 'searxng'"):
        build_settings(web_order=["searxng"])


def test_key_only_literature_source_needs_its_key() -> None:
    settings = build_settings()
    assert "semantic_scholar" not in settings.literature_sources()
    assert "semantic_scholar" in settings.literature_sources(has_key={"semantic_scholar": True})


# ---------------------------------------------------------------- classify


def _resp(status: int, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers=headers)


def test_short_retry_after_is_a_rate_limit_long_one_is_a_used_allowance() -> None:
    assert (
        http.classify("x", _resp(429, **{"retry-after": "30"}), keyed=False).kind
        == http.RATE_LIMITED
    )
    used = http.classify("x", _resp(429, **{"retry-after": "14413"}), keyed=False)
    assert used.kind == http.ALLOWANCE_USED
    assert used.cooldown_s() == pytest.approx(14413)
    assert http.classify("x", _resp(402), keyed=False).kind == http.ALLOWANCE_USED
    assert http.classify("x", _resp(401), keyed=True).kind == http.KEY_REJECTED
    assert http.classify("x", _resp(503), keyed=False).kind == http.FAILED
    assert http.classify("x", _resp(200), keyed=False) is None


def test_recorded_rate_limits_classify_as_used_allowances(monkeypatch) -> None:
    for api, case in (
        ("exa", "rate_limited"),
        ("firecrawl", "rate_limited"),
        ("openalex", "budget_exhausted"),
    ):
        failure = http.classify(api, response_of(fixture(api, case)), keyed=False)
        assert failure is not None and failure.kind == http.ALLOWANCE_USED, api


def test_mcp_minus_32000_with_http_200_rests_the_source(monkeypatch) -> None:
    recorded = fixture("parallel", "rate_limited_search")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            return response_of(recorded)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}})

    install(monkeypatch, handler)
    with pytest.raises(SourceError) as caught:
        asyncio.run(web_sources.search("parallel", WebQuery(query="adios2")))
    assert caught.value.kind == http.ALLOWANCE_USED


# ------------------------------------------------------------- web adapters


def test_keenable_parses_results_and_applies_its_filters(monkeypatch) -> None:
    seen = install(monkeypatch, lambda request: response_of(fixture("keenable", "search_ok")))
    query = WebQuery(query="ADIOS2 BP5", after="2025-01-01", include_domains=["github.com"])
    answer = asyncio.run(web_sources.search("keenable", query))
    assert answer.results and answer.results[0].url.startswith("https://github.com/")
    assert "BP5" in answer.results[0].snippet
    assert answer.applied == {"after", "include_domains"}
    sent = json.loads(seen[0].content)
    assert sent["published_after"] == "2025-01-01" and sent["site"] == "github.com"
    assert seen[0].headers["X-Keenable-Title"] == "CLIO"
    assert str(seen[0].url).endswith("/v1/search/public")


def test_exa_parses_the_json_text_inside_the_mcp_result(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("method") == "tools/call":
            return response_of(fixture("exa", "search_ok"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}})

    install(monkeypatch, handler)
    answer = asyncio.run(web_sources.search("exa", WebQuery(query="adios2", kind="research")))
    assert answer.results[0].url == "https://adios2.readthedocs.io/en/latest/engines/engines.html"
    assert "kind" in answer.applied


def test_post_filter_applies_what_the_source_did_not() -> None:
    answer = SourceAnswer(
        results=[
            WebResult("a", "https://arxiv.org/abs/1", date="2024-01-01"),
            WebResult("b", "https://arxiv.org/abs/2", date="2026-03-01"),
            WebResult("c", "https://example.com/x", date="2026-03-01"),
            WebResult("d", "https://export.arxiv.org/y"),
        ],
        raw={},
    )
    query = WebQuery(query="q", after="2025-01-01", include_domains=["arxiv.org"])
    kept, by_clio = post_filter(answer, query)
    assert [r.title for r in kept] == ["b", "d"]
    assert by_clio == ["after", "include_domains"]


def test_compact_result_bounds_the_snippet() -> None:
    row = WebResult("t", "https://x.org", snippet="word " * 400).compact(120)
    assert len(row["snippet"]) <= 121 and "text" not in row


# ------------------------------------------------------- literature adapters


def test_crossref_preprint_relation_becomes_a_version() -> None:
    record = literature_sources.crossref_record(
        fixture("crossref", "preprint_relation")["body"]["message"]
    )
    assert record.kind == "preprint"
    assert {"relation": "is-preprint-of", "doi": "10.1002/anie.6921317"} in record.versions
    assert record.readable_url.startswith("https://doi.org/10.26434/")


def test_arxiv_feed_records_point_to_readable_abstract_pages() -> None:
    records = literature_sources.arxiv_records(fixture("arxiv", "search_ok")["body"])
    assert records and all(r.found_in == ["arxiv"] for r in records)
    first = records[0]
    assert first.ids["arxiv"] == "2209.04847"
    assert first.readable_url == "https://arxiv.org/abs/2209.04847"
    assert first.pdf_url == "https://arxiv.org/pdf/2209.04847"


def test_europepmc_osti_datacite_and_opencitations_parse() -> None:
    epmc = [
        literature_sources.europepmc_record(r)
        for r in fixture("europepmc", "search_ok")["body"]["resultList"]["result"]
    ]
    assert epmc and all(r.title and "&lt;" not in r.title for r in epmc)
    osti = [literature_sources.osti_record(r) for r in fixture("osti", "search_ok")["body"]]
    assert osti[0].authors[0] == "Reshniak, Viktor"
    assert osti[0].readable_url.startswith("https://www.osti.gov/biblio/")
    data = [
        literature_sources.datacite_record(r)
        for r in fixture("datacite", "search_ok")["body"]["data"]
    ]
    assert data and all(r.ids.get("doi") for r in data)
    refs = literature_sources.opencitations_dois(
        fixture("opencitations", "references")["body"], "cited"
    )
    assert refs and all(d.startswith("10.") for d in refs)


def test_merge_joins_the_same_work_and_ranks_multi_source_first() -> None:
    a = PaperRecord("Lossy compression", ids={"doi": "10.1/x"}, found_in=["openalex"])
    b = PaperRecord("Other", ids={"doi": "10.1/y"}, found_in=["openalex"])
    c = PaperRecord(
        "Lossy Compression", ids={"doi": "10.1/X"}, citation_count=4, found_in=["crossref"]
    )
    d = PaperRecord(
        "Lossy compression!", ids={"arxiv": "2201.08228"}, year=2022, found_in=["arxiv"]
    )
    merged = merge([[b, a], [c], [d]])
    assert merged[0].found_in == ["openalex", "crossref", "arxiv"]
    assert merged[0].citation_count == 4 and merged[0].ids["arxiv"] == "2201.08228"
    assert [m.title for m in merged] == ["Lossy compression", "Other"]


def test_coverage_table_adds_field_and_kind_sources() -> None:
    settings = build_settings()
    assert chosen_sources("any", "any", settings, {}) == [
        "openalex",
        "crossref",
        "arxiv",
        "europepmc",
        "osti",
        "datacite",
    ]
    assert chosen_sources("computing", "article", settings, {}) == [
        "openalex",
        "crossref",
        "arxiv",
    ]
    assert chosen_sources("life", "preprint", settings, {}) == [
        "openalex",
        "crossref",
        "europepmc",
        "arxiv",
    ]
    assert chosen_sources("energy", "dataset", settings, {}) == [
        "openalex",
        "crossref",
        "osti",
        "datacite",
    ]
    off = build_settings(literature_disabled=["crossref"])
    assert chosen_sources("computing", "any", off, {"semantic_scholar": True}) == [
        "openalex",
        "arxiv",
        "datacite",
        "semantic_scholar",
    ]


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("10.1038/s41586-025-09482-1", {"doi": "10.1038/s41586-025-09482-1"}),
        ("https://doi.org/10.1038/ABC", {"doi": "10.1038/abc"}),
        ("https://arxiv.org/abs/2201.08228v2", {"arxiv": "2201.08228"}),
        ("10.48550/arXiv.2201.08228", {"arxiv": "2201.08228"}),
        ("pmid:12345", {"pmid": "12345"}),
        ("W4414090318", {"openalex": "W4414090318"}),
    ],
)
def test_parse_identifier(raw: str, parsed: dict[str, str]) -> None:
    assert parse_identifier(raw) == parsed


def test_exa_drops_the_publication_category_when_domains_are_given() -> None:
    from clio_agent.search import web_sources as ws

    sent: dict = {}

    async def fake_tool(source, url, tool, args, **_):
        sent.update(args)
        return {"content": [{"type": "text", "text": '{"results": []}'}]}

    import asyncio

    original = ws.http.mcp_tool
    ws.http.mcp_tool = fake_tool
    try:
        query = ws.WebQuery(query="q", kind="research", include_domains=["arxiv.org"])
        answer = asyncio.run(ws._exa(query, "", 10))
    finally:
        ws.http.mcp_tool = original
    assert "category" not in sent and sent["includeDomains"] == ["arxiv.org"]
    assert "kind" not in answer.applied


def test_an_mcp_tool_error_keeps_its_text_and_invalid_arguments_are_refused() -> None:
    from clio_agent.search import http as h

    refused = h.tool_error(
        "exa",
        {
            "isError": True,
            "content": [{"type": "text", "text": "web_search error (400): bad domain"}],
        },
    )
    assert refused.kind == h.REFUSED and "bad domain" in refused.detail
    failed = h.tool_error("exa", {"isError": True, "content": [{"type": "text", "text": "boom"}]})
    assert failed.kind == h.FAILED and failed.detail == "boom"
