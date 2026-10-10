"""The one search-backend abstraction: routing, the Web MCP environment and the guard."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from clio_agent.gact.runtime.app_state import with_search_guard
from clio_agent.search import backend as search_backend
from clio_agent.search.backend import (
    ClioWebSearchBackend,
    LocalSearxngBackend,
    NoSearchBackend,
    SearchBackendUnavailableError,
    SearchNotConfiguredError,
    is_web_search_mcp,
    register_local_endpoint_resolver,
    resolve_search_backend,
    web_mcp_environment,
    web_search_guard,
)
from clio_agent.search.settings import build_settings
from clio_agent.tools.mcp_environment import stdio_environment
from clio_agent.tools.tool_hooks import InterceptDecision

WEB_MCP = ("clio-kit", ["mcp-server", "web"])


@pytest.fixture(autouse=True)
def _fresh_routing() -> Iterator[None]:
    register_local_endpoint_resolver(None)
    search_backend._set_routed(False)  # noqa: SLF001
    yield
    register_local_endpoint_resolver(None)
    search_backend._set_routed(False)  # noqa: SLF001


@pytest.fixture
def listener() -> Iterator[int]:
    """A loopback port something is listening on (stands in for a running SearXNG)."""

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        yield server.getsockname()[1]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_each_backend_name_resolves_to_its_backend() -> None:
    assert isinstance(resolve_search_backend(build_settings()), LocalSearxngBackend)
    gateway = build_settings(backend="clio_web_search", clio_web_search_url="http://h:8089")
    assert isinstance(resolve_search_backend(gateway), ClioWebSearchBackend)
    assert isinstance(resolve_search_backend(build_settings(backend="none")), NoSearchBackend)


@pytest.mark.parametrize(
    ("command", "args", "expected"),
    [
        ("clio-kit", ["mcp-server", "web"], True),
        ("uvx", ["--from", "clio-kit==2.10.5", "clio-kit", "mcp-server", "web"], True),
        ("/opt/bin/clio-web-search-mcp", [], True),
        ("C:\\clio\\clio-web-search-mcp.exe", [], True),
        ("python", ["-c", "from web_mcp.server import main; main()"], True),
        ("clio-kit", ["mcp-server", "hdf5"], False),
        ("web-search-other", ["mcp-server"], False),
    ],
)
def test_web_mcp_launchers_are_recognised(command: str, args: list[str], expected: bool) -> None:
    assert is_web_search_mcp(command, args) is expected


def test_local_searxng_routes_the_web_mcp_to_the_recorded_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "internal.example")
    register_local_endpoint_resolver(lambda: "http://127.0.0.1:24001/")
    env = web_mcp_environment(*WEB_MCP, {}, settings=build_settings())
    assert env["WEB_SEARCH_PROVIDER"] == "searxng"
    assert env["WEB_SEARXNG_BASE_URL"] == "http://127.0.0.1:24001"
    # A host proxy must not carry the loopback requests; the host's own bypasses stay.
    assert env["NO_PROXY"] == env["no_proxy"] == "internal.example,127.0.0.1,localhost,::1"


def test_local_searxng_uses_the_configured_port_before_it_is_recorded() -> None:
    env = web_mcp_environment(*WEB_MCP, {}, settings=build_settings(port=24555))
    assert env["WEB_SEARXNG_BASE_URL"] == "http://127.0.0.1:24555"


def test_clio_web_search_routes_the_web_mcp_to_the_gateway() -> None:
    settings = build_settings(
        backend="clio_web_search", clio_web_search_url="http://other-machine:8089"
    )
    assert web_mcp_environment(*WEB_MCP, {}, settings=settings) == {
        "WEB_REMOTE_URL": "http://other-machine:8089"
    }


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (["mcp-server", "web", "--remote-url", "http://mine:8089"], {}),
        (["mcp-server", "web", "--remote-url=http://mine:8089"], {}),
        (["mcp-server", "web"], {"WEB_SEARXNG_BASE_URL": "http://mine:8888"}),
        (["mcp-server", "web"], {"WEB_SEARCH_PROVIDER": "brave"}),
    ],
)
def test_an_explicit_declaration_wins(args: list[str], env: dict[str, str]) -> None:
    assert web_mcp_environment("clio-kit", args, env, settings=build_settings()) == {}
    assert web_search_guard("web_search", {}, settings=build_settings(backend="none")) is None


def test_other_servers_are_untouched() -> None:
    assert web_mcp_environment("clio-kit", ["mcp-server", "hdf5"], {}) == {}


def test_stdio_environment_routes_only_the_web_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_SEARXNG_BASE_URL", raising=False)
    monkeypatch.setenv("CLIO_SEARCH_SEARXNG_PORT", "24777")
    routed = stdio_environment({}, command="clio-kit", args=["mcp-server", "web"])
    assert routed["WEB_SEARXNG_BASE_URL"] == "http://127.0.0.1:24777"
    declared = stdio_environment(
        {"WEB_SEARXNG_BASE_URL": "http://declared:1"},
        command="clio-kit",
        args=["mcp-server", "web"],
    )
    assert declared["WEB_SEARXNG_BASE_URL"] == "http://declared:1"
    other = stdio_environment({}, command="clio-kit", args=["mcp-server", "hdf5"])
    assert "WEB_SEARXNG_BASE_URL" not in other


def test_search_off_answers_web_search_with_the_typed_fix() -> None:
    settings = build_settings(backend="none")
    assert web_mcp_environment(*WEB_MCP, {}, settings=settings) == {}
    result = web_search_guard("web_search", {"query": "x"}, settings=settings)
    assert result is not None
    assert result["ok"] is False and result["error"] == "search_not_configured"
    assert "search.backend" in result["fix"]
    assert web_search_guard("web_fetch", {}, settings=settings) is None
    with pytest.raises(SearchNotConfiguredError):
        resolve_search_backend(settings).search("x")


def test_a_local_searxng_that_is_not_answering_yet_is_reported_with_the_fix() -> None:
    settings = build_settings(port=free_port())
    web_mcp_environment(*WEB_MCP, {}, settings=settings)
    result = web_search_guard("web_search", {}, settings=settings)
    assert result is not None and result["error"] == "search_backend_unavailable"
    assert "Infrastructure > SearXNG" in result["fix"]
    assert "clio_web_search" in result["fix"]


def test_a_listening_local_searxng_is_not_guarded(listener: int) -> None:
    settings = build_settings(port=listener)
    web_mcp_environment(*WEB_MCP, {}, settings=settings)
    assert web_search_guard("web_search", {}, settings=settings) is None


def test_the_gateway_without_a_url_is_not_configured() -> None:
    settings = build_settings(backend="clio_web_search")
    problem = resolve_search_backend(settings).problem()
    assert isinstance(problem, SearchNotConfiguredError)
    assert "search.clio_web_search.url" in problem.fix


def _searxng(payload: dict[str, Any], seen: list[httpx.Request]) -> httpx.Client:
    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(respond))


def test_direct_search_uses_the_json_api_and_caps_results(listener: int) -> None:
    seen: list[httpx.Request] = []
    payload = {
        "results": [
            {"title": f"t{i}", "url": f"https://e/{i}", "content": "c", "engines": ["brave"]}
            for i in range(5)
        ],
        "unresponsive_engines": [["qwant", "timeout"]],
    }
    settings = build_settings(port=listener, max_results=3, safe_search=2, language="de")
    answer = resolve_search_backend(settings).search("hdf5", 10, client=_searxng(payload, seen))
    assert answer["ok"] and answer["backend"] == "local_searxng"
    assert answer["count"] == 3 and answer["results"][0]["url"] == "https://e/0"
    assert answer["unresponsive_engines"] == ["qwant"]
    params = seen[0].url.params
    assert seen[0].url.path == "/search"
    assert params["format"] == "json" and params["safesearch"] == "2"
    assert params["language"] == "de"


def test_direct_search_through_the_gateway() -> None:
    seen: list[httpx.Request] = []
    settings = build_settings(backend="clio_web_search", clio_web_search_url="http://gw:8089")
    answer = resolve_search_backend(settings).search(
        "q", client=_searxng({"results": [{"url": "https://x"}]}, seen)
    )
    assert answer["backend"] == "clio_web_search" and answer["count"] == 1
    assert str(seen[0].url).startswith("http://gw:8089/search?")


def test_a_failing_backend_is_a_typed_unavailable_error(listener: int) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    with pytest.raises(SearchBackendUnavailableError):
        resolve_search_backend(build_settings(port=listener)).search("q", client=client)


def test_the_guard_runs_after_the_live_interceptor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_SEARCH_BACKEND", "none")
    search_backend._set_routed(True)  # noqa: SLF001
    guarded = with_search_guard(None)("web_search", {})
    assert isinstance(guarded, InterceptDecision) and guarded.kind == "synthesize"
    assert guarded.result["error"] == "search_not_configured"
    assert with_search_guard(None)("fs_read_file", {}) is None
    hooked = InterceptDecision(kind="synthesize", result="hook")
    assert with_search_guard(lambda _n, _a: hooked)("web_search", {}) is hooked
