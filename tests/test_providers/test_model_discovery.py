"""Tests for the #1211 model-catalog refresh overlay + discovery mechanisms.

Unit-level: the overlay read/write/delta machinery, and each ``discover_*``
mechanism mocked at its CLI/network boundary (never a real subprocess or HTTP
call here). An autouse fixture stubs the context/output-limit resolution
(``attach_context_limits`` — #1211 review D4) every ``discover_*`` success path
now runs, so this file never touches models.dev/litellm/the local DB — that
cascade has its own tests in ``tests/test_providers/test_handshake_sources.py``.
Two ``@pytest.mark.live`` tests at the bottom actually hit the Codex
catalog/credential store and the installed ``claude`` binary — gated behind ``CLIO_RUN_LIVE=1`` like every
other live test in this suite (see ``tests/test_arc/test_live_plane_alcf.py``
for the house convention).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.providers import model_discovery
from clio_agent.providers.catalog import get_provider
from clio_agent.providers.model_discovery import claude_code as md_claude_code


@pytest.mark.asyncio
async def test_startup_refreshes_configured_cli_and_remote_claude_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.providers.model_discovery import claude_code_catalog
    from clio_agent.providers.model_discovery import refresh as md_refresh

    seen: list[str] = []
    monkeypatch.setattr(md_refresh, "is_provider_configured", lambda preset: preset.id == "codex")
    monkeypatch.setattr(
        claude_code_catalog,
        "refresh_claude_code_candidates",
        lambda: seen.append("github") or [],
    )

    async def _refresh(*, presets: Any) -> list[dict[str, Any]]:
        seen.extend(preset.id for preset in presets)
        return []

    monkeypatch.setattr(md_refresh, "refresh_all", _refresh)
    await md_refresh.refresh_subscription_catalogs_at_startup()
    assert seen == ["github", "codex"]


@pytest.fixture(autouse=True)
def _stub_claude_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery tests default to a deterministic catalog snapshot.

    Individual ``discover_claude_code`` tests override this again with their
    own ``refresh_claude_code_catalog`` stub; this only keeps tests that don't
    care about the catalog (e.g. the overlay/record_refresh section above)
    from ever making a real network call if something imports this module.
    """
    from clio_agent.providers.model_discovery import claude_code_effort
    from clio_agent.providers.model_discovery.claude_code_catalog import ClaudeCodeCatalog

    # Never spawn the real Claude Code CLI for its initialize model list.
    monkeypatch.setattr(
        claude_code_effort,
        "read_cli_model_catalog",
        lambda *_a, **_k: ([], "claude_code_cli_model_catalog_unavailable: test stub"),
    )
    monkeypatch.setattr(
        md_claude_code,
        "refresh_claude_code_catalog",
        lambda: ClaudeCodeCatalog(
            models=[
                {
                    "id": alias,
                    "name": alias,
                    "capabilities": ["text", "image", "pdf"],
                    "capability_evidence": {
                        "source": "claude_code_catalog",
                        "reason": "modality_cataloged",
                    },
                }
                for alias in ("fable", "opus", "sonnet", "haiku")
            ],
            default_model="sonnet",
            default_model_reason="",
        ),
    )


@pytest.fixture(autouse=True)
def _stub_codex_catalog_offline(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Keep every test in this file offline for the Codex catalog.

    ``discover_codex`` otherwise force-refreshes the maintained GitHub catalog;
    individual tests override this with their own rows via ``_stub_codex_catalog``.
    ``@pytest.mark.live`` tests are exempt -- they exist to hit the real catalog.
    """
    if request.node.get_closest_marker("live") is not None:
        return
    from clio_agent.providers.model_discovery import codex as md_codex
    from clio_agent.providers.model_discovery.codex_catalog import CodexCatalogError

    def _offline() -> Any:
        raise CodexCatalogError("Codex catalog fetch disabled in unit tests")

    monkeypatch.setattr(md_codex, "refresh_codex_catalog", _offline)


@pytest.fixture(autouse=True)
def _stub_context_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``discover_*`` success path calls ``attach_context_limits`` (#1211
    D4), which resolves each model's context/output limit via the SAME cascade
    the handshake uses (models.dev -> litellm -> local DB). Stub it out here so
    this whole file stays fast/deterministic/offline-safe."""
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.resolve_context", lambda model_id, kind: (None, "")
    )
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.resolve_output_limit", lambda model_id, kind: None
    )


# --------------------------------------------------------------------------- #
# overlay: path resolution, read, dual-key lookup.
# --------------------------------------------------------------------------- #


def test_overlay_path_honors_clio_model_catalog_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "custom_overlay.json"
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(target))
    assert model_discovery.overlay_path() == target


def test_overlay_path_defaults_to_user_data_dir_sibling_of_model_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLIO_MODEL_CATALOG", raising=False)
    from clio_agent import paths

    assert model_discovery.overlay_path() == paths.user_data_dir() / "model_catalog.json"


def test_read_overlay_absent_file_is_empty_dict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "does_not_exist.json"))
    assert model_discovery.read_overlay() == {}


def test_read_overlay_malformed_json_raises_typed_not_silent_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The #1202 ``_read_mcp_yaml`` lesson: a corrupt file is a typed error, not {}."""
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text("{this is not json", encoding="utf-8")
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))
    with pytest.raises(model_discovery.OverlayMalformedError):
        model_discovery.read_overlay()


def test_read_overlay_non_object_json_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))
    with pytest.raises(model_discovery.OverlayMalformedError):
        model_discovery.read_overlay()


def test_read_overlay_os_error_is_unreadable_not_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review N1: an I/O failure (permissions, locked file) is a DISTINCT
    typed error from bad JSON content -- OverlayUnreadableError, a subclass of
    OverlayMalformedError so every existing catch site keeps working."""
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))

    def _boom(self: Path, encoding: str = "utf-8") -> str:  # noqa: ARG001
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(model_discovery.OverlayUnreadableError):
        model_discovery.read_overlay()
    # Subclass relationship: existing `except OverlayMalformedError` still catches it.
    assert issubclass(model_discovery.OverlayUnreadableError, model_discovery.OverlayMalformedError)


def test_overlay_models_wire_absent_entry_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    assert model_discovery.overlay_models_wire("codex", "codex") is None


def test_overlay_models_wire_empty_models_list_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text(json.dumps({"codex": {"models": []}}), encoding="utf-8")
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))
    assert model_discovery.overlay_models_wire("codex", "codex") is None


def test_overlay_models_wire_present_serves_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text(
        json.dumps(
            {
                "codex": {
                    "models": [{"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol", "description": ""}],
                    "source": "codex_catalog",
                    "default_model": "gpt-5.6-sol",
                    "generated_at": "2026-08-14T00:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))
    wire = model_discovery.overlay_models_wire("codex", "codex")
    assert wire is not None
    assert wire["models"] == [{"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol", "description": ""}]
    assert wire["source"] == "codex_catalog"
    assert wire["default_model"] == "gpt-5.6-sol"


def test_overlay_models_wire_falls_back_to_bare_kind_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dual-keying (mirrors ``as_provider_models_dict``): a lookup by preset id
    that has no dedicated row falls back to the bare provider_kind row."""
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text(
        json.dumps({"argonne": {"models": [{"id": "m1", "name": "m1", "description": ""}]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))
    wire = model_discovery.overlay_models_wire("argonne_sophia", "argonne")
    assert wire is not None
    assert wire["models"][0]["id"] == "m1"


def test_overlay_default_model_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text(
        json.dumps({"codex": {"models": [{"id": "x"}], "default_model": "gpt-5.6-sol"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))
    assert model_discovery.overlay_default_model("codex", "codex") == "gpt-5.6-sol"


def test_overlay_default_model_absent_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    assert model_discovery.overlay_default_model("codex", "codex") == ""


def test_overlay_default_model_malformed_degrades_to_empty_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A passive read (#1211 D2) must never crash a listing/bind path on a
    corrupt overlay; it degrades to "" -- logged, never silent."""
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))
    with caplog.at_level("WARNING"):
        assert model_discovery.overlay_default_model("codex", "codex") == ""
    assert any("malformed" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# record_refresh: atomic merge-write + added/removed/unchanged delta.
# --------------------------------------------------------------------------- #


def test_record_refresh_first_success_reports_everything_added(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    result = model_discovery.ProviderDiscoveryResult(
        provider="codex",
        discovered=[
            {"id": "gpt-5.6-sol", "name": "Sol", "description": ""},
            {"id": "gpt-5.6-terra", "name": "Terra", "description": ""},
        ],
        source="codex_catalog",
        default_model="gpt-5.6-sol",
    )
    wire = model_discovery.record_refresh(result)
    assert wire["provider"] == "codex"
    assert sorted(wire["added"]) == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert wire["removed"] == []
    assert wire["unchanged"] == []
    assert wire["default_model"] == "gpt-5.6-sol"
    assert "failed_reason" not in wire

    # Persisted for the next GET / refresh to read back.
    overlay = model_discovery.read_overlay()
    assert {m["id"] for m in overlay["codex"]["models"]} == {"gpt-5.6-sol", "gpt-5.6-terra"}


def test_record_refresh_second_success_computes_delta_against_previous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[
                {"id": "gpt-5.5", "name": "5.5", "description": ""},
                {"id": "gpt-5.5-mini", "name": "5.5-mini", "description": ""},
            ],
            source="codex_catalog",
        )
    )
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[
                {"id": "gpt-5.5", "name": "5.5", "description": ""},
                {"id": "gpt-5.6-sol", "name": "Sol", "description": ""},
            ],
            source="codex_catalog",
        )
    )
    assert wire["added"] == ["gpt-5.6-sol"]
    assert wire["removed"] == ["gpt-5.5-mini"]
    assert wire["unchanged"] == ["gpt-5.5"]


def test_record_refresh_failure_keeps_previous_models_never_clears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No-silent-fallback: a failed probe must not clear the prior good list."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[{"id": "sonnet", "name": "Sonnet", "description": ""}],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            default_model="sonnet",
        )
    )
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            failed_reason="claude CLI not found on PATH",
        )
    )
    assert wire["failed_reason"] == "claude CLI not found on PATH"
    # The PREVIOUS list is what's served -- never silently cleared to [].
    assert wire["discovered"] == [{"id": "sonnet", "name": "Sonnet", "description": ""}]
    assert wire["added"] == []
    assert wire["removed"] == []
    assert wire["unchanged"] == ["sonnet"]

    overlay = model_discovery.read_overlay()
    assert overlay["claude_code"]["models"] == [
        {"id": "sonnet", "name": "Sonnet", "description": ""}
    ]
    assert overlay["claude_code"]["failed_reason"] == "claude CLI not found on PATH"


def test_record_refresh_failure_with_no_previous_entry_stays_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="openrouter",
            discovered=[],
            source="live_handshake",
            failed_reason="no api key",
        )
    )
    assert wire["failed_reason"] == "no api key"
    assert wire["discovered"] == []
    assert wire["added"] == wire["removed"] == wire["unchanged"] == []


def test_record_refresh_never_silently_clobbers_a_malformed_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))
    result = model_discovery.ProviderDiscoveryResult(
        provider="codex",
        discovered=[{"id": "x", "name": "x", "description": ""}],
        source="codex_catalog",
    )
    with pytest.raises(model_discovery.OverlayMalformedError):
        model_discovery.record_refresh(result)
    # The corrupt file is left exactly as it was -- never silently overwritten.
    assert overlay_file.read_text(encoding="utf-8") == "{not valid json"


def test_record_refresh_refuses_claimed_success_with_empty_discovered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review R1: a write-boundary guard. A ProviderDiscoveryResult with no
    failed_reason but discovered=[] is an upstream bug -- refuse it rather than
    silently narrowing the overlay to nothing."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    bad = model_discovery.ProviderDiscoveryResult(
        provider="codex", discovered=[], source="codex_catalog"
    )
    with pytest.raises(ValueError, match="refusing to write an empty models list"):
        model_discovery.record_refresh(bad)
    # Nothing was written at all.
    assert model_discovery.read_overlay() == {}


def test_record_refresh_persists_and_clears_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review N3: rejected reasons are persisted in the overlay, not just
    the transient wire response -- and a later fully-clean run clears them."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[{"id": "sonnet", "name": "Sonnet", "description": ""}],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            default_model="sonnet",
            rejected=[{"id": "fable", "reason": "404"}],
        )
    )
    assert wire["rejected"] == [{"id": "fable", "reason": "404"}]
    overlay = model_discovery.read_overlay()
    assert overlay["claude_code"]["rejected"] == [{"id": "fable", "reason": "404"}]

    wire2 = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[
                {"id": "sonnet", "name": "Sonnet", "description": ""},
                {"id": "fable", "name": "Fable", "description": ""},
            ],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            default_model="fable",
        )
    )
    assert "rejected" not in wire2
    overlay2 = model_discovery.read_overlay()
    assert "rejected" not in overlay2["claude_code"]


def test_record_refresh_persists_default_model_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review N5: a fallback default_model (not CLI-verified) carries a
    typed reason, persisted alongside it."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[{"id": "sonnet", "name": "Sonnet", "description": ""}],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            default_model="sonnet",
            default_model_reason="bare probe inconclusive; falling back",
        )
    )
    assert wire["default_model_reason"] == "bare probe inconclusive; falling back"
    overlay = model_discovery.read_overlay()
    assert overlay["claude_code"]["default_model_reason"] == "bare probe inconclusive; falling back"


def test_record_refresh_claude_code_serves_the_account_discovered_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A verified account default wins over every compiled-in candidate."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[
                {"id": "fable", "name": "Fable", "description": ""},
                {"id": "sonnet", "name": "Sonnet", "description": ""},
            ],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            default_model="fable",
        )
    )
    assert wire["default_model"] == "fable"
    assert "cli_default" not in wire
    overlay = model_discovery.read_overlay()
    assert overlay["claude_code"]["default_model"] == "fable"
    assert "cli_default" not in overlay["claude_code"]
    assert model_discovery.overlay_default_model("claude_code", "claude_code") == "fable"


def test_record_refresh_claude_code_keeps_discovered_sonnet_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A discovered Sonnet default is served without synthetic metadata."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[{"id": "sonnet", "name": "Sonnet", "description": ""}],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            default_model="sonnet",
        )
    )
    assert wire["default_model"] == "sonnet"
    assert "cli_default" not in wire


def test_record_refresh_claude_code_falls_back_when_sonnet_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The account-discovered default is kept when Sonnet is unavailable."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[{"id": "opus", "name": "Opus", "description": ""}],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            default_model="opus",
        )
    )
    assert wire["default_model"] == "opus"
    assert "cli_default" not in wire


def test_record_refresh_codex_has_only_one_discovered_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Codex follows its catalog default without synthetic alternatives."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[{"id": "gpt-5.6-sol", "name": "Sol", "description": ""}],
            source=model_discovery.CODEX_SOURCE,
            default_model="gpt-5.6-sol",
        )
    )
    assert wire["default_model"] == "gpt-5.6-sol"
    assert "cli_default" not in wire
    overlay = model_discovery.read_overlay()
    assert "cli_default" not in overlay["codex"]


# --------------------------------------------------------------------------- #
# resolve_cloud_api_key
# --------------------------------------------------------------------------- #


def test_resolve_cloud_api_key_uses_dedicated_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dedicated")
    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)
    assert model_discovery.resolve_cloud_api_key("openai") == "sk-dedicated"


def test_resolve_cloud_api_key_falls_back_to_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CLIO_LM_API_KEY", "sk-generic")
    assert model_discovery.resolve_cloud_api_key("openai") == "sk-generic"


def test_resolve_cloud_api_key_unknown_kind_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)
    assert model_discovery.resolve_cloud_api_key("lm_studio") == ""


def test_resolve_cloud_api_key_is_keyed_by_provider_id_not_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openrouter and nvidia_nim share the catalog kind "openai" with the literal
    OpenAI provider; resolving by kind previously gave both of them
    OPENAI_API_KEY (model-capabilities brief Part 3)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-literal")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "sk-nvidia")
    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)

    assert model_discovery.resolve_cloud_api_key("openrouter") == "sk-openrouter"
    assert model_discovery.resolve_cloud_api_key("nvidia_nim") == "sk-nvidia"
    # The literal OpenAI provider still resolves its own key.
    assert model_discovery.resolve_cloud_api_key("openai") == "sk-openai-literal"


def test_resolve_cloud_api_key_never_borrows_a_same_kind_siblings_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with no dedicated env var set, openrouter must not fall through to
    a same-kind sibling's key -- only to the generic CLIO_LM_API_KEY."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-literal")
    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)

    assert model_discovery.resolve_cloud_api_key("openrouter") == ""


# --------------------------------------------------------------------------- #
# discover_codex -- mocked at the maintained-catalog boundary
# (refresh_codex_catalog) and the credential-store sign-in boundary.
# --------------------------------------------------------------------------- #


class _StubCredentialStore:
    def __init__(self, signed_in: bool) -> None:
        self._signed_in = signed_in

    def is_signed_in(self) -> bool:
        return self._signed_in


def _codex_row(
    model_id: str,
    *,
    name: str = "",
    capabilities: list[str] | None = None,
    effort_levels: list[str] | None = None,
) -> dict[str, Any]:
    """One validated maintained-catalog row (the ``CodexCatalog.models`` shape)."""

    return {
        "id": model_id,
        "name": name or model_id,
        "context_window": 272000,
        "max_output_tokens": 128000,
        "reasoning": True,
        "effort_levels": ["low", "medium", "high"] if effort_levels is None else effort_levels,
        "capabilities": ["text", "image"] if capabilities is None else capabilities,
    }


def _stub_codex_catalog(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
    default_model: str = "",
    error: Exception | None = None,
) -> None:
    from clio_agent.providers.model_discovery import codex as md_codex
    from clio_agent.providers.model_discovery.codex_catalog import CodexCatalog

    def _refresh() -> CodexCatalog:
        if error is not None:
            raise error
        return CodexCatalog(models=rows, default_model=default_model)

    monkeypatch.setattr(md_codex, "refresh_codex_catalog", _refresh)


def test_discover_codex_success_reports_default_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_codex_catalog(
        monkeypatch,
        [_codex_row("gpt-5.6-sol", name="GPT-5.6-Sol"), _codex_row("gpt-5.6-terra")],
        default_model="gpt-5.6-sol",
    )

    result = model_discovery.discover_codex(credential_store=_StubCredentialStore(True))

    assert result.failed_reason is None
    assert result.source == model_discovery.CODEX_SOURCE == "codex_catalog"
    assert result.provider == "codex"
    assert {m["id"] for m in result.discovered} == {"gpt-5.6-sol", "gpt-5.6-terra"}
    assert result.default_model == "gpt-5.6-sol"
    assert all(m["capabilities"] == ["text", "image"] for m in result.discovered)
    assert all(
        m["capability_evidence"]["reason"] == "modality_cataloged" for m in result.discovered
    )
    # The catalog's per-model efforts are persisted in the field names the
    # handshake/reasoning_levels readers already consume.
    assert all(
        m["supported_reasoning_efforts"] == ["low", "medium", "high"] for m in result.discovered
    )
    assert all(m["default_reasoning_effort"] == "medium" for m in result.discovered)


def test_discover_codex_does_not_guess_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_codex_catalog(monkeypatch, [_codex_row("gpt-5.6-sol")], default_model="")

    result = model_discovery.discover_codex(credential_store=_StubCredentialStore(True))

    assert [model["id"] for model in result.discovered] == ["gpt-5.6-sol"]
    assert result.default_model == ""


def test_discover_codex_text_only_row_is_not_widened(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model the catalog declares text-only stays text-only, and says why."""

    _stub_codex_catalog(monkeypatch, [_codex_row("gpt-5.6-mini", capabilities=["text"])])

    result = model_discovery.discover_codex(credential_store=_StubCredentialStore(True))

    assert result.discovered[0]["capabilities"] == ["text"]
    assert result.discovered[0]["capability_evidence"]["reason"] == "modality_cataloged"


def test_discover_codex_default_effort_without_medium_is_the_first_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_codex_catalog(monkeypatch, [_codex_row("gpt-5.6-sol", effort_levels=["high", "xhigh"])])

    result = model_discovery.discover_codex(credential_store=_StubCredentialStore(True))

    assert result.discovered[0]["supported_reasoning_efforts"] == ["high", "xhigh"]
    assert result.discovered[0]["default_reasoning_effort"] == "high"


def test_discover_codex_requires_a_signed_in_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sign-in is a typed failure -- never a catalog served as account evidence."""

    _stub_codex_catalog(monkeypatch, [_codex_row("gpt-5.6-sol")], default_model="gpt-5.6-sol")

    result = model_discovery.discover_codex(credential_store=_StubCredentialStore(False))

    assert result.discovered == []
    assert "sign-in is required" in (result.failed_reason or "")


def test_discover_codex_catalog_error_is_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.model_discovery.codex_catalog import CodexCatalogError

    _stub_codex_catalog(
        monkeypatch, [], error=CodexCatalogError("Could not fetch the Codex model catalog")
    )

    result = model_discovery.discover_codex(credential_store=_StubCredentialStore(True))

    assert result.discovered == []
    assert "Could not fetch the Codex model catalog" in (result.failed_reason or "")


def test_discover_codex_zero_models_is_typed_reason() -> None:
    result = model_discovery.discover_codex(
        credential_store=_StubCredentialStore(True), catalog_candidates=[]
    )

    assert result.discovered == []
    assert result.failed_reason is not None


# --------------------------------------------------------------------------- #
# discover_claude_code -- mocked at the maintained-catalog boundary
# (refresh_claude_code_catalog) and the CLI sign-in boundary (subprocess.run).
# Per owner ruling, model existence/capabilities/defaults come ONLY from the
# catalog; the CLI is consulted for exactly one thing: `auth status`.
# --------------------------------------------------------------------------- #


def _catalog(*, default_model: str = "sonnet", default_model_reason: str = "") -> Any:
    from clio_agent.providers.model_discovery.claude_code_catalog import ClaudeCodeCatalog

    return ClaudeCodeCatalog(
        models=[
            {
                "id": alias,
                "name": alias.title(),
                "capabilities": ["text", "image", "pdf"],
                "capability_evidence": {
                    "source": "claude_code_catalog",
                    "reason": "modality_cataloged",
                },
            }
            for alias in ("fable", "sonnet", "haiku")
        ],
        default_model=default_model,
        default_model_reason=default_model_reason,
    )


def _fake_auth_status_run(*, logged_in: bool = True, stdout: str | None = None) -> Any:
    """A fake ``subprocess.run`` for the ``auth status`` sign-in check."""

    def _run(args: list[str], **kwargs: Any) -> Any:
        payload = (
            stdout
            if stdout is not None
            else json.dumps(
                {"loggedIn": logged_in, "authMethod": "claude.ai", "apiProvider": "firstParty"}
            )
        )
        return SimpleNamespace(stdout=payload, stderr="", returncode=0)

    return _run


def test_discover_claude_code_signed_in_reports_catalog_capabilities_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", lambda: _catalog())
    monkeypatch.setattr(md_claude_code.subprocess, "run", _fake_auth_status_run())

    result = model_discovery.discover_claude_code(timeout=5.0)

    assert result.failed_reason is None
    assert {m["id"] for m in result.discovered} == {"fable", "sonnet", "haiku"}
    assert result.default_model == "sonnet"
    assert result.default_model_reason == ""
    assert all(sorted(m["capabilities"]) == ["image", "pdf", "text"] for m in result.discovered)
    assert all(
        m["capability_evidence"]["reason"] == "modality_cataloged" for m in result.discovered
    )


def test_discover_claude_code_catalog_without_default_reports_typed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(
        md_claude_code,
        "refresh_claude_code_catalog",
        lambda: _catalog(
            default_model="", default_model_reason="the maintained catalog names no default model"
        ),
    )
    monkeypatch.setattr(md_claude_code.subprocess, "run", _fake_auth_status_run())

    result = model_discovery.discover_claude_code(timeout=5.0)

    assert result.failed_reason is None
    assert result.default_model == ""
    assert result.default_model_reason == "the maintained catalog names no default model"


def test_discover_claude_code_catalog_fetch_failure_is_typed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.providers.model_discovery.claude_code_catalog import ClaudeCodeCatalogError

    def _boom() -> Any:
        raise ClaudeCodeCatalogError("Could not fetch Claude Code model catalog: boom")

    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", _boom)
    result = model_discovery.discover_claude_code(timeout=5.0)
    assert result.discovered == []
    assert "Could not fetch" in (result.failed_reason or "")


def test_discover_claude_code_cli_unavailable_is_typed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", lambda: _catalog())

    def _boom() -> str:
        raise model_discovery.ClaudeCodeCLIUnavailableError("claude not on PATH")

    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", _boom)
    result = model_discovery.discover_claude_code()
    assert result.discovered == []
    assert "claude not on PATH" in (result.failed_reason or "")


def test_discover_claude_code_never_invokes_a_model_probe_only_auth_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE-sensitive: discovery calls the CLI EXACTLY once, with EXACTLY
    ``[binary, "auth", "status"]`` -- never a per-model or bare-default probe."""
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", lambda: _catalog())

    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: Any) -> Any:
        calls.append(list(args))
        return SimpleNamespace(
            stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(md_claude_code.subprocess, "run", _run)

    result = model_discovery.discover_claude_code(timeout=5.0)

    assert result.failed_reason is None
    assert calls == [["claude", "auth", "status"]]


def test_discover_claude_code_not_logged_in_is_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", lambda: _catalog())
    monkeypatch.setattr(md_claude_code.subprocess, "run", _fake_auth_status_run(logged_in=False))

    result = model_discovery.discover_claude_code(timeout=5.0)

    assert result.discovered == []
    assert "not signed in" in (result.failed_reason or "")
    assert "claude auth login" in (result.failed_reason or "")


def test_discover_claude_code_auth_status_missing_logged_in_key_is_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``loggedIn`` absent (not merely false) is ALSO not signed in -- never assumed."""
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", lambda: _catalog())
    monkeypatch.setattr(
        md_claude_code.subprocess,
        "run",
        _fake_auth_status_run(stdout=json.dumps({"authMethod": "claude.ai"})),
    )

    result = model_discovery.discover_claude_code(timeout=5.0)

    assert result.discovered == []
    assert "not signed in" in (result.failed_reason or "")


def test_discover_claude_code_auth_status_non_json_is_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", lambda: _catalog())
    monkeypatch.setattr(
        md_claude_code.subprocess, "run", _fake_auth_status_run(stdout="not json at all")
    )

    result = model_discovery.discover_claude_code(timeout=5.0)

    assert result.discovered == []
    assert "non-JSON" in (result.failed_reason or "")


def test_discover_claude_code_auth_status_timeout_is_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess as real_subprocess

    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", lambda: _catalog())

    def _run(args: list[str], **kwargs: Any) -> Any:
        raise real_subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 5.0))

    monkeypatch.setattr(md_claude_code.subprocess, "run", _run)

    result = model_discovery.discover_claude_code(timeout=5.0)

    assert result.discovered == []
    assert "timed out" in (result.failed_reason or "")


def test_discover_claude_code_auth_status_oserror_is_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", lambda: _catalog())

    def _run(args: list[str], **kwargs: Any) -> Any:
        raise OSError("no such file or directory")

    monkeypatch.setattr(md_claude_code.subprocess, "run", _run)

    result = model_discovery.discover_claude_code(timeout=5.0)

    assert result.discovered == []
    assert "failed to launch" in (result.failed_reason or "")


def test_discover_claude_code_auth_status_failure_keeps_prior_overlay_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed sign-in check must not clear the overlay's prior good list --
    the same no-silent-fallback contract a catalog-fetch failure gets."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[
                {"id": "sonnet", "name": "Sonnet", "description": ""},
                {"id": "opus", "name": "Opus", "description": ""},
            ],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            default_model="sonnet",
        )
    )

    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", lambda: _catalog())
    monkeypatch.setattr(md_claude_code.subprocess, "run", _fake_auth_status_run(logged_in=False))

    result = model_discovery.discover_claude_code(timeout=5.0)
    assert result.discovered == []
    assert result.failed_reason is not None

    wire = model_discovery.record_refresh(result)
    assert wire["removed"] == []
    assert set(wire["unchanged"]) == {"sonnet", "opus"}
    overlay = model_discovery.read_overlay()
    assert {m["id"] for m in overlay["claude_code"]["models"]} == {"sonnet", "opus"}
    assert overlay["claude_code"]["failed_reason"] == result.failed_reason


def test_discover_claude_code_explicit_candidates_bypass_the_network_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostic callers can bypass the network catalog with an explicit id list;
    an id that bypassed the catalog carries no evidenced non-text capability."""
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(md_claude_code.subprocess, "run", _fake_auth_status_run())

    def _boom() -> Any:
        raise AssertionError("must not fetch the network catalog when candidates is given")

    monkeypatch.setattr(md_claude_code, "refresh_claude_code_catalog", _boom)

    result = model_discovery.discover_claude_code(candidates=("haiku",), timeout=5.0)

    assert result.failed_reason is None
    assert [m["id"] for m in result.discovered] == ["haiku"]
    assert result.discovered[0]["capabilities"] == ["text"]
    evidence = result.discovered[0]["capability_evidence"]
    assert evidence["reason"] == "modality_uncataloged"
    assert result.default_model == ""


# --------------------------------------------------------------------------- #
# discover_http -- mocked run_handshake (the SAME live path GET .../models uses).
# --------------------------------------------------------------------------- #


class _FakeHandshakeReport:
    def __init__(self, *, models: list[dict[str, Any]], error: str | None = None) -> None:
        self._models = models
        self.error = error
        self.connectivity = SimpleNamespace(value="ok" if models else "unreachable")
        self.auth = SimpleNamespace(value="ok" if models else "missing")

    def to_models_wire(self) -> dict[str, Any]:
        return {"models": self._models, "source": "live", "error": self.error}


async def test_discover_http_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run_handshake(ctx: Any, *, force: bool = False) -> Any:
        assert force is True  # a refresh must bypass the handshake TTL cache
        return _FakeHandshakeReport(models=[{"id": "gpt-4o", "name": "gpt-4o"}])

    monkeypatch.setattr("clio_agent.providers.handshake.run_handshake", _fake_run_handshake)
    preset = get_provider("openai")
    assert preset is not None
    result = await model_discovery.discover_http(preset, api_key="sk-test")
    assert result.failed_reason is None
    assert result.discovered == [{"id": "gpt-4o", "name": "gpt-4o", "description": ""}]


async def test_discover_http_no_models_is_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run_handshake(ctx: Any, *, force: bool = False) -> Any:
        return _FakeHandshakeReport(models=[], error="connection refused")

    monkeypatch.setattr("clio_agent.providers.handshake.run_handshake", _fake_run_handshake)
    preset = get_provider("openai")
    assert preset is not None
    result = await model_discovery.discover_http(preset, api_key="")
    assert result.discovered == []
    assert result.failed_reason == "connection refused"


# --------------------------------------------------------------------------- #
# is_provider_configured (#1211 review R2).
# --------------------------------------------------------------------------- #


def test_is_provider_configured_codex_requires_a_signed_in_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.providers.codex.credentials import CodexCredentialStore

    preset = get_provider("codex")
    assert preset is not None
    monkeypatch.setattr(CodexCredentialStore, "is_signed_in", lambda self: False)
    assert model_discovery.is_provider_configured(preset) is False
    monkeypatch.setattr(CodexCredentialStore, "is_signed_in", lambda self: True)
    assert model_discovery.is_provider_configured(preset) is True


def test_is_provider_configured_cloud_needs_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    preset = get_provider("openai")
    assert preset is not None
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)
    assert model_discovery.is_provider_configured(preset) is False
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert model_discovery.is_provider_configured(preset) is True


def test_is_provider_configured_local_no_auth_always_true() -> None:
    preset = get_provider("lm_studio")
    assert preset is not None
    assert model_discovery.is_provider_configured(preset) is True


def test_is_provider_configured_argonne_needs_token(monkeypatch: pytest.MonkeyPatch) -> None:
    preset = get_provider("argonne_sophia")
    assert preset is not None
    from clio_agent.providers import argonne_auth

    monkeypatch.setattr(argonne_auth, "tokens_exist", lambda: False)
    assert model_discovery.is_provider_configured(preset) is False
    monkeypatch.setattr(argonne_auth, "tokens_exist", lambda: True)
    assert model_discovery.is_provider_configured(preset) is True


# --------------------------------------------------------------------------- #
# refresh_all -- one provider failing must not block the others (#1211 spec);
# configured-only filtering + explicit presets bypass it (#1211 review R2/R3).
# --------------------------------------------------------------------------- #


async def test_refresh_all_one_provider_failing_others_still_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))

    def _fake_discover_codex() -> model_discovery.ProviderDiscoveryResult:
        return model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[{"id": "gpt-5.6-sol", "name": "Sol", "description": ""}],
            source=model_discovery.CODEX_SOURCE,
            default_model="gpt-5.6-sol",
        )

    async def _fake_discover_http(
        preset: Any, *, api_key: str
    ) -> model_discovery.ProviderDiscoveryResult:
        if preset.id == "openai":
            return model_discovery.ProviderDiscoveryResult(
                provider="openai",
                discovered=[{"id": "gpt-4o", "name": "gpt-4o", "description": ""}],
                source=model_discovery.HTTP_SOURCE,
            )
        return model_discovery.ProviderDiscoveryResult(
            provider=preset.id,
            discovered=[],
            source=model_discovery.HTTP_SOURCE,
            failed_reason="simulated network failure",
        )

    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.refresh.discover_codex", _fake_discover_codex
    )
    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.refresh.discover_http", _fake_discover_http
    )

    presets = [get_provider("codex"), get_provider("openai"), get_provider("anthropic")]
    results = await model_discovery.refresh_all(presets=presets)  # type: ignore[arg-type]

    by_id = {r["provider"]: r for r in results}
    assert "failed_reason" not in by_id["codex"]
    assert by_id["codex"]["added"] == ["gpt-5.6-sol"]
    assert "failed_reason" not in by_id["openai"]
    assert by_id["openai"]["added"] == ["gpt-4o"]
    assert by_id["anthropic"]["failed_reason"] == "simulated network failure"
    assert by_id["anthropic"]["added"] == []
    assert by_id["anthropic"]["discovered"] == []


async def test_refresh_all_default_scan_filters_to_configured_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review R2: with no explicit presets, only configured providers are probed."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    # Codex counts as configured only with a signed-in credential; pin it
    # instead of depending on the host's real Codex sign-in.
    from clio_agent.providers.codex.credentials import CodexCredentialStore

    monkeypatch.setattr(CodexCredentialStore, "is_signed_in", lambda self: True)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)

    def _claude_boom() -> str:
        raise model_discovery.ClaudeCodeCLIUnavailableError("no claude")

    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", _claude_boom)

    seen: list[str] = []

    async def _fake_discover_http(
        preset: Any, *, api_key: str
    ) -> model_discovery.ProviderDiscoveryResult:
        seen.append(preset.id)
        return model_discovery.ProviderDiscoveryResult(
            provider=preset.id,
            discovered=[{"id": "m", "name": "m", "description": ""}],
            source=model_discovery.HTTP_SOURCE,
        )

    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.refresh.discover_http", _fake_discover_http
    )
    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.refresh.discover_codex",
        lambda: (
            seen.append("codex")
            or model_discovery.ProviderDiscoveryResult(
                provider="codex",
                discovered=[{"id": "gpt-5.6-luna", "name": "Luna", "description": ""}],
                source=model_discovery.CODEX_SOURCE,
                default_model="gpt-5.6-luna",
            )
        ),
    )

    await model_discovery.refresh_all()

    # The signed-in Codex credential is configured; Claude Code and API-key
    # providers are not.
    assert "codex" in seen
    assert "claude_code" not in seen
    assert "openai" not in seen
    assert "anthropic" not in seen
    # Local/no-auth kinds still are.
    assert "lm_studio" in seen
    assert "ollama" in seen


async def test_refresh_all_explicit_presets_bypass_the_configured_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review R3: an explicit {"providers": [...]} list is honored verbatim."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)

    async def _fake_discover_http(
        preset: Any, *, api_key: str
    ) -> model_discovery.ProviderDiscoveryResult:
        return model_discovery.ProviderDiscoveryResult(
            provider=preset.id,
            discovered=[],
            source=model_discovery.HTTP_SOURCE,
            failed_reason="no api key",
        )

    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.refresh.discover_http", _fake_discover_http
    )

    results = await model_discovery.refresh_all(presets=[get_provider("openai")])  # type: ignore[list-item]
    assert [r["provider"] for r in results] == ["openai"]


async def test_refresh_all_bounds_a_wedged_provider_to_the_per_provider_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review R2/R3: a wedged provider is capped, never hangs the whole refresh."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.refresh.REFRESH_PER_PROVIDER_DEADLINE_S", 0.05
    )

    import asyncio as _asyncio

    async def _hang(preset: Any, *, api_key: str) -> model_discovery.ProviderDiscoveryResult:
        await _asyncio.sleep(10)
        raise AssertionError("should have been cancelled by the deadline")

    monkeypatch.setattr("clio_agent.providers.model_discovery.refresh.discover_http", _hang)

    results = await model_discovery.refresh_all(presets=[get_provider("openai")])  # type: ignore[list-item]
    assert len(results) == 1
    assert "timed out" in (results[0].get("failed_reason") or "")


async def test_refresh_all_one_providers_malformed_overlay_write_does_not_discard_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review N2: record_refresh failures are caught PER PROVIDER -- one
    provider's OverlayMalformedError must not discard the OTHER providers'
    already-recorded results, and the affected row is itself typed."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))

    async def _fake_discover_http(
        preset: Any, *, api_key: str
    ) -> model_discovery.ProviderDiscoveryResult:
        return model_discovery.ProviderDiscoveryResult(
            provider=preset.id,
            discovered=[{"id": "m", "name": "m", "description": ""}],
            source=model_discovery.HTTP_SOURCE,
        )

    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.refresh.discover_http", _fake_discover_http
    )

    calls = {"n": 0}
    real_record_refresh = model_discovery.record_refresh

    def _flaky_record_refresh(result: model_discovery.ProviderDiscoveryResult) -> dict[str, Any]:
        calls["n"] += 1
        if result.provider == "anthropic":
            raise model_discovery.OverlayMalformedError("simulated corruption")
        return real_record_refresh(result)

    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.refresh.record_refresh", _flaky_record_refresh
    )

    presets = [get_provider("openai"), get_provider("anthropic")]
    results = await model_discovery.refresh_all(presets=presets)  # type: ignore[arg-type]

    by_id = {r["provider"]: r for r in results}
    # The sibling provider's result SURVIVES -- not discarded by the other's failure.
    assert by_id["openai"]["added"] == ["m"]
    assert "failed_reason" not in by_id["openai"]
    # The affected provider gets a typed, informative reason of its own.
    assert "overlay_malformed" in by_id["anthropic"]["failed_reason"]
    assert "simulated corruption" in by_id["anthropic"]["failed_reason"]


def test_record_refresh_writes_use_a_unique_temp_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review R7: the write temp filename is uuid-suffixed so two
    concurrent refreshes never collide on the same tmp file."""
    overlay_file = tmp_path / "overlay.json"
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))
    seen_tmp_names: list[str] = []
    real_write_text = Path.write_text

    def _spy_write_text(self: Path, *a: Any, **kw: Any) -> int:
        if self.name.endswith(".tmp"):
            seen_tmp_names.append(self.name)
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _spy_write_text)

    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex", discovered=[{"id": "a", "name": "a", "description": ""}], source="x"
        )
    )
    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex", discovered=[{"id": "b", "name": "b", "description": ""}], source="x"
        )
    )
    assert len(seen_tmp_names) == 2
    assert seen_tmp_names[0] != seen_tmp_names[1]  # never the same literal ".tmp" name
    assert overlay_file.with_suffix(".tmp").name not in seen_tmp_names  # not the bare old name


# --------------------------------------------------------------------------- #
# the refresh_provider_models agent tool (#1211 review R6).
# --------------------------------------------------------------------------- #


def test_build_refresh_provider_models_tool_shape() -> None:
    tool = model_discovery.build_refresh_provider_models_tool()
    assert tool.name == "refresh_provider_models"
    assert tool.args == {}


def test_refresh_provider_models_tool_calls_refresh_all(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_refresh_all(
        presets: Any = None, *, only_configured: bool = True
    ) -> list[dict[str, Any]]:
        return [
            {
                "provider": "codex",
                "discovered": [],
                "source": "x",
                "default_model": "",
                "added": [],
                "removed": [],
                "unchanged": [],
            }
        ]

    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.refresh.refresh_all", _fake_refresh_all
    )
    tool = model_discovery.build_refresh_provider_models_tool()
    out = tool.func()
    assert out == {
        "results": [
            {
                "provider": "codex",
                "discovered": [],
                "source": "x",
                "default_model": "",
                "added": [],
                "removed": [],
                "unchanged": [],
            }
        ]
    }


# --------------------------------------------------------------------------- #
# live: actually invoke the configured provider SDK/CLI (CLIO_RUN_LIVE=1 only).
# --------------------------------------------------------------------------- #


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live Codex discovery: set CLIO_RUN_LIVE=1 (needs an existing Codex sign-in)",
)
def test_discover_codex_live() -> None:
    """Real maintained-catalog fetch + the real credential store -- no LM cost."""
    result = model_discovery.discover_codex()
    assert result.failed_reason is None, result.failed_reason
    assert result.discovered, "the maintained Codex catalog returned zero models"


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live claude_code discovery: set CLIO_RUN_LIVE=1 (needs `claude` on PATH + "
    "`claude auth login`; no LM cost -- a real GitHub catalog fetch plus one "
    "`claude auth status` call, never a model probe)",
)
def test_discover_claude_code_live_reads_catalog_and_checks_sign_in() -> None:
    """Real GitHub catalog fetch + one real ``claude auth status`` call."""
    result = model_discovery.discover_claude_code(timeout=30.0)
    assert result.failed_reason is None, result.failed_reason
    assert result.discovered, "the maintained catalog returned zero models"
    # The catalog is the sole source of capabilities -- every row must carry
    # a typed evidence record naming which catalog reason produced it.
    for model in result.discovered:
        assert "text" in model["capabilities"]
        assert model["capability_evidence"]["reason"] in {
            "modality_cataloged",
            "modality_uncataloged",
        }


# --------------------------------------------------------------------------- #
# Overlay staleness: a cache is an accelerator, never truth (AF-IMG F4).
# --------------------------------------------------------------------------- #


def _write_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: dict[str, Any]) -> None:
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text(json.dumps({"codex": entry}), encoding="utf-8")
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))


def test_overlay_within_ttl_is_served_without_a_staleness_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import datetime, timezone

    _write_overlay(
        tmp_path,
        monkeypatch,
        {
            "models": [{"id": "gpt-5.6-sol"}],
            "source": "codex_catalog",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    wire = model_discovery.overlay_models_wire("codex", "codex")
    assert wire is not None
    assert "staleness" not in wire


def test_overlay_older_than_ttl_is_still_served_but_marked_typed_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sticky cache: nothing ever re-examined an entry once written."""

    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _write_overlay(
        tmp_path,
        monkeypatch,
        {"models": [{"id": "gpt-5.6-sol"}], "source": "codex_catalog", "generated_at": old},
    )

    wire = model_discovery.overlay_models_wire("codex", "codex")

    assert wire is not None
    # Still SERVED -- the documented contract never clears a prior good list.
    assert [m["id"] for m in wire["models"]] == ["gpt-5.6-sol"]
    assert wire["staleness"]["reason"] == "overlay_age_exceeded_ttl"
    assert wire["staleness"]["ttl_s"] == model_discovery.overlay_staleness_ttl_s()
    assert wire["staleness"]["age_s"] > wire["staleness"]["ttl_s"]
    assert wire["staleness"]["description"]


def test_overlay_ttl_zero_disables_the_age_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import datetime, timedelta, timezone

    from tests._config_layer import set_config

    old = (datetime.now(timezone.utc) - timedelta(days=3650)).isoformat()
    _write_overlay(
        tmp_path,
        monkeypatch,
        {"models": [{"id": "gpt-5.6-sol"}], "source": "codex_catalog", "generated_at": old},
    )
    set_config("providers.model_catalog_ttl_s", 0)
    wire = model_discovery.overlay_models_wire("codex", "codex")
    assert wire is not None
    assert "staleness" not in wire


def test_overlay_with_unreadable_timestamp_is_unverified_not_assumed_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_overlay(
        tmp_path,
        monkeypatch,
        {
            "models": [{"id": "gpt-5.6-sol"}],
            "source": "codex_catalog",
            "generated_at": "whenever",
        },
    )
    wire = model_discovery.overlay_models_wire("codex", "codex")
    assert wire is not None
    assert wire["staleness"]["reason"] == "overlay_generated_at_unreadable"


def test_a_failed_refresh_keeps_the_prior_list_and_marks_it_typed_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The retained list is PRIOR evidence -- it must never be served as fresh."""

    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[{"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol", "description": ""}],
            source=model_discovery.CODEX_SOURCE,
            default_model="gpt-5.6-sol",
        )
    )

    failed_row = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=model_discovery.CODEX_SOURCE,
            failed_reason="Could not fetch the Codex model catalog: transport closed",
        )
    )

    assert [m["id"] for m in failed_row["discovered"]] == ["gpt-5.6-sol"]
    assert failed_row["staleness"]["reason"] == "overlay_refresh_failed"
    assert "transport closed" in failed_row["staleness"]["failed_reason"]
    # And every later READ of that entry keeps saying so.
    wire = model_discovery.overlay_models_wire("codex", "codex")
    assert wire is not None
    assert wire["staleness"]["reason"] == "overlay_refresh_failed"


def test_an_uncatalogued_staleness_reason_is_refused() -> None:
    from clio_agent.providers.model_discovery import overlay as md_overlay

    with pytest.raises(ValueError, match="Unknown overlay staleness reason"):
        md_overlay._staleness_reason("silently_probably_fine")


def test_discover_claude_code_attaches_cli_effort_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-model effort levels come from the CLI initialize model list (real shape)."""
    from clio_agent.providers.model_discovery import claude_code_effort
    from clio_agent.providers.model_discovery.claude_code_catalog import ClaudeCodeCatalog

    cli_models = [
        {
            "value": "sonnet",
            "resolvedModel": "claude-sonnet-5",
            "supportsEffort": True,
            "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
            "supportsAdaptiveThinking": True,
        },
        {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001"},
        {
            "value": "claude-fable-5-1[1m]",
            "resolvedModel": "claude-fable-5-1",
            "supportsEffort": True,
            "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        },
    ]
    monkeypatch.setattr(
        claude_code_effort, "read_cli_model_catalog", lambda *_a, **_k: (cli_models, "")
    )
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(md_claude_code.subprocess, "run", _fake_auth_status_run())
    ids = ("claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-fable-5-1")
    monkeypatch.setattr(
        md_claude_code,
        "refresh_claude_code_catalog",
        lambda: ClaudeCodeCatalog(
            models=[
                {"id": model_id, "name": model_id, "capabilities": ["text"]} for model_id in ids
            ],
            default_model="claude-sonnet-5",
            default_model_reason="",
        ),
    )

    result = model_discovery.discover_claude_code(timeout=5.0)
    rows = {row["id"]: row for row in result.discovered}

    assert rows["claude-sonnet-5"]["supported_effort_levels"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert rows["claude-sonnet-5"]["cli_values"] == ["sonnet"]
    assert rows["claude-fable-5-1"]["cli_values"] == ["claude-fable-5-1[1m]"]
    assert rows["claude-haiku-4-5-20251001"]["supported_effort_levels"] == []
    assert "effort_evidence_failure" not in rows["claude-sonnet-5"]


def test_failed_cli_effort_read_is_typed_on_every_row() -> None:
    from clio_agent.providers.model_discovery.claude_code_effort import attach_effort_levels

    failure = "claude_code_cli_model_catalog_unavailable: x"
    rows = attach_effort_levels([{"id": "claude-sonnet-5"}], [], failure)
    assert rows[0]["effort_evidence_failure"] == failure
    assert "supported_effort_levels" not in rows[0]
