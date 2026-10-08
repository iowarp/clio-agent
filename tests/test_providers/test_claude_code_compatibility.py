"""Client compatibility follows the maintained catalog without a model turn."""

from pathlib import Path

import pytest

from clio_agent.providers.components import client_binary
from clio_agent.providers.model_discovery import claude_code, claude_code_catalog
from clio_agent.providers.model_discovery.claude_code_compatibility import models_for_client


def _models() -> list[dict[str, object]]:
    return [
        {"id": "claude-sonnet-5", "name": "Sonnet 5"},
        {"id": "claude-haiku-5-5", "name": "Haiku 5.5", "minimum_client_version": "2.1.293"},
    ]


def test_catalog_can_announce_a_model_before_the_compatible_client() -> None:
    """The old model stays usable and the future model is informational."""
    models = _models()
    accepted, waiting = models_for_client(models, "2.1.292")
    assert [row["id"] for row in accepted] == ["claude-sonnet-5"]
    assert waiting[0]["code"] == "client_update_required"
    assert waiting[0]["minimum_client_version"] == "2.1.293"
    assert "official Anthropic client update" in waiting[0]["reason"]
    assert len(models) == 2


def test_a_new_client_unlocks_the_maintained_model_without_a_catalog_change() -> None:
    """An actual client version at the documented minimum qualifies the model."""
    accepted, waiting = models_for_client(_models(), "2.1.293")
    assert [row["id"] for row in accepted] == ["claude-sonnet-5", "claude-haiku-5-5"]
    assert waiting == []


def test_an_unknown_client_version_never_claims_future_model_support() -> None:
    """Missing version evidence does not clear models with no documented minimum."""
    accepted, waiting = models_for_client(_models(), "")
    assert [row["id"] for row in accepted] == ["claude-sonnet-5"]
    assert waiting[0]["code"] == "client_version_unknown"


def test_discovery_rechecks_client_compatibility_after_a_client_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real discovery path applies committed catalog requirements per refresh."""
    document = Path(__file__).resolve().parents[2] / "catalogs" / "claude-code-models.json"
    catalog = claude_code_catalog._parse_catalog(document.read_bytes())
    monkeypatch.setattr(claude_code, "refresh_claude_code_catalog", lambda: catalog)
    monkeypatch.setattr(claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(claude_code, "_auth_status", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(claude_code.claude_code_effort, "read_cli_model_catalog", lambda: ([], ""))
    version = "2.1.292"
    monkeypatch.setattr(
        client_binary,
        "claude_client",
        lambda: client_binary.ClientSelection(
            client=client_binary.ClientBinary("claude", version, "installed"), reason="test"
        ),
    )
    first = claude_code.discover_claude_code()
    assert first.failed_reason is None
    assert "claude-haiku-5-5" not in {row["id"] for row in first.discovered}
    assert first.rejected[0]["code"] == "client_update_required"
    assert first.default_model == "claude-sonnet-5"
    version = "2.1.293"
    second = claude_code.discover_claude_code()
    assert "claude-haiku-5-5" in {row["id"] for row in second.discovered}
    assert second.rejected == []
    assert "claude-haiku-5-5" in {row["id"] for row in catalog.models}


def test_catalog_rejects_a_malformed_client_requirement() -> None:
    """A bad maintenance edit cannot silently disable compatibility checking."""
    with pytest.raises(claude_code_catalog.ClaudeCodeCatalogError, match="minimum_client_version"):
        claude_code_catalog._parse_catalog(
            b'{"schema_version":1,"models":[{"id":"claude-haiku-5-5",'
            b'"name":"Haiku","minimum_client_version":"latest"}]}'
        )
