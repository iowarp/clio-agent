"""``web_search`` stays present while the gateway starts, and its calls say what to do."""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from clio_agent.search import backend as search_backend
from clio_agent.search.backend import (
    WEB_SEARCH_TOOL,
    ClioWebSearchBackend,
    SearchBackendUnavailableError,
    SearchNotConfiguredError,
    register_local_endpoint_resolver,
    resolve_search_backend,
    web_mcp_environment,
    web_search_guard,
)
from clio_agent.search.settings import build_settings
from clio_agent.search.web_mcp_default import DEFAULT_WEB_MCP_NAME, degraded_web_mcp_placeholder
from clio_agent.tools.mcp_config import MCPServerSpec

WEB_MCP = ("clio-kit", ["mcp-server", "web"])
WEB_SPEC = MCPServerSpec(
    name=DEFAULT_WEB_MCP_NAME, transport="stdio", command="clio-kit", args=("mcp-server", "web")
)


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    register_local_endpoint_resolver(None)
    search_backend._set_routed(False)  # noqa: SLF001
    search_backend._READYZ_CACHE.clear()  # noqa: SLF001
    yield
    register_local_endpoint_resolver(None)
    search_backend._set_routed(False)  # noqa: SLF001
    search_backend._READYZ_CACHE.clear()  # noqa: SLF001


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def gateway_settings(url: str = "http://gw:8089") -> Any:
    return build_settings(backend="clio_web_search", clio_web_search_url=url)


@pytest.fixture
def readyz(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[httpx.Request], Any]], list]:
    """Answer the probe's HTTP calls with a handler; returns the requests seen."""

    real_client = httpx.Client

    def install(handler: Callable[[httpx.Request], Any]) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        def client(**kwargs: Any) -> httpx.Client:
            kwargs.pop("trust_env", None)
            return real_client(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr(search_backend.httpx, "Client", client)
        return seen

    return install


# -- the placeholder -------------------------------------------------------------------


def test_a_degraded_web_namespace_gets_a_web_search_placeholder() -> None:
    tools = degraded_web_mcp_placeholder(DEFAULT_WEB_MCP_NAME, WEB_SPEC)
    assert list(tools) == [WEB_SEARCH_TOOL] == ["web_search"]
    tool = tools[WEB_SEARCH_TOOL]
    assert tool.name == WEB_SEARCH_TOOL
    assert tool.description and "not ready" in tool.description
    schema = tool.input_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["count"]["type"] == "integer"


def test_the_placeholder_also_covers_a_uvx_launched_web_mcp() -> None:
    spec = MCPServerSpec(
        name=DEFAULT_WEB_MCP_NAME,
        transport="stdio",
        command="uvx",
        args=("--from", "clio-kit==2.10.5", "clio-kit", "mcp-server", "web"),
    )
    assert set(degraded_web_mcp_placeholder(DEFAULT_WEB_MCP_NAME, spec)) == {WEB_SEARCH_TOOL}


@pytest.mark.parametrize(
    ("namespace", "spec"),
    [
        ("hdf5", WEB_SPEC),
        ("search", WEB_SPEC),
        (DEFAULT_WEB_MCP_NAME, None),
        (
            DEFAULT_WEB_MCP_NAME,
            MCPServerSpec(name="web", transport="http", url="http://x/mcp", source="user"),
        ),
        (
            DEFAULT_WEB_MCP_NAME,
            MCPServerSpec(
                name="web", transport="stdio", command="clio-kit", args=("mcp-server", "hdf5")
            ),
        ),
    ],
)
def test_no_placeholder_outside_a_web_mcp_named_web(
    namespace: str, spec: MCPServerSpec | None
) -> None:
    assert degraded_web_mcp_placeholder(namespace, spec) == {}


# -- the gateway's readiness probe -----------------------------------------------------


def test_a_ready_gateway_has_no_readiness_problem(readyz: Any) -> None:
    seen = readyz(lambda _r: httpx.Response(200, json={"ok": True}))
    backend = resolve_search_backend(gateway_settings("http://gw:8089/"))
    assert isinstance(backend, ClioWebSearchBackend)
    assert backend.readiness_problem() is None
    assert [str(r.url) for r in seen] == ["http://gw:8089/readyz"]


def test_a_gateway_whose_readyz_fails_is_unavailable(readyz: Any) -> None:
    readyz(lambda _r: httpx.Response(503))
    problem = resolve_search_backend(gateway_settings()).readiness_problem()
    assert isinstance(problem, SearchBackendUnavailableError)
    assert problem.code == "search_backend_unavailable"
    assert "HTTP 503" in problem.detail and "http://gw:8089" in problem.detail
    assert "http://gw:8089" in problem.fix


def test_a_gateway_that_times_out_is_unavailable(readyz: Any) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    readyz(timeout)
    problem = resolve_search_backend(gateway_settings()).readiness_problem()
    assert isinstance(problem, SearchBackendUnavailableError)
    assert "ReadTimeout" in problem.detail


def test_a_gateway_refusing_connections_is_unavailable() -> None:
    url = f"http://127.0.0.1:{free_port()}"
    problem = resolve_search_backend(gateway_settings(url)).readiness_problem()
    assert isinstance(problem, SearchBackendUnavailableError)
    assert "not answering" in problem.detail and url in problem.detail


def test_a_gateway_without_a_url_reports_configuration_not_readiness(readyz: Any) -> None:
    seen = readyz(lambda _r: httpx.Response(200))
    problem = resolve_search_backend(build_settings(backend="clio_web_search")).readiness_problem()
    assert isinstance(problem, SearchNotConfiguredError)
    assert not isinstance(problem, SearchBackendUnavailableError)
    assert seen == []


def test_the_readyz_answer_is_cached_briefly(readyz: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(search_backend.time, "monotonic", lambda: clock["now"])
    status = {"code": 503}
    seen = readyz(lambda _r: httpx.Response(status["code"]))
    backend = resolve_search_backend(gateway_settings())

    first = backend.readiness_problem()
    assert isinstance(first, SearchBackendUnavailableError)
    status["code"] = 200
    clock["now"] += search_backend._READYZ_CACHE_S - 0.5  # noqa: SLF001
    assert backend.readiness_problem() is first
    assert len(seen) == 1

    clock["now"] += 1.0
    assert backend.readiness_problem() is None
    assert len(seen) == 2
    assert backend.readiness_problem() is None
    assert len(seen) == 2


def test_other_backends_readiness_is_their_problem() -> None:
    backend = resolve_search_backend(build_settings(backend="none"))
    assert backend.readiness_problem() is not None
    assert backend.readiness_problem().code == backend.problem().code  # type: ignore[union-attr]


# -- the guard -------------------------------------------------------------------------


def test_the_guard_answers_a_typed_error_while_the_gateway_is_not_ready(readyz: Any) -> None:
    readyz(lambda _r: httpx.Response(502))
    settings = gateway_settings()
    web_mcp_environment(*WEB_MCP, {}, settings=settings)
    result = web_search_guard(WEB_SEARCH_TOOL, {"query": "x"}, settings=settings)
    assert result is not None
    assert result["ok"] is False
    assert result["error"] == "search_backend_unavailable"
    assert "HTTP 502" in result["detail"]
    assert "http://gw:8089" in result["fix"]
    assert web_search_guard("web_fetch", {}, settings=settings) is None


def test_the_guard_answers_a_typed_error_when_the_gateway_refuses() -> None:
    settings = gateway_settings(f"http://127.0.0.1:{free_port()}")
    web_mcp_environment(*WEB_MCP, {}, settings=settings)
    result = web_search_guard(WEB_SEARCH_TOOL, {}, settings=settings)
    assert result is not None and result["error"] == "search_backend_unavailable"


def test_the_guard_lets_calls_through_once_the_gateway_is_ready(readyz: Any) -> None:
    readyz(lambda _r: httpx.Response(200))
    settings = gateway_settings()
    web_mcp_environment(*WEB_MCP, {}, settings=settings)
    assert web_search_guard(WEB_SEARCH_TOOL, {"query": "x"}, settings=settings) is None


def test_an_unrouted_web_mcp_is_not_probed(readyz: Any) -> None:
    seen = readyz(lambda _r: httpx.Response(503))
    assert web_search_guard(WEB_SEARCH_TOOL, {}, settings=gateway_settings()) is None
    assert seen == []
