"""GitHub Copilot sign-in, live model discovery, runtime selection and typed errors.

* :mod:`clio_agent.providers.copilot.auth` reads CLIO's own GitHub sign-in (never an
  ambient login); a signed-out account is the typed sign-in error and the runtime's
  token callback answers ``cancelled``.
* :mod:`clio_agent.providers.model_discovery.copilot` reads the account's LIVE
  ``list_models()`` after checking ``auth.getStatus``; policy-disabled models are
  typed rejections, never silently missing; a failure is CLIO's typed text.
* the overlay marks a Copilot list read under a previous GitHub grant stale.
* :mod:`clio_agent.providers.copilot.runtime` prefers a newer native ``copilot``,
  else the staged runtime, and never downloads.
* :mod:`clio_agent.providers.copilot.errors` classifies ``session.error`` by its typed
  fields only.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.providers.components import client_binary
from clio_agent.providers.copilot import auth, errors, runtime
from clio_agent.providers.copilot.errors import (
    COPILOT_INSTALL_FAILED_MESSAGE,
    COPILOT_NOT_INSTALLED_MESSAGE,
    COPILOT_PLAN_LIMIT_MESSAGE_MARKER,
    COPILOT_SIGNED_OUT_MESSAGE,
    CopilotAuthRequiredError,
    CopilotModelUnavailableError,
    CopilotNotInstalledError,
    CopilotPlanLimitError,
    CopilotPolicyDisabledError,
    CopilotRuntimeMissingError,
)
from clio_agent.providers.model_discovery import copilot as discovery
from clio_agent.providers.model_discovery import overlay
from tests import _fake_copilot_sdk as fake


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "model_catalog.json"))
    fake.reset()
    client_binary.reset_client_cache()
    yield
    fake.reset()
    client_binary.reset_client_cache()


# --------------------------------------------------------------------------- #
# Sign-in                                                                     #
# --------------------------------------------------------------------------- #
def test_no_token_source_is_the_typed_sign_in_error() -> None:
    with pytest.raises(CopilotAuthRequiredError, match="github_sign_in_unavailable") as caught:
        auth.github_credential()
    assert str(caught.value).startswith(COPILOT_SIGNED_OUT_MESSAGE)
    assert auth.signed_in() is False


def test_a_refused_refresh_is_the_typed_sign_in_error_with_its_words() -> None:
    def refused() -> tuple[str, int]:
        raise PermissionError("refresh token revoked")

    auth.set_token_source(refused)
    with pytest.raises(CopilotAuthRequiredError, match="refresh token revoked"):
        auth.github_token()


def test_an_empty_token_is_signed_out() -> None:
    auth.set_token_source(lambda: ("", 100))
    with pytest.raises(CopilotAuthRequiredError, match="github_token_empty"):
        auth.github_token()


def test_the_credential_carries_a_positive_lifetime() -> None:
    auth.set_token_source(lambda: ("ghu_1", 0))
    assert auth.github_credential() == ("ghu_1", 1)
    assert auth.signed_in() is True


def test_the_runtimes_token_callback_answers_from_clios_sign_in() -> None:
    auth.set_token_source(lambda: ("ghu_2", 900))
    assert asyncio.run(auth.token_provider(None)) == {
        "kind": "token",
        "accessToken": "ghu_2",
        "expiresIn": 900,
    }
    auth.set_token_source(None)
    # Signed out: the runtime rejects the session, never falling back to an ambient login.
    assert asyncio.run(auth.token_provider(None)) == {"kind": "cancelled"}


# --------------------------------------------------------------------------- #
# Live model discovery                                                        #
# --------------------------------------------------------------------------- #
def test_discovery_offers_enabled_models_with_their_reported_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(
        monkeypatch,
        models=[
            fake.model_info(
                "claude-sonnet-4.5", name="Claude Sonnet 4.5", vision=True, multiplier=1.0
            ),
            fake.model_info(
                "gpt-5", reasoning=True, efforts=["low", "medium", "high"], prompt_tokens=None
            ),
            fake.model_info("o-free", state=None, multiplier=None),
        ],
    )
    result = discovery.discover_copilot()

    assert result.failed_reason is None
    assert result.source == overlay.COPILOT_SOURCE
    assert result.default_model == "claude-sonnet-4.5"  # the account's first enabled model
    sonnet, gpt, free = result.discovered
    assert sonnet["name"] == "Claude Sonnet 4.5"
    assert (sonnet["context_window"], sonnet["context_window_field"]) == (
        128_000,
        "max_prompt_tokens",
    )
    assert sonnet["output_limit"] == 16_000
    assert sonnet["capabilities"] == ["text", "image"]
    assert sonnet["billing_multiplier"] == 1.0
    # No prompt budget reported: the total window, and the row says which field it was.
    assert (gpt["context_window"], gpt["context_window_field"]) == (
        200_000,
        "max_context_window_tokens",
    )
    assert gpt["capabilities"] == ["text"]
    assert gpt["reasoning_effort_supported"] is True
    assert gpt["supported_reasoning_efforts"] == ["low", "medium", "high"]
    assert gpt["default_reasoning_effort"] == "low"
    assert free["billing_multiplier"] is None  # not reported: not invented
    [client] = sdk.clients
    assert client.kwargs["use_logged_in_user"] is False
    assert client.kwargs["github_token"] == fake.TOKEN
    assert client.stopped  # the discovery runtime is short-lived


def test_policy_disabled_models_are_typed_rejections(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(
        monkeypatch,
        models=[
            fake.model_info("gpt-5"),
            fake.model_info("o3", name="o3", state="disabled"),
            fake.model_info("grok", name="Grok", state="unconfigured"),
        ],
    )
    result = discovery.discover_copilot()
    assert [row["id"] for row in result.discovered] == ["gpt-5"]
    assert [(r["id"], r["code"]) for r in result.rejected] == [
        ("o3", "copilot_policy_disabled"),
        ("grok", "copilot_policy_unconfigured"),
    ]
    assert "disabled by the GitHub Copilot policy" in result.rejected[0]["reason"]


def test_no_enabled_model_is_a_failure_not_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch, models=[fake.model_info("o3", state="disabled")])
    result = discovery.discover_copilot()
    assert result.failed_reason == discovery.NO_ENABLED_MODELS_MESSAGE
    assert result.discovered == []
    assert [r["code"] for r in result.rejected] == ["copilot_policy_disabled"]


def test_a_token_the_service_refuses_is_the_typed_sign_in_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(monkeypatch, authenticated=False, status_message="Bad credentials")
    result = discovery.discover_copilot()
    assert result.failed_reason is not None
    assert result.failed_reason.startswith(COPILOT_SIGNED_OUT_MESSAGE)
    assert "Bad credentials" in result.failed_reason
    assert errors.contains_copilot_auth_required(result.failed_reason)
    assert sdk.clients[0].stopped


def test_a_signed_out_clio_never_starts_a_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = fake.install(monkeypatch, token=None)
    result = discovery.discover_copilot()
    assert result.failed_reason is not None
    assert result.failed_reason.startswith(COPILOT_SIGNED_OUT_MESSAGE)
    assert sdk.clients == []


def test_a_transport_failure_keeps_the_runtimes_words(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch, list_error=fake.JsonRpcError("models.list: 503"))
    result = discovery.discover_copilot()
    assert result.failed_reason == (
        "GitHub Copilot model list unavailable: JsonRpcError: models.list: 503"
    )


def test_a_missing_runtime_is_the_typed_dependency_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.install(monkeypatch)

    def missing() -> str:
        raise CopilotRuntimeMissingError(
            errors.COPILOT_RUNTIME_MISSING_MESSAGE, detail="copilot_no_runtime"
        )

    monkeypatch.setattr(runtime, "runtime_path", missing)
    result = discovery.discover_copilot()
    assert errors.contains_copilot_dependency_error(result.failed_reason)


# --------------------------------------------------------------------------- #
# Overlay: a list read under a previous GitHub grant is stale                 #
# --------------------------------------------------------------------------- #
def _success() -> overlay.ProviderDiscoveryResult:
    return overlay.ProviderDiscoveryResult(
        provider="copilot",
        discovered=[{"id": "gpt-5"}],
        source=overlay.COPILOT_SOURCE,
        default_model="gpt-5",
    )


def test_a_github_grant_change_marks_the_copilot_list_stale_until_rechecked() -> None:
    overlay.record_refresh(_success())
    assert "staleness" not in (overlay.overlay_models_wire("copilot", "copilot") or {})

    overlay.update_entry_fields("copilot", {"account_changed_at": "2026-10-22T00:00:00+00:00"})
    wire = overlay.overlay_models_wire("copilot", "copilot")
    assert wire is not None
    assert wire["models"] == [{"id": "gpt-5"}]  # still served, marked as prior evidence
    assert wire["staleness"]["reason"] == "overlay_account_changed"
    assert wire["staleness"]["account_changed_at"] == "2026-10-22T00:00:00+00:00"

    overlay.record_refresh(_success())
    assert "staleness" not in (overlay.overlay_models_wire("copilot", "copilot") or {})
    stored = json.loads(overlay.overlay_path().read_text(encoding="utf-8"))
    assert "account_changed_at" not in stored["copilot"]


def test_a_failed_recheck_keeps_the_account_change_marker() -> None:
    overlay.record_refresh(_success())
    overlay.update_entry_fields("copilot", {"account_changed_at": "2026-10-22T00:00:00+00:00"})
    overlay.record_refresh(
        overlay.ProviderDiscoveryResult(
            provider="copilot",
            discovered=[],
            source=overlay.COPILOT_SOURCE,
            failed_reason=COPILOT_SIGNED_OUT_MESSAGE,
        )
    )
    wire = overlay.overlay_models_wire("copilot", "copilot")
    assert wire is not None and "staleness" in wire


def test_the_account_changed_reason_is_catalogued() -> None:
    assert "overlay_account_changed" in overlay.OVERLAY_STALENESS_REASONS


# --------------------------------------------------------------------------- #
# Runtime selection                                                           #
# --------------------------------------------------------------------------- #
def _candidates(
    monkeypatch: pytest.MonkeyPatch,
    *,
    staged: str | None,
    installed: tuple[str, str] | None,
    reason: str = "copilot_installed_not_found",
    pinned: str = "1.0.107",
) -> None:
    monkeypatch.setattr(runtime, "staged_runtime_path", lambda: staged)
    monkeypatch.setattr(runtime, "pinned_runtime_version", lambda: pinned)
    binary = (
        None
        if installed is None
        else client_binary.ClientBinary(path=installed[0], version=installed[1], source="installed")
    )
    monkeypatch.setattr(runtime, "_installed_copilot", lambda: (binary, "" if binary else reason))


def test_no_runtime_at_all_is_a_typed_refusal_never_a_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidates(monkeypatch, staged=None, installed=None)
    assert runtime.copilot_client().reason == "copilot_no_runtime"
    with pytest.raises(CopilotRuntimeMissingError, match="copilot_no_runtime"):
        runtime.runtime_path()


def _staged_bundle(*, with_runtime: bool = True) -> Path:
    """The bundle ``python -m copilot download-runtime`` leaves: no ``copilot`` alias."""
    from copilot._cli_download import get_cache_dir  # noqa: PLC0415
    from copilot._cli_version import CLI_VERSION, get_runtime_platform  # noqa: PLC0415

    pair = get_cache_dir(CLI_VERSION) / "prebuilds" / get_runtime_platform()
    pair.mkdir(parents=True)
    names = [runtime._wrapper_name(), ".hostless-runtime-assets-v2"]
    names += ["runtime.node"] if with_runtime else []
    for name in names:
        (pair / name).write_bytes(b"x")
    return pair


def test_a_runtime_staged_by_download_runtime_is_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SABOTAGE: require the ``copilot`` alias (``get_cached_cli_path``) -> None -> red."""
    monkeypatch.setenv("COPILOT_CLI_EXTRACT_DIR", str(tmp_path))
    pair = _staged_bundle()
    assert runtime.staged_runtime_path() == str(pair / runtime._wrapper_name())


def test_an_incomplete_staged_bundle_is_not_a_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COPILOT_CLI_EXTRACT_DIR", str(tmp_path))
    _staged_bundle(with_runtime=False)
    assert runtime.staged_runtime_path() is None


def test_staging_the_runtime_drops_a_selection_cached_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess  # noqa: PLC0415

    from clio_agent.providers import dependencies, support_record  # noqa: PLC0415

    _candidates(monkeypatch, staged=None, installed=None)
    assert runtime.copilot_client().reason == "copilot_no_runtime"
    monkeypatch.setattr(dependencies, "ensure_provider_extra", lambda **_kwargs: False)
    monkeypatch.setattr(support_record, "record_support", lambda _kind: None)

    def download(command: list[str]) -> subprocess.CompletedProcess[str]:
        monkeypatch.setattr(runtime, "staged_runtime_path", lambda: "/cache/copilot-runtime")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(dependencies, "_run_install_command", download)
    assert dependencies.ensure_copilot_support() is True
    assert runtime.copilot_client().path == "/cache/copilot-runtime"


def test_the_staged_runtime_wins_unless_the_installed_one_is_newer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidates(monkeypatch, staged="/staged/rt", installed=("/usr/bin/copilot", "1.0.100"))
    selection = runtime.copilot_client()
    assert (selection.path, selection.reason) == ("/staged/rt", "copilot_staged_not_older")

    client_binary.reset_client_cache()
    _candidates(monkeypatch, staged="/staged/rt", installed=("/usr/bin/copilot", "1.0.200"))
    selection = runtime.copilot_client()
    assert (selection.path, selection.reason) == ("/usr/bin/copilot", "copilot_installed_newer")


def test_only_one_candidate_is_used_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _candidates(
        monkeypatch, staged="/staged/rt", installed=None, reason="copilot_installed_not_native"
    )
    assert runtime.copilot_client().reason == "copilot_installed_not_native"
    client_binary.reset_client_cache()
    _candidates(monkeypatch, staged=None, installed=("/usr/bin/copilot", "1.0.1"))
    selection = runtime.copilot_client()
    assert (selection.path, selection.reason) == ("/usr/bin/copilot", "copilot_runtime_not_staged")


def test_a_shim_on_path_is_not_a_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    shim = tmp_path / "copilot.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: str(shim))
    monkeypatch.setattr(runtime, "is_native_executable", lambda _path: False)
    assert runtime._installed_copilot() == (None, "copilot_installed_not_native")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    assert runtime._installed_copilot() == (None, "copilot_installed_not_found")


# --------------------------------------------------------------------------- #
# Typed errors                                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("data", "kind"),
    [
        ({"remediation": SimpleNamespace(value="sign_in")}, CopilotAuthRequiredError),
        ({"error_type": "authentication"}, CopilotAuthRequiredError),
        ({"error_code": "no_copilot_access"}, errors.CopilotNoSeatError),
        ({"status_code": 402}, CopilotPlanLimitError),
        ({"error_code": "quota_exceeded"}, CopilotPlanLimitError),
        ({"error_code": "model_policy_disabled"}, CopilotPolicyDisabledError),
        ({"error_code": "model_not_supported"}, CopilotModelUnavailableError),
        ({"error_type": "server", "message": "upstream 500"}, errors.CopilotSessionError),
    ],
)
def test_session_errors_classify_by_typed_fields(data: dict[str, Any], kind: type) -> None:
    data.setdefault("message", "the runtime's words")
    error = errors.classify_session_error(SimpleNamespace(**data), resets_at="2026-11-01")
    assert type(error) is kind
    assert error.detail == data["message"]


def test_the_free_text_message_is_never_parsed() -> None:
    error = errors.classify_session_error(SimpleNamespace(message="401 sign in, quota, policy"))
    assert type(error) is errors.CopilotSessionError


def test_a_plan_limit_carries_its_reset_and_clean_message() -> None:
    error = errors.classify_session_error(
        SimpleNamespace(message="limit", status_code=402), resets_at="2026-11-01"
    )
    assert isinstance(error, CopilotPlanLimitError)
    assert error.resets_at == "2026-11-01"
    assert errors.copilot_plan_limit_message(RuntimeError(f"wrapped: {error}")) is not None
    assert errors.copilot_plan_limit_message(error).startswith(COPILOT_PLAN_LIMIT_MESSAGE_MARKER)
    assert errors.copilot_plan_limit_message(RuntimeError("other")) is None


def test_the_contains_helpers_walk_groups_causes_and_rewrapped_text() -> None:
    signed_out = CopilotAuthRequiredError(COPILOT_SIGNED_OUT_MESSAGE)
    wrapped = RuntimeError("litellm: boom")
    wrapped.__cause__ = signed_out
    assert errors.contains_copilot_auth_required(wrapped)
    assert errors.contains_copilot_auth_required(ExceptionGroup("g", [ValueError(), signed_out]))
    assert errors.contains_copilot_auth_required(f"APIError: {COPILOT_SIGNED_OUT_MESSAGE}")
    assert not errors.contains_copilot_auth_required(RuntimeError("401 Unauthorized"))

    missing = CopilotNotInstalledError(COPILOT_NOT_INSTALLED_MESSAGE)
    assert errors.contains_copilot_dependency_error(missing)
    assert errors.contains_copilot_dependency_error(ExceptionGroup("g", [missing]))
    assert errors.contains_copilot_dependency_error("No module named 'copilot'")
    assert not errors.contains_copilot_dependency_error(COPILOT_INSTALL_FAILED_MESSAGE)


def test_without_the_extra_there_are_no_sdk_transport_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "copilot._jsonrpc", None)
    assert errors.sdk_transport_errors() == ()
    assert OSError in errors.teardown_errors()
