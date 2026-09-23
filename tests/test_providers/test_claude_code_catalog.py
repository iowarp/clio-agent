"""The GitHub-hosted Claude Code catalog is fetched and validated on every read.

Per owner ruling, Claude Code model existence, per-model input-modality
capabilities, and the account default all come from this document -- never
from an SDK/CLI probe. These tests pin the validation contract.
"""

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

    assert first[0]["id"] == "claude-opus-5-1"
    assert first[0]["name"] == "Claude Opus"
    # No "capabilities" key in the source row -> text-only, typed unevidenced.
    assert first[0]["capabilities"] == ["text"]
    evidence = first[0]["capability_evidence"]
    assert evidence["source"] == "claude_code_catalog"
    assert evidence["reason"] == "modality_uncataloged"
    assert evidence["unevidenced"] == ["image", "pdf"]
    assert second[0]["id"] == "claude-opus-5-2"
    assert calls == [claude_code_catalog.CLAUDE_CODE_CATALOG_URL] * 2


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":1,"models":[]}',
        '{"schema_version":2,"models":[{"id":"claude-opus-5","name":"Opus"}]}',
        '{"schema_version":1,"models":[{"id":"--danger","name":"Opus"}]}',
        '{"schema_version":1,"models":[{"id":"claude-opus-5","name":"Opus"},'
        '{"id":"claude-opus-5","name":"Again"}]}',
        # Invalid capabilities: unknown modality string.
        '{"schema_version":1,"models":[{"id":"claude-opus-5","name":"Opus",'
        '"capabilities":["text","smell"]}]}',
        # Invalid capabilities: empty list.
        '{"schema_version":1,"models":[{"id":"claude-opus-5","name":"Opus","capabilities":[]}]}',
        # Invalid capabilities: missing "text".
        '{"schema_version":1,"models":[{"id":"claude-opus-5","name":"Opus",'
        '"capabilities":["image"]}]}',
        # Invalid capabilities: duplicate entries.
        '{"schema_version":1,"models":[{"id":"claude-opus-5","name":"Opus",'
        '"capabilities":["text","text"]}]}',
        # default_model not a cataloged id.
        '{"schema_version":1,"default_model":"claude-nope","models":'
        '[{"id":"claude-opus-5","name":"Opus"}]}',
        # default_model wrong type.
        '{"schema_version":1,"default_model":5,"models":[{"id":"claude-opus-5","name":"Opus"}]}',
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


def test_catalog_with_default_and_capabilities_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-declared catalog row: explicit capabilities + a valid default."""
    monkeypatch.setattr(
        claude_code_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(
            '{"schema_version":1,"default_model":"claude-sonnet-5","models":['
            '{"id":"claude-sonnet-5","name":"Claude Sonnet 5",'
            '"capabilities":["text","image","pdf"]},'
            '{"id":"claude-haiku-4-5","name":"Claude Haiku 4.5",'
            '"capabilities":["text","image","pdf"]}]}'
        ),
    )
    catalog = claude_code_catalog.load_claude_code_catalog()
    assert catalog.default_model == "claude-sonnet-5"
    assert catalog.default_model_reason == ""
    ids = {row["id"]: row for row in catalog.models}
    assert ids["claude-sonnet-5"]["capabilities"] == ["text", "image", "pdf"]
    assert ids["claude-sonnet-5"]["capability_evidence"]["reason"] == "modality_cataloged"
    assert ids["claude-sonnet-5"]["capability_evidence"]["source"] == "claude_code_catalog"
    assert "unevidenced" not in ids["claude-sonnet-5"]["capability_evidence"]


def test_catalog_without_default_model_key_reports_typed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        claude_code_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(
            '{"schema_version":1,"models":[{"id":"claude-sonnet-5","name":"Claude Sonnet 5"}]}'
        ),
    )
    catalog = claude_code_catalog.load_claude_code_catalog()
    assert catalog.default_model == ""
    assert catalog.default_model_reason != ""
    assert "no default" in catalog.default_model_reason


def test_catalog_missing_capabilities_defaults_to_text_only_unevidenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        claude_code_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(
            '{"schema_version":1,"models":[{"id":"claude-sonnet-5","name":"Claude Sonnet 5"}]}'
        ),
    )
    catalog = claude_code_catalog.load_claude_code_catalog()
    row = catalog.models[0]
    assert row["capabilities"] == ["text"]
    evidence = row["capability_evidence"]
    assert evidence["reason"] == "modality_uncataloged"
    assert evidence["source"] == "claude_code_catalog"
    assert evidence["unevidenced"] == ["image", "pdf"]


def test_refresh_replaces_candidate_cache_and_clears_it_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_code_catalog, "_cached_catalog", None)
    monkeypatch.setattr(claude_code_catalog, "_cached_error", "")
    monkeypatch.setattr(
        claude_code_catalog,
        "load_claude_code_catalog",
        lambda: claude_code_catalog.ClaudeCodeCatalog(
            models=[
                {
                    "id": "claude-opus-5-5",
                    "name": "Claude Opus 5.5",
                    "capabilities": ["text"],
                    "capability_evidence": {},
                }
            ],
            default_model="",
            default_model_reason="",
        ),
    )
    claude_code_catalog.refresh_claude_code_catalog()
    cached, error = claude_code_catalog.cached_claude_code_catalog()
    assert cached is not None
    assert cached.models[0]["id"] == "claude-opus-5-5"
    assert error == ""
    assert claude_code_catalog.cached_claude_code_candidates() == (
        [
            {
                "id": "claude-opus-5-5",
                "name": "Claude Opus 5.5",
                "capabilities": ["text"],
                "capability_evidence": {},
            }
        ],
        "",
    )

    def _offline() -> claude_code_catalog.ClaudeCodeCatalog:
        raise claude_code_catalog.ClaudeCodeCatalogError("GitHub unavailable")

    monkeypatch.setattr(claude_code_catalog, "load_claude_code_catalog", _offline)
    with pytest.raises(claude_code_catalog.ClaudeCodeCatalogError):
        claude_code_catalog.refresh_claude_code_catalog()
    assert claude_code_catalog.cached_claude_code_catalog() == (None, "GitHub unavailable")
    assert claude_code_catalog.cached_claude_code_candidates() == (None, "GitHub unavailable")
