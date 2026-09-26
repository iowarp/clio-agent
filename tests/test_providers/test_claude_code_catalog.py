"""The GitHub-hosted Claude Code catalog is fetched, cached, and validated.

Per owner ruling, Claude Code model existence, per-model input-modality
capabilities, and the account default all come from this document -- never
from an SDK/CLI probe. These tests pin the validation contract, plus the
fetched_catalog-backed caching contract: a fresh disk cache short-circuits the
network, and a failed fetch or a failed validation NEVER clears a previously
good catalog (the old in-memory cache used to clear itself on any failure).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from clio_agent.providers import fetched_catalog
from clio_agent.providers.fetched_catalog import FetchedCatalog
from clio_agent.providers.model_discovery import claude_code_catalog

REPO_CATALOG = Path(__file__).resolve().parents[2] / "catalogs" / "claude-code-models.json"


def _response(payload: str, *, status: int = 200, etag: str = "") -> httpx.Response:
    headers = {"etag": etag} if etag else {}
    return httpx.Response(
        status,
        text=payload,
        headers=headers,
        request=httpx.Request("GET", claude_code_catalog.CLAUDE_CODE_CATALOG_URL),
    )


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FetchedCatalog:
    """Point the module's catalog singleton at a per-test disk cache.

    Exercises the REAL module functions end to end (no stubbing of
    ``load_claude_code_catalog`` et al.), isolated from both the real network
    and any other test's cache file.
    """
    fresh: FetchedCatalog = FetchedCatalog(
        "claude-code-models-test",
        claude_code_catalog.CLAUDE_CODE_CATALOG_URL,
        parse=claude_code_catalog._parse_catalog,
        ttl_s=claude_code_catalog.DEFAULT_TTL_S,
        max_bytes=claude_code_catalog._MAX_BYTES,
        timeout_s=claude_code_catalog._FETCH_TIMEOUT_S,
        cache_path=tmp_path / "claude-code-models.json",
    )
    monkeypatch.setattr(claude_code_catalog, "_CATALOG", fresh)
    return fresh


def test_committed_catalog_file_parses_via_real_validator() -> None:
    """The repo-shipped ``catalogs/claude-code-models.json`` must itself be valid."""
    catalog = claude_code_catalog._parse_catalog(REPO_CATALOG.read_bytes())
    ids = {row["id"] for row in catalog.models}
    assert "claude-sonnet-5" in ids
    assert catalog.default_model in ids
    for row in catalog.models:
        assert "text" in row["capabilities"]


def test_first_read_fetches_then_ttl_serves_disk_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _get(url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        return _response(
            '{"schema_version":1,"models":[{"id":"claude-opus-5","name":"Claude Opus"}]}'
        )

    monkeypatch.setattr(fetched_catalog.httpx, "get", _get)
    first = claude_code_catalog.load_claude_code_candidates()
    second = claude_code_catalog.load_claude_code_candidates()

    assert first[0]["id"] == "claude-opus-5"
    assert first[0]["name"] == "Claude Opus"
    # No "capabilities" key in the source row -> text-only, typed unevidenced.
    assert first[0]["capabilities"] == ["text"]
    evidence = first[0]["capability_evidence"]
    assert evidence["source"] == "claude_code_catalog"
    assert evidence["reason"] == "modality_uncataloged"
    assert evidence["unevidenced"] == ["image", "pdf"]
    assert second[0]["id"] == "claude-opus-5"
    # Second read is within the TTL -> served from disk, no second network call.
    assert calls == [claude_code_catalog.CLAUDE_CODE_CATALOG_URL]


def test_refresh_forces_a_live_fetch_past_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _get(url: str, **kwargs: object) -> httpx.Response:
        calls["n"] += 1
        return _response(
            f'{{"schema_version":1,"models":[{{"id":"claude-opus-5-{calls["n"]}","name":"Opus"}}]}}'
        )

    monkeypatch.setattr(fetched_catalog.httpx, "get", _get)
    first = claude_code_catalog.load_claude_code_candidates()
    second = claude_code_catalog.refresh_claude_code_candidates()

    assert first[0]["id"] == "claude-opus-5-1"
    assert second[0]["id"] == "claude-opus-5-2"
    assert calls["n"] == 2


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
def test_claude_code_catalog_rejects_bad_data_on_cold_start(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """Invalid data with NO existing cache is a total miss -> typed error."""
    monkeypatch.setattr(fetched_catalog.httpx, "get", lambda *_a, **_kw: _response(payload))
    with pytest.raises(claude_code_catalog.ClaudeCodeCatalogError):
        claude_code_catalog.load_claude_code_candidates()


def test_network_failure_on_cold_start_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response("upstream unavailable", status=503),
    )
    with pytest.raises(claude_code_catalog.ClaudeCodeCatalogError, match="Could not fetch"):
        claude_code_catalog.load_claude_code_candidates()


def test_oversized_response_on_cold_start_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    huge = (
        '{"schema_version":1,"models":['
        + ",".join('{"id":"claude-opus-5","name":"' + ("x" * 200) + '"}' for _ in range(400))
        + "]}"
    )
    assert len(huge.encode("utf-8")) > claude_code_catalog._MAX_BYTES
    monkeypatch.setattr(fetched_catalog.httpx, "get", lambda *_a, **_kw: _response(huge))
    with pytest.raises(claude_code_catalog.ClaudeCodeCatalogError):
        claude_code_catalog.load_claude_code_candidates()


def test_catalog_with_default_and_capabilities_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-declared catalog row: explicit capabilities + a valid default."""
    monkeypatch.setattr(
        fetched_catalog.httpx,
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
        fetched_catalog.httpx,
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
        fetched_catalog.httpx,
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


def test_refresh_survives_a_failed_fetch_and_keeps_last_good(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The core behavior change: a failed fetch no longer clears the catalog."""
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(
            '{"schema_version":1,"models":[{"id":"claude-opus-5-5","name":"Claude Opus 5.5"}]}'
        ),
    )
    good = claude_code_catalog.refresh_claude_code_catalog()
    assert good.models[0]["id"] == "claude-opus-5-5"

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("network is unreachable")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    with caplog.at_level("WARNING"):
        survived = claude_code_catalog.refresh_claude_code_catalog()
    assert survived.models[0]["id"] == "claude-opus-5-5"
    assert any("reason=" in record.message for record in caplog.records)

    cached, error = claude_code_catalog.cached_claude_code_catalog()
    assert error == ""
    assert cached is not None
    assert cached.models[0]["id"] == "claude-opus-5-5"
    assert claude_code_catalog.cached_claude_code_candidates() == (
        [
            {
                "id": "claude-opus-5-5",
                "name": "Claude Opus 5.5",
                "capabilities": ["text"],
                "capability_evidence": cached.models[0]["capability_evidence"],
            }
        ],
        "",
    )


def test_refresh_survives_a_failed_validation_and_keeps_last_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(
            '{"schema_version":1,"models":[{"id":"claude-opus-6","name":"Claude Opus 6"}]}'
        ),
    )
    good = claude_code_catalog.refresh_claude_code_catalog()
    assert good.models[0]["id"] == "claude-opus-6"

    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response('{"schema_version":1,"models":[]}'),
    )
    survived = claude_code_catalog.refresh_claude_code_catalog()
    assert survived.models[0]["id"] == "claude-opus-6"


def test_cached_claude_code_catalog_never_touches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fetched_catalog.httpx,
        "get",
        lambda *_a, **_kw: _response(
            '{"schema_version":1,"models":[{"id":"claude-opus-5","name":"Claude Opus"}]}'
        ),
    )
    claude_code_catalog.refresh_claude_code_catalog()

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise AssertionError("cached_claude_code_catalog must never hit the network")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    cached, error = claude_code_catalog.cached_claude_code_catalog()
    assert error == ""
    assert cached is not None
    assert cached.models[0]["id"] == "claude-opus-5"


def test_cold_start_with_no_cache_and_no_network_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No prior successful fetch, no bundled fallback for this catalog -> typed error."""

    def _boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("network is unreachable")

    monkeypatch.setattr(fetched_catalog.httpx, "get", _boom)
    cached, error = claude_code_catalog.cached_claude_code_catalog()
    assert cached is None
    assert error != ""
    with pytest.raises(claude_code_catalog.ClaudeCodeCatalogError):
        claude_code_catalog.load_claude_code_catalog()
