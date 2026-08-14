"""Tests for the #1211 model-catalog refresh overlay + discovery mechanisms.

Unit-level: the overlay read/write/delta machinery, and each ``discover_*``
mechanism mocked at its CLI/network boundary (never a real subprocess or HTTP
call here). Two ``@pytest.mark.live`` tests at the bottom actually invoke the
installed ``codex``/``claude`` binaries — gated behind ``CLIO_RUN_LIVE=1`` like
every other live test in this suite (see ``tests/test_arc/test_live_plane_alcf.py``
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
from clio_agent.providers.codex_app_server import CodexAppServerError

# --------------------------------------------------------------------------- #
# overlay: path resolution, read, dual-key lookup.
# --------------------------------------------------------------------------- #


def test_overlay_path_honors_clio_model_catalog_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
                    "source": "codex_app_server",
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
    assert wire["source"] == "codex_app_server"
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
        source="codex_app_server",
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
            source="codex_app_server",
        )
    )
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[
                {"id": "gpt-5.5", "name": "5.5", "description": ""},
                {"id": "gpt-5.6-sol", "name": "Sol", "description": ""},
            ],
            source="codex_app_server",
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
    assert overlay["claude_code"]["models"] == [{"id": "sonnet", "name": "Sonnet", "description": ""}]
    assert overlay["claude_code"]["failed_reason"] == "claude CLI not found on PATH"


def test_record_refresh_failure_with_no_previous_entry_stays_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    wire = model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="openrouter", discovered=[], source="live_handshake", failed_reason="no api key"
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
        provider="codex", discovered=[{"id": "x", "name": "x", "description": ""}], source="codex_app_server"
    )
    with pytest.raises(model_discovery.OverlayMalformedError):
        model_discovery.record_refresh(result)
    # The corrupt file is left exactly as it was -- never silently overwritten.
    assert overlay_file.read_text(encoding="utf-8") == "{not valid json"


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
# discover_codex -- mocked at the CLI boundary (the app-server pool + binary).
# --------------------------------------------------------------------------- #


class _StubCodexProcess:
    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self._rows = rows or []
        self._error = error

    def list_models(self, *, timeout: float) -> list[dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return self._rows


def test_discover_codex_success_reports_default_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers import codex_app_server, codex_litellm

    monkeypatch.setattr(codex_litellm, "_resolve_codex_binary", lambda: "codex")
    stub = _StubCodexProcess(
        rows=[
            {"id": "gpt-5.6-sol", "displayName": "GPT-5.6-Sol", "description": "d1", "isDefault": True},
            {"id": "gpt-5.6-terra", "displayName": "GPT-5.6-Terra", "description": "d2", "isDefault": False},
        ]
    )
    monkeypatch.setattr(codex_app_server._APP_SERVER_POOL, "process_for", lambda **_kw: stub)

    result = model_discovery.discover_codex()
    assert result.failed_reason is None
    assert result.source == model_discovery.CODEX_SOURCE
    assert {m["id"] for m in result.discovered} == {"gpt-5.6-sol", "gpt-5.6-terra"}
    assert result.default_model == "gpt-5.6-sol"


def test_discover_codex_cli_unavailable_is_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers import codex_litellm

    def _boom() -> str:
        raise codex_litellm.CodexCLIUnavailableError("codex not on PATH")

    monkeypatch.setattr(codex_litellm, "_resolve_codex_binary", _boom)
    result = model_discovery.discover_codex()
    assert result.discovered == []
    assert result.failed_reason is not None
    assert "codex not on PATH" in result.failed_reason


def test_discover_codex_rpc_error_is_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers import codex_app_server, codex_litellm

    monkeypatch.setattr(codex_litellm, "_resolve_codex_binary", lambda: "codex")
    stub = _StubCodexProcess(error=CodexAppServerError("app-server closed mid-request"))
    monkeypatch.setattr(codex_app_server._APP_SERVER_POOL, "process_for", lambda **_kw: stub)

    result = model_discovery.discover_codex()
    assert result.discovered == []
    assert "app-server closed mid-request" in (result.failed_reason or "")


def test_discover_codex_zero_models_is_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers import codex_app_server, codex_litellm

    monkeypatch.setattr(codex_litellm, "_resolve_codex_binary", lambda: "codex")
    monkeypatch.setattr(
        codex_app_server._APP_SERVER_POOL, "process_for", lambda **_kw: _StubCodexProcess(rows=[])
    )

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
    monkeypatch.setattr(model_discovery, "_resolve_claude_binary", lambda: "claude")
    responses = {
        None: {"is_error": False, "modelUsage": {"claude-fable-5": {}}},
        "fable": {"is_error": False, "modelUsage": {"claude-fable-5": {}}},
        "opus": {"is_error": False, "modelUsage": {"claude-opus-4-6-20251001": {}}},
        "sonnet": {"is_error": False, "modelUsage": {"claude-sonnet-4-6-20251001": {}}},
        "haiku": {"is_error": False, "modelUsage": {"claude-haiku-4-5-20251001": {}}},
    }
    monkeypatch.setattr(model_discovery.subprocess, "run", _fake_claude_run(responses))

    result = model_discovery.discover_claude_code(timeout=5.0)
    assert result.failed_reason is None
    assert {m["id"] for m in result.discovered} == {"fable", "opus", "sonnet", "haiku"}
    # The bare (no --model) probe resolved to claude-fable-5, matching the "fable"
    # alias's own resolution -- the CLI's own default, not a guess.
    assert result.default_model == "fable"
    assert result.rejected == []


def test_discover_claude_code_one_alias_rejected_others_still_validate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_discovery, "_resolve_claude_binary", lambda: "claude")
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
    monkeypatch.setattr(model_discovery.subprocess, "run", _fake_claude_run(responses))

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
    monkeypatch.setattr(model_discovery, "_resolve_claude_binary", lambda: "claude")
    responses = {
        alias: {"is_error": True, "api_error_status": 404, "result": f"{alias} gone"}
        for alias in (None, "fable", "opus", "sonnet", "haiku")
    }
    monkeypatch.setattr(model_discovery.subprocess, "run", _fake_claude_run(responses))

    result = model_discovery.discover_claude_code(timeout=5.0)
    assert result.discovered == []
    assert result.failed_reason is not None
    assert "fable" in result.failed_reason


def test_discover_claude_code_cli_unavailable_is_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str:
        raise model_discovery.ClaudeCodeCLIUnavailableError("claude not on PATH")

    monkeypatch.setattr(model_discovery, "_resolve_claude_binary", _boom)
    result = model_discovery.discover_claude_code()
    assert result.discovered == []
    assert "claude not on PATH" in (result.failed_reason or "")


def test_probe_claude_non_json_response_is_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(args: list[str], **_kw: Any) -> Any:
        return SimpleNamespace(stdout="not json at all", stderr="", returncode=1)

    monkeypatch.setattr(model_discovery.subprocess, "run", _run)
    probe = model_discovery._probe_claude("claude", "sonnet", timeout=5.0)
    assert probe["ok"] is False
    assert "non-JSON" in probe["reason"]


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
# refresh_all -- one provider failing must not block the others (#1211 spec).
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

    async def _fake_discover_http(preset: Any, *, api_key: str) -> model_discovery.ProviderDiscoveryResult:
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

    monkeypatch.setattr(model_discovery, "discover_codex", _fake_discover_codex)
    monkeypatch.setattr(model_discovery, "discover_http", _fake_discover_http)

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


# --------------------------------------------------------------------------- #
# live: actually invoke the installed CLIs (CLIO_RUN_LIVE=1 only).
# --------------------------------------------------------------------------- #


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live codex CLI probe: set CLIO_RUN_LIVE=1 (needs `codex` on PATH + `codex login`)",
)
def test_discover_codex_live() -> None:
    """Real ``codex app-server`` ``model/list`` call -- no LM cost, cheap+fast."""
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
