"""Rotation, the person's questions (one for many callers), fetch fallbacks, MRTR."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from clio_agent.search import broker, fetch, http, web, web_sources
from clio_agent.search.http import SourceError
from clio_agent.search.records import WebResult
from clio_agent.search.settings import build_settings, load_search_settings
from clio_agent.search.settings_store import update_search_settings
from clio_agent.search.web_sources import SourceAnswer, WebQuery
from clio_agent.tools.servers.web_limits import SEARCH_LIMITS
from tests.test_search._fakes import fixture, install, response_of


@pytest.fixture(autouse=True)
def _fresh_limits(tmp_path, monkeypatch):
    SEARCH_LIMITS.reset()
    monkeypatch.setattr(
        "clio_agent.search.records.payload_directory", lambda: tmp_path / "payloads"
    )
    yield
    SEARCH_LIMITS.reset()


def _settings(**values: Any):
    def change(section: dict[str, Any]) -> None:
        section.update(values)

    return update_search_settings(change)


class FakeSources:
    """Scripted per-source answers for ``web_sources.search``."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script
        self.calls: list[str] = []

    async def __call__(self, source: str, query: WebQuery, **_: Any) -> SourceAnswer:
        self.calls.append(source)
        step = (
            self.script[source].pop(0) if len(self.script[source]) > 1 else self.script[source][0]
        )
        if isinstance(step, Exception):
            raise step
        return SourceAnswer(
            [WebResult(f"{source} hit", f"https://{source}.example.org/a")], {"raw": source}
        )


def _ok() -> str:
    return "ok"


def test_first_use_asks_approval_then_remembers_it(monkeypatch) -> None:
    fake = FakeSources({"keenable": [_ok()]})
    monkeypatch.setattr(web_sources, "search", fake)
    _settings(web={"order": ["keenable"], "disabled": ["exa", "firecrawl", "you", "parallel"]})
    with pytest.raises(broker.AskPerson) as asked:
        asyncio.run(web.web_search(WebQuery(query="adios2")))
    assert asked.value.decision.topic == "approve:keenable"
    assert fake.calls == []  # nothing left the computer before the person agreed
    broker.apply_answer("approve:keenable", broker.APPROVE)
    assert "keenable" in load_search_settings().approved
    result = asyncio.run(web.web_search(WebQuery(query="adios2")))
    assert result["ok"] and result["source"] == "keenable"
    assert result["results"] == [{"title": "keenable hit", "url": "https://keenable.example.org/a"}]
    assert result["payload_file"].endswith(".json")


def test_declining_a_source_moves_to_the_next_with_a_note(monkeypatch) -> None:
    monkeypatch.setattr(web_sources, "search", FakeSources({"keenable": [_ok()], "exa": [_ok()]}))
    _settings(approved=["exa"], web={"order": ["keenable", "exa"]})
    broker.apply_answer("approve:keenable", broker.DECLINE)
    result = asyncio.run(web.web_search(WebQuery(query="q")))
    assert result["source"] == "exa"
    assert result["notes"][0]["code"] == "search_not_approved"


def test_used_allowance_asks_once_for_eight_parallel_callers(monkeypatch) -> None:
    used = SourceError("keenable", http.ALLOWANCE_USED, "HTTP 429", 3600)
    fake = FakeSources({"keenable": [used], "exa": [_ok()]})
    monkeypatch.setattr(web_sources, "search", fake)
    _settings(approved=["keenable", "exa"], web={"order": ["keenable", "exa"]})

    async def run() -> list[Any]:
        calls = [asyncio.create_task(_attempt()) for _ in range(8)]
        await asyncio.sleep(0.3)
        asked = [t for t in calls if t.done() and isinstance(t.result(), broker.Decision)]
        assert len(asked) == 1, "exactly one caller asks the person"
        broker.apply_answer(asked[0].result().topic, broker.SWITCH)
        return await asyncio.gather(*calls)

    async def _attempt() -> Any:
        try:
            return await web.web_search(WebQuery(query="q"))
        except broker.AskPerson as ask:
            return ask.decision

    results = asyncio.run(run())
    answered = [r for r in results if isinstance(r, dict)]
    assert len(answered) == 7 and all(r["source"] == "exa" for r in answered)
    assert fake.calls.count("keenable") == 1  # the resting source is never called again
    codes = [n["code"] for n in answered[0]["notes"]]
    assert codes == ["search_source_allowance_used", "search_rotated"]


def test_stop_ends_the_search_with_a_typed_reason(monkeypatch) -> None:
    used = SourceError("keenable", http.ALLOWANCE_USED, "HTTP 429", 3600)
    monkeypatch.setattr(web_sources, "search", FakeSources({"keenable": [used], "exa": [_ok()]}))
    _settings(approved=["keenable", "exa"], web={"order": ["keenable", "exa"]})
    with pytest.raises(broker.AskPerson) as asked:
        asyncio.run(web.web_search(WebQuery(query="q")))
    broker.apply_answer(asked.value.decision.topic, broker.STOP)
    with pytest.raises(web.SearchFailure) as failed:
        asyncio.run(web.web_search(WebQuery(query="q")))
    assert failed.value.code == "search_stopped_by_person"


def test_auto_switch_and_short_rate_limits_never_ask(monkeypatch) -> None:
    burst = SourceError("keenable", http.RATE_LIMITED, "HTTP 429", 20)
    monkeypatch.setattr(web_sources, "search", FakeSources({"keenable": [burst], "exa": [_ok()]}))
    _settings(approved=["keenable", "exa"], web={"order": ["keenable", "exa"]})
    result = asyncio.run(web.web_search(WebQuery(query="q")))
    assert result["source"] == "exa"
    assert result["notes"][0]["code"] == "search_source_rate_limited"

    SEARCH_LIMITS.reset()
    used = SourceError("keenable", http.ALLOWANCE_USED, "HTTP 429", 3600)
    monkeypatch.setattr(web_sources, "search", FakeSources({"keenable": [used], "exa": [_ok()]}))
    _settings(web={"order": ["keenable", "exa"], "auto_switch": True})
    assert asyncio.run(web.web_search(WebQuery(query="q")))["source"] == "exa"


def test_switch_always_saves_auto_switch(monkeypatch) -> None:
    _settings(approved=["keenable"])
    broker.apply_answer("exhausted:keenable:1", broker.SWITCH_ALWAYS)
    assert load_search_settings().auto_switch is True


def test_every_source_resting_is_one_typed_failure(monkeypatch) -> None:
    down = SourceError("x", http.FAILED, "HTTP 503")
    monkeypatch.setattr(web_sources, "search", FakeSources({"keenable": [down], "exa": [down]}))
    _settings(
        approved=["keenable", "exa"],
        web={"order": ["keenable", "exa"], "disabled": ["firecrawl", "you", "parallel"]},
    )
    with pytest.raises(web.SearchFailure) as failed:
        asyncio.run(web.web_search(WebQuery(query="q")))
    assert failed.value.code == "search_all_sources_unavailable"
    assert [n.code for n in failed.value.notes] == ["search_source_failed", "search_source_failed"]


def test_invalid_arguments_are_refused_before_any_call() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        web.check_query(WebQuery(query="q", after="2025/01/01"))
    with pytest.raises(ValueError, match="kind"):
        web.check_query(WebQuery(query="q", kind="blogs"))


# --------------------------------------------------------------------- fetch


@pytest.mark.parametrize(
    ("url", "public"),
    [
        ("https://arxiv.org/abs/2201.08228", True),
        ("http://localhost:8080/x", False),
        ("http://10.0.0.5/report", False),
        ("https://wiki.lab.internal/page", False),
        ("https://intranet/page", False),
        ("https://user:pw@example.org/", False),
        ("https://bucket.s3.amazonaws.com/f?X-Amz-Signature=abc", False),
        ("https://example.org/doc?token=abc", False),
        ("https://example.org/search?q=token", True),
    ],
)
def test_only_public_urls_may_reach_a_hosted_reader(url: str, public: bool) -> None:
    assert web.is_public_url(url) is public


_PAGE = (
    "<html><head><title>Slurm job arrays</title></head><body><main>"
    + (
        "<p>Job arrays submit many similar jobs with one sbatch command and the --array option.</p>"
        * 6
    )
    + "</main></body></html>"
)


def test_local_fetch_returns_markdown_for_evidence_binding(monkeypatch) -> None:
    install(
        monkeypatch,
        lambda request: httpx.Response(200, text=_PAGE, headers={"content-type": "text/html"}),
    )
    result = asyncio.run(fetch.web_fetch("https://slurm.schedmd.com/job_array.html"))
    assert result["ok"] is True and result["status"] == 200 and result["reader"] == "local"
    assert result["title"] == "Slurm job arrays"
    assert "--array option" in result["content"]


def test_blocked_public_page_is_read_by_an_approved_hosted_reader(monkeypatch) -> None:
    _settings(approved=["keenable"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.keenable.ai":
            return response_of(fixture("keenable", "fetch_ok"))
        return httpx.Response(403, text="blocked")

    install(monkeypatch, handler)
    result = asyncio.run(
        fetch.web_fetch("https://adios2.readthedocs.io/en/latest/engines/engines.html")
    )
    assert result["ok"] is True and result["reader"] == "keenable"
    assert result["notes"][0]["code"] == "fetch_hosted_reader"
    assert "Supported Engines" in result["content"]


def test_blocked_private_page_never_reaches_a_hosted_reader(monkeypatch) -> None:
    _settings(approved=["keenable"])
    seen = install(monkeypatch, lambda request: httpx.Response(403, text="no"))
    result = asyncio.run(fetch.web_fetch("https://wiki.lab.internal/secret"))
    assert result["ok"] is False
    assert [n["code"] for n in result["notes"]] == ["fetch_private_url"]
    assert all(r.url.host != "api.keenable.ai" for r in seen)


def test_pdf_is_saved_for_view_pdf_when_no_reader_can_help(monkeypatch, tmp_path) -> None:
    install(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"%PDF-1.7 ...", headers={"content-type": "application/pdf"}
        ),
    )
    monkeypatch.setattr(fetch, "payload_directory", lambda: tmp_path)
    result = asyncio.run(fetch.web_fetch("https://arxiv.org/pdf/2201.08228"))
    assert result["ok"] is False and result["pdf_path"].endswith(".pdf")
    assert "view_pdf" in result["error"]["detail"]


# ----------------------------------------------------- MCP round trip (MRTR)


def test_question_round_trip_through_the_mcp_client(monkeypatch) -> None:
    from fastmcp.client.elicitation import ElicitResult

    from clio_agent.tools.mcp_runtime import (
        MCPClientCapabilities,
        MCPClientHandlers,
        make_mcp_client,
    )
    from clio_agent.tools.servers.web_server import web_server

    fake = FakeSources({"keenable": [_ok()]})
    monkeypatch.setattr(web_sources, "search", fake)
    _settings(web={"order": ["keenable"], "disabled": ["exa", "firecrawl", "you", "parallel"]})
    asked: list[Any] = []

    async def answer(context, message, response_type, params, request_context) -> Any:
        asked.append(params.requested_schema)
        return ElicitResult(action="accept", content={"choice": broker.APPROVE})

    client = make_mcp_client(
        web_server,
        handlers=MCPClientHandlers(elicitation=answer),
        capabilities=MCPClientCapabilities(elicitation_form=True),
    )

    async def run() -> Any:
        async with client:
            return await client.call_tool("search", {"query": "adios2"})

    result = asyncio.run(run())
    data = result.structured_content
    assert data["ok"] is True and data["source"] == "keenable"
    schema = asked[0]["properties"]["choice"]
    assert schema["enum"] == [broker.APPROVE, broker.DECLINE]
    assert "keenable" in load_search_settings().approved


def test_settings_build_rejects_bad_service_url() -> None:
    with pytest.raises(ValueError, match="http"):
        build_settings(service_url="ftp://x")


def test_literature_waits_out_a_short_burst_limit_then_answers(monkeypatch) -> None:
    from clio_agent.search import literature

    calls: list[int] = []

    async def flaky() -> list[str]:
        calls.append(1)
        if len(calls) == 1:
            raise SourceError("crossref", http.RATE_LIMITED, "HTTP 429", 0.05)
        return ["work"]

    notes: list[Any] = []
    assert asyncio.run(literature._call("crossref", flaky, notes)) == ["work"]
    assert len(calls) == 2 and notes == []


def test_literature_reports_a_long_limit_without_waiting(monkeypatch) -> None:
    from clio_agent.search import literature

    async def used() -> list[str]:
        raise SourceError("openalex", http.ALLOWANCE_USED, "HTTP 429", 14000)

    notes: list[Any] = []
    assert asyncio.run(literature._call("openalex", used, notes)) is None
    assert [n.code for n in notes] == ["literature_source_budget_used"]
