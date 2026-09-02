"""Tests for the #1211 model-catalog refresh overlay + discovery mechanisms.

Unit-level: the overlay read/write/delta machinery, and each ``discover_*``
mechanism mocked at its CLI/network boundary (never a real subprocess or HTTP
call here). An autouse fixture stubs the context/output-limit resolution
(``attach_context_limits`` — #1211 review D4) every ``discover_*`` success path
now runs, so this file never touches models.dev/litellm/the local DB — that
cascade has its own tests in ``tests/test_providers/test_handshake_sources.py``.
Two ``@pytest.mark.live`` tests at the bottom actually invoke the installed
``codex``/``claude`` binaries — gated behind ``CLIO_RUN_LIVE=1`` like every
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
                    "source": "codex_sdk",
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
    assert wire["source"] == "codex_sdk"
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
        source="codex_sdk",
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
                {"id": "gpt-5.5-codex", "name": "5.5-codex", "description": ""},
            ],
            source="codex_sdk",
        )
    )
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[
                {"id": "gpt-5.5", "name": "5.5", "description": ""},
                {"id": "gpt-5.6-sol", "name": "Sol", "description": ""},
            ],
            source="codex_sdk",
        )
    )
    assert wire["added"] == ["gpt-5.6-sol"]
    assert wire["removed"] == ["gpt-5.5-codex"]
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
        source="codex_sdk",
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
        provider="codex", discovered=[], source="codex_sdk"
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


def test_record_refresh_claude_code_served_default_is_cost_policy_not_cli_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Owner ruling 2026-08-14 (failing-first): the CLI's own bare default can
    resolve to the premium ``fable`` tier; the overlay's SERVED default_model
    for claude_code must be the cost-policy value (``sonnet``) instead, with
    the CLI's honest choice preserved alongside it as ``cli_default`` (never
    dropped) -- both in the write (the persisted overlay) and the returned
    wire row (the /update-models delta)."""
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
    assert wire["default_model"] == "sonnet"
    assert wire["cli_default"] == "fable"
    overlay = model_discovery.read_overlay()
    assert overlay["claude_code"]["default_model"] == "sonnet"
    assert overlay["claude_code"]["cli_default"] == "fable"
    assert model_discovery.overlay_default_model("claude_code", "claude_code") == "sonnet"


def test_record_refresh_claude_code_keeps_cli_default_when_it_already_is_sonnet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the CLI's own default already IS the cost-policy model, cli_default
    and default_model agree -- no surprising divergence for the common case."""
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
    assert wire["cli_default"] == "sonnet"


def test_record_refresh_claude_code_falls_back_when_sonnet_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cost policy never points the default at a model the account doesn't
    actually serve: if ``sonnet`` never validated for this account, the
    overlay's served default_model stays the CLI's own (best-available)
    choice rather than a broken override."""
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
    assert wire["cli_default"] == "opus"


def test_record_refresh_codex_never_gains_a_cli_default_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cost policy (and its cli_default bookkeeping) is claude_code-only --
    codex keeps following its own account default verbatim, no cli_default
    key at all (#1211 review, owner ruling 2026-08-14)."""
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


# --------------------------------------------------------------------------- #
# discover_codex -- mocked at the official Python SDK boundary.
# --------------------------------------------------------------------------- #


class _StubCodex:
    def __init__(self, rows: list[Any] | None = None, error: Exception | None = None) -> None:
        self._rows = rows or []
        self._error = error
        self.closed = False

    async def __aenter__(self) -> _StubCodex:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.closed = True

    async def models(self) -> Any:
        if self._error is not None:
            raise self._error
        return SimpleNamespace(data=self._rows)


def _codex_model(
    model_id: str,
    *,
    name: str = "",
    description: str = "",
    is_default: bool = False,
    input_modalities: list[str] | None = None,
) -> Any:
    return SimpleNamespace(
        id=model_id,
        display_name=name or model_id,
        description=description,
        is_default=is_default,
        input_modalities=input_modalities or ["text", "image"],
    )


def test_discover_codex_success_reports_default_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from openai_codex.generated.v2_all import InputModality

    from clio_agent.providers.model_discovery import codex as md_codex

    stub = _StubCodex(
        rows=[
            _codex_model(
                "gpt-5.6-sol",
                name="GPT-5.6-Sol",
                description="d1",
                is_default=True,
                input_modalities=[InputModality.text, InputModality.image],
            ),
            _codex_model(
                "gpt-5.6-terra",
                name="GPT-5.6-Terra",
                description="d2",
                input_modalities=[InputModality.text, InputModality.image],
            ),
        ]
    )
    monkeypatch.setattr(md_codex, "AsyncCodex", lambda *_args, **_kwargs: stub)

    result = model_discovery.discover_codex()
    assert result.failed_reason is None
    assert result.source == model_discovery.CODEX_SOURCE
    assert {m["id"] for m in result.discovered} == {"gpt-5.6-sol", "gpt-5.6-terra"}
    assert result.default_model == "gpt-5.6-sol"
    assert result.source == "codex_sdk"
    assert all(m["capabilities"] == ["text", "image"] for m in result.discovered)
    assert stub.closed is True


def test_discover_codex_sdk_error_is_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.model_discovery import codex as md_codex

    stub = _StubCodex(error=RuntimeError("SDK transport closed"))
    monkeypatch.setattr(md_codex, "AsyncCodex", lambda *_args, **_kwargs: stub)
    result = model_discovery.discover_codex()
    assert result.discovered == []
    assert "SDK transport closed" in (result.failed_reason or "")
    assert stub.closed is True


def test_discover_codex_zero_models_is_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.model_discovery import codex as md_codex

    monkeypatch.setattr(md_codex, "AsyncCodex", lambda *_args, **_kwargs: _StubCodex(rows=[]))

    result = model_discovery.discover_codex()
    assert result.discovered == []
    assert result.failed_reason is not None


# --------------------------------------------------------------------------- #
# discover_claude_code -- mocked at the CLI boundary (subprocess.run + binary).
# --------------------------------------------------------------------------- #


def _fake_claude_run(responses: dict[str | None, dict[str, Any]]) -> Any:
    def _run(args: list[str], **_kw: Any) -> Any:
        alias = args[args.index("--model") + 1] if "--model" in args else None
        payload = responses[alias]
        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)

    return _run


def test_discover_claude_code_all_aliases_validate_default_follows_bare_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    responses = {
        None: {"is_error": False, "modelUsage": {"claude-fable-5": {}}},
        "fable": {"is_error": False, "modelUsage": {"claude-fable-5": {}}},
        "opus": {"is_error": False, "modelUsage": {"claude-opus-4-6-20251001": {}}},
        "sonnet": {"is_error": False, "modelUsage": {"claude-sonnet-4-6-20251001": {}}},
        "haiku": {"is_error": False, "modelUsage": {"claude-haiku-4-5-20251001": {}}},
    }
    monkeypatch.setattr(md_claude_code.subprocess, "run", _fake_claude_run(responses))

    result = model_discovery.discover_claude_code(timeout=5.0)
    assert result.failed_reason is None
    assert {m["id"] for m in result.discovered} == {"fable", "opus", "sonnet", "haiku"}
    # The bare (no --model) probe resolved to claude-fable-5, matching the "fable"
    # alias's own resolution -- the CLI's own default, not a guess.
    assert result.default_model == "fable"
    assert result.default_model_reason == ""
    assert result.rejected == []


def test_discover_claude_code_one_alias_rejected_others_still_validate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    responses: dict[str | None, dict[str, Any]] = {
        None: {"is_error": False, "modelUsage": {"claude-sonnet-4-6-20251001": {}}},
        "fable": {
            "is_error": True,
            "api_error_status": 404,
            "result": "There's an issue with the selected model (fable).",
        },
        "opus": {"is_error": False, "modelUsage": {"claude-opus-4-6-20251001": {}}},
        "sonnet": {"is_error": False, "modelUsage": {"claude-sonnet-4-6-20251001": {}}},
        "haiku": {"is_error": False, "modelUsage": {"claude-haiku-4-5-20251001": {}}},
    }
    monkeypatch.setattr(md_claude_code.subprocess, "run", _fake_claude_run(responses))

    result = model_discovery.discover_claude_code(timeout=5.0)
    assert result.failed_reason is None
    assert {m["id"] for m in result.discovered} == {"opus", "sonnet", "haiku"}
    assert result.rejected == [
        {"id": "fable", "reason": "There's an issue with the selected model (fable)."}
    ]
    assert result.default_model == "sonnet"


def test_discover_claude_code_every_alias_rejected_is_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    responses = {
        alias: {"is_error": True, "api_error_status": 404, "result": f"{alias} gone"}
        for alias in (None, "fable", "opus", "sonnet", "haiku")
    }
    monkeypatch.setattr(md_claude_code.subprocess, "run", _fake_claude_run(responses))

    result = model_discovery.discover_claude_code(timeout=5.0)
    assert result.discovered == []
    assert result.failed_reason is not None
    assert "fable" in result.failed_reason


def test_discover_claude_code_cli_unavailable_is_typed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> str:
        raise model_discovery.ClaudeCodeCLIUnavailableError("claude not on PATH")

    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", _boom)
    result = model_discovery.discover_claude_code()
    assert result.discovered == []
    assert "claude not on PATH" in (result.failed_reason or "")


def test_probe_claude_non_json_response_is_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(args: list[str], **_kw: Any) -> Any:
        return SimpleNamespace(stdout="not json at all", stderr="", returncode=1)

    monkeypatch.setattr(md_claude_code.subprocess, "run", _run)
    probe = md_claude_code._probe_claude("claude", "sonnet", timeout=5.0)
    assert probe["outcome"] == "inconclusive"
    assert "non-JSON" in probe["reason"]


def test_probe_claude_404_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1211 review D3: a 404 model-rejection envelope is the ONLY outcome that
    counts as a definitive rejection."""

    def _run(args: list[str], **_kw: Any) -> Any:
        payload = {"is_error": True, "api_error_status": 404, "result": "issue with the model"}
        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)

    monkeypatch.setattr(md_claude_code.subprocess, "run", _run)
    probe = md_claude_code._probe_claude("claude", "bogus", timeout=5.0)
    assert probe["outcome"] == "rejected"


@pytest.mark.parametrize("status", [429, 500, 503, None])
def test_probe_claude_non_404_error_status_is_inconclusive_not_rejected(
    monkeypatch: pytest.MonkeyPatch, status: int | None
) -> None:
    """#1211 review D3: transient noise (rate limit, server error, an
    unrecognised is_error shape) must NEVER be classified as a rejection."""

    def _run(args: list[str], **_kw: Any) -> Any:
        payload = {"is_error": True, "api_error_status": status, "result": "transient"}
        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)

    monkeypatch.setattr(md_claude_code.subprocess, "run", _run)
    probe = md_claude_code._probe_claude("claude", "sonnet", timeout=5.0)
    assert probe["outcome"] == "inconclusive"


def test_probe_claude_timeout_is_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as real_subprocess

    def _run(args: list[str], **kw: Any) -> Any:
        raise real_subprocess.TimeoutExpired(cmd=args, timeout=kw.get("timeout", 5.0))

    monkeypatch.setattr(md_claude_code.subprocess, "run", _run)
    probe = md_claude_code._probe_claude("claude", "sonnet", timeout=5.0)
    assert probe["outcome"] == "inconclusive"
    assert "timed out" in probe["reason"]


def test_discover_claude_code_alias_timeout_does_not_remove_it_keeps_prior_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review D3 failing-first: a timeout on ONE alias must NOT produce
    removed:[alias] -- the whole provider aborts with failed_reason instead,
    keeping the overlay's prior list untouched."""
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
    import subprocess as real_subprocess

    def _run(args: list[str], **kw: Any) -> Any:
        alias = args[args.index("--model") + 1] if "--model" in args else None
        if alias == "opus":
            raise real_subprocess.TimeoutExpired(cmd=args, timeout=kw.get("timeout", 5.0))
        payload = {"is_error": False, "modelUsage": {f"claude-{alias or 'default'}": {}}}
        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)

    monkeypatch.setattr(md_claude_code.subprocess, "run", _run)

    result = model_discovery.discover_claude_code(timeout=5.0)
    assert result.discovered == []
    assert result.failed_reason is not None
    assert "opus" in result.failed_reason

    wire = model_discovery.record_refresh(result)
    assert wire["removed"] == []  # "opus" must NOT be reported removed
    assert set(wire["unchanged"]) == {"sonnet", "opus"}  # the prior list, untouched


def test_discover_claude_code_bare_probe_timeout_aborts_provider_overlay_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1211 review D3 gap (claude_code.py:167-174): the BARE (no --model)
    probe itself being inconclusive must ALSO abort the whole provider with a
    typed failed_reason -- before any alias is even probed -- and the prior
    overlay entry must be left completely intact (never narrowed), mirroring
    the per-alias-timeout coverage above."""
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
    import subprocess as real_subprocess

    calls: list[str | None] = []

    def _run(args: list[str], **kw: Any) -> Any:
        alias = args[args.index("--model") + 1] if "--model" in args else None
        calls.append(alias)
        if alias is None:
            raise real_subprocess.TimeoutExpired(cmd=args, timeout=kw.get("timeout", 5.0))
        payload = {"is_error": False, "modelUsage": {f"claude-{alias}": {}}}
        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)

    monkeypatch.setattr(md_claude_code.subprocess, "run", _run)

    result = model_discovery.discover_claude_code(timeout=5.0)
    assert result.discovered == []
    assert result.failed_reason is not None
    assert "bare CLI-default probe inconclusive" in result.failed_reason
    # The loop never even started probing aliases -- only the bare call ran.
    assert calls == [None]

    wire = model_discovery.record_refresh(result)
    assert wire["removed"] == []
    assert wire["added"] == []
    assert set(wire["unchanged"]) == {"sonnet", "opus"}  # the prior list, fully intact
    overlay = model_discovery.read_overlay()
    assert {m["id"] for m in overlay["claude_code"]["models"]} == {"sonnet", "opus"}
    assert overlay["claude_code"]["failed_reason"] == result.failed_reason


def test_discover_claude_code_bare_probe_failure_falls_back_with_typed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1211 review N5: when the bare default-probe itself succeeds but its
    resolved id matches no validated alias, default_model falls back to the
    first validated alias and default_model_reason explains why."""
    monkeypatch.setattr(md_claude_code, "_resolve_claude_binary", lambda: "claude")
    responses = {
        None: {"is_error": False, "modelUsage": {"claude-unmatched-id": {}}},
        "fable": {"is_error": False, "modelUsage": {"claude-fable-5": {}}},
        "opus": {"is_error": False, "modelUsage": {"claude-opus-4-6-20251001": {}}},
        "sonnet": {"is_error": False, "modelUsage": {"claude-sonnet-4-6-20251001": {}}},
        "haiku": {"is_error": False, "modelUsage": {"claude-haiku-4-5-20251001": {}}},
    }
    monkeypatch.setattr(md_claude_code.subprocess, "run", _fake_claude_run(responses))

    result = model_discovery.discover_claude_code(timeout=5.0)
    assert result.failed_reason is None
    assert result.default_model == "fable"  # first validated alias, in candidate order
    assert result.default_model_reason != ""
    assert "no validated alias matched" in result.default_model_reason


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


def test_is_provider_configured_codex_uses_required_sdk() -> None:
    preset = get_provider("codex")
    assert preset is not None
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

    def _fake_discover_codex(*, timeout: float = 20.0) -> model_discovery.ProviderDiscoveryResult:
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
        lambda **_kwargs: (
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

    # The required Codex SDK is configured; Claude Code and API-key providers are not.
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
    reason="live Codex SDK probe: set CLIO_RUN_LIVE=1 (needs existing Codex authentication)",
)
def test_discover_codex_live() -> None:
    """Real official Codex Python SDK model-list call -- no LM cost."""
    result = model_discovery.discover_codex(timeout=30.0)
    assert result.failed_reason is None, result.failed_reason
    assert result.discovered, "codex model/list returned zero models"
    assert result.default_model


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live claude CLI probe: set CLIO_RUN_LIVE=1 (needs `claude` on PATH + `claude login`; "
    "billed API call)",
)
def test_discover_claude_code_live_single_alias() -> None:
    """Real ``claude -p`` probe -- bounded to ONE alias (a real billed call)."""
    result = model_discovery.discover_claude_code(candidates=("haiku",), timeout=60.0)
    assert result.failed_reason is None, result.failed_reason
    assert [m["id"] for m in result.discovered] == ["haiku"]
