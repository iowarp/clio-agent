"""The GitHub-hosted Claude Code catalog is fetched and validated on every read."""

from __future__ import annotations

import httpx
import pytest

from clio_agent.providers.model_discovery import claude_code_catalog


def _response(payload: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        text=payload,
        request=httpx.Request("GET", claude_code_catalog.CLAUDE_CODE_CATALOG_URL),
    )


def test_claude_code_catalog_reads_github_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _get(url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        assert kwargs["timeout"] == 8.0
        return _response(
            '{"schema_version":1,"models":[{"id":"claude-opus-5-'
            + str(len(calls))
            + '","name":"Claude Opus"}]}'
        )

    monkeypatch.setattr(claude_code_catalog.httpx, "get", _get)
    first = claude_code_catalog.load_claude_code_candidates()
    second = claude_code_catalog.load_claude_code_candidates()

    assert first == [{"id": "claude-opus-5-1", "name": "Claude Opus"}]
    assert second == [{"id": "claude-opus-5-2", "name": "Claude Opus"}]
    assert calls == [claude_code_catalog.CLAUDE_CODE_CATALOG_URL] * 2


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":1,"models":[]}',
        '{"schema_version":2,"models":[{"id":"claude-opus-5","name":"Opus"}]}',
        '{"schema_version":1,"models":[{"id":"--danger","name":"Opus"}]}',
        '{"schema_version":1,"models":[{"id":"claude-opus-5","name":"Opus"},'
        '{"id":"claude-opus-5","name":"Again"}]}',
    ],
)
def test_claude_code_catalog_rejects_bad_data(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setattr(claude_code_catalog.httpx, "get", lambda *_a, **_kw: _response(payload))
    with pytest.raises(claude_code_catalog.ClaudeCodeCatalogError):
        claude_code_catalog.load_claude_code_candidates()


def test_claude_code_catalog_network_failure_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        claude_code_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response("upstream unavailable", status=503),
    )
    with pytest.raises(claude_code_catalog.ClaudeCodeCatalogError, match="Could not fetch"):
        claude_code_catalog.load_claude_code_candidates()


def test_refresh_replaces_candidate_cache_and_clears_it_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_code_catalog, "_cached_candidates", None)
    monkeypatch.setattr(claude_code_catalog, "_cached_error", "")
    monkeypatch.setattr(
        claude_code_catalog,
        "load_claude_code_candidates",
        lambda: [{"id": "claude-opus-5-5", "name": "Claude Opus 5.5"}],
    )
    claude_code_catalog.refresh_claude_code_candidates()
    assert claude_code_catalog.cached_claude_code_candidates()[0] == [
        {"id": "claude-opus-5-5", "name": "Claude Opus 5.5"}
    ]

    def _offline() -> list[dict[str, str]]:
        raise claude_code_catalog.ClaudeCodeCatalogError("GitHub unavailable")

    monkeypatch.setattr(claude_code_catalog, "load_claude_code_candidates", _offline)
    with pytest.raises(claude_code_catalog.ClaudeCodeCatalogError):
        claude_code_catalog.refresh_claude_code_candidates()
    assert claude_code_catalog.cached_claude_code_candidates() == ([], "GitHub unavailable")
