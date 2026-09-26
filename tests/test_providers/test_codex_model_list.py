"""Codex live model lists: the Direct backend list and the SDK ``model/list``.

Recorded responses (``fixtures/codex/``) were captured live on 2026-09-26:

* ``models_client_<v>.json`` -- ``GET https://chatgpt.com/backend-api/codex/models
  ?client_version=<v>`` with a signed-in ChatGPT credential (trimmed to the
  fields CLIO reads plus the gating field). ``0.147.0`` (the old pinned runtime)
  gets no ``gpt-6-*`` rows; ``0.157.1`` does -- the backend gates each model on
  ``minimal_client_version``.
* ``sdk_model_list_0.157.1.json`` -- the openai-codex 0.157.1 SDK's
  ``AsyncCodex.models()`` (the app-server ``model/list`` RPC), dumped by alias
  with only the fields the wire carried.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.providers import model_discovery
from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex import model_list, sdk_discovery
from clio_agent.providers.codex.errors import CodexRefreshFailedError
from clio_agent.providers.codex.login_flow import CodexCredential
from clio_agent.providers.codex.model_list import (
    CodexModelListError,
    fetch_direct_models,
    parse_model_list,
)
from clio_agent.providers.model_discovery import codex as md_codex

_FIXTURES = Path(__file__).parent / "fixtures" / "codex"
_VISIBLE_0157 = [
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
]


def _recorded(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _credential(token: str = "tok-1") -> CodexCredential:
    return CodexCredential(
        access_token=token,
        refresh_token="refresh",
        expires_at_ms=4_102_444_800_000,
        account_id="acct-123",
    )


class _Store:
    """A credential store stand-in that records refreshes."""

    def __init__(self, *, signed_in: bool = True, refresh_fails: bool = False) -> None:
        self.signed_in = signed_in
        self.refresh_fails = refresh_fails
        self.refreshes = 0

    def is_signed_in(self) -> bool:
        return self.signed_in

    def get_valid_credential(self, *, force_refresh: bool = False) -> CodexCredential:
        if not self.signed_in:
            from clio_agent.providers.codex.errors import CodexCredentialMissingError

            raise CodexCredentialMissingError("No stored Codex credential. Sign in first.")
        return _credential()

    def mark_invalid_after_401(self) -> CodexCredential:
        self.refreshes += 1
        if self.refresh_fails:
            raise CodexRefreshFailedError(
                "Codex token refresh failed: invalid_grant. Sign in again."
            )
        return _credential("tok-2")


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Direct: parsing the recorded backend response
# --------------------------------------------------------------------------- #


def test_recorded_list_yields_picker_visible_models_in_priority_order() -> None:
    listing = parse_model_list(_recorded("models_client_0.157.1.json"), client_version="0.157.1")

    assert [model.id for model in listing.models] == _VISIBLE_0157
    # Hidden rows (reserve / internal reviewer) are never offered.
    assert not {"gpt-reserve", "codex-auto-review"} & {model.id for model in listing.models}
    # The CLI's own rule: the first picker-visible model by priority.
    assert listing.default_model == "gpt-6-astra"
    sol = next(model for model in listing.models if model.id == "gpt-6-sol")
    assert sol.name == "GPT-6-Sol"
    assert sol.context_window == 272000
    assert sol.input_modalities == ["text", "image"]
    assert sol.reasoning_efforts == ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert sol.default_reasoning_effort == "medium"


def test_old_client_version_is_gated_out_of_gpt_6() -> None:
    """The recorded evidence for the owner's report: 0.147.0 never sees gpt-6."""

    listing = parse_model_list(_recorded("models_client_0.147.0.json"), client_version="0.147.0")

    assert not [model.id for model in listing.models if model.id.startswith("gpt-6")]
    assert listing.default_model == "gpt-5.6-sol"


def test_malformed_and_empty_lists_are_typed() -> None:
    with pytest.raises(CodexModelListError) as malformed:
        parse_model_list({"data": []}, client_version="1")
    assert malformed.value.code == "codex_direct_invalid_response"

    with pytest.raises(CodexModelListError) as no_slug:
        parse_model_list({"models": [{"visibility": "list"}]}, client_version="1")
    assert no_slug.value.code == "codex_direct_invalid_response"

    hidden_only = {
        "models": [{"slug": "x", "visibility": "hide", "supported_reasoning_levels": []}]
    }
    with pytest.raises(CodexModelListError) as empty:
        parse_model_list(hidden_only, client_version="1")
    assert empty.value.code == "codex_direct_zero_models"


# --------------------------------------------------------------------------- #
# Direct: the HTTP exchange
# --------------------------------------------------------------------------- #


def test_fetch_sends_the_cli_request_shape_with_clios_credential() -> None:
    seen: list[httpx.Request] = []
    body = _recorded("models_client_0.157.1.json")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body, headers={"etag": 'W/"abc"'})

    listing = fetch_direct_models(store=_Store(), client=_client(handler))  # type: ignore[arg-type]

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url).split("?")[0] == c.CODEX_MODELS_URL
    assert c.CODEX_MODELS_URL == "https://chatgpt.com/backend-api/codex/models"
    # The same Codex version the SDK half's runtime presents.
    installed = metadata.version("openai-codex-cli-bin")
    assert request.url.params["client_version"] == installed == listing.client_version
    assert request.headers["authorization"] == "Bearer tok-1"
    assert request.headers["chatgpt-account-id"] == "acct-123"
    assert request.headers["originator"] == c.ORIGINATOR
    assert listing.etag == 'W/"abc"'
    assert [model.id for model in listing.models] == _VISIBLE_0157


def test_fetch_refreshes_once_on_401_then_retries() -> None:
    tokens: list[str] = []
    body = _recorded("models_client_0.157.1.json")

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        if len(tokens) == 1:
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json=body)

    store = _Store()
    listing = fetch_direct_models(store=store, client=_client(handler))  # type: ignore[arg-type]

    assert tokens == ["Bearer tok-1", "Bearer tok-2"]
    assert store.refreshes == 1
    assert listing.default_model == "gpt-6-astra"


@pytest.mark.parametrize(
    ("store", "handler", "code"),
    [
        (_Store(), lambda r: httpx.Response(401), "codex_direct_auth_rejected"),
        (_Store(refresh_fails=True), lambda r: httpx.Response(401), "codex_direct_auth_rejected"),
        (_Store(), lambda r: httpx.Response(503, text="busy"), "codex_direct_http_error"),
        (_Store(), lambda r: httpx.Response(200, text="<html>"), "codex_direct_invalid_response"),
        (_Store(signed_in=False), lambda r: httpx.Response(200), "codex_direct_signed_out"),
    ],
)
def test_fetch_failures_are_typed(store: _Store, handler: Any, code: str) -> None:
    with pytest.raises(CodexModelListError) as error:
        fetch_direct_models(store=store, client=_client(handler))  # type: ignore[arg-type]
    assert error.value.code == code
    assert str(error.value).startswith(code)


def test_transport_error_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(CodexModelListError) as error:
        fetch_direct_models(store=_Store(), client=_client(handler))  # type: ignore[arg-type]
    assert error.value.code == "codex_direct_transport_error"


def test_missing_runtime_version_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(_name: str) -> str:
        raise metadata.PackageNotFoundError(_name)

    monkeypatch.setattr(model_list.metadata, "version", _missing)
    with pytest.raises(CodexModelListError) as error:
        model_list.codex_client_version()
    assert error.value.code == "codex_direct_client_version_unknown"


# --------------------------------------------------------------------------- #
# Direct: discovery rows (labels, PDF transport fact, unknown output limit)
# --------------------------------------------------------------------------- #


def test_discover_codex_rows_carry_live_facts_and_the_pdf_transport_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = parse_model_list(_recorded("models_client_0.157.1.json"), client_version="0.157.1")
    monkeypatch.setattr(md_codex, "fetch_direct_models", lambda **_k: listing)

    result = md_codex.discover_codex(credential_store=_Store())  # type: ignore[arg-type]

    assert result.failed_reason is None
    assert result.provider == "codex"
    assert result.source == model_discovery.CODEX_SOURCE == "codex_direct_model_list"
    assert result.default_model == "gpt-6-astra"
    assert [row["id"] for row in result.discovered] == _VISIBLE_0157
    sol = next(row for row in result.discovered if row["id"] == "gpt-6-sol")
    assert sol["capabilities"] == ["text", "image", "pdf"]
    evidence = sol["capability_evidence"]
    assert evidence["source"] == "codex_direct_model_list"
    assert evidence["reason"] == "modality_reported"
    assert evidence["added"]["pdf"]["source"] == "codex_direct_input_file"
    assert sol["context_window"] == 272000
    assert sol["output_limit"] is None  # not reported; never guessed
    assert sol["supported_reasoning_efforts"][-1] == "ultra"


def test_discover_codex_failure_is_the_typed_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(**_k: Any) -> Any:
        raise CodexModelListError("codex_direct_http_error", "HTTP 503")

    monkeypatch.setattr(md_codex, "fetch_direct_models", _fail)

    result = md_codex.discover_codex(credential_store=_Store())  # type: ignore[arg-type]

    assert result.discovered == []
    assert (result.failed_reason or "").startswith("codex_direct_http_error")


def test_discover_codex_signed_out_never_asks_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unexpected(**_k: Any) -> Any:
        raise AssertionError("the backend must not be asked without a credential")

    monkeypatch.setattr(md_codex, "fetch_direct_models", _unexpected)

    result = md_codex.discover_codex(credential_store=_Store(signed_in=False))  # type: ignore[arg-type]

    assert result.discovered == []
    assert "sign-in is required" in (result.failed_reason or "")


# --------------------------------------------------------------------------- #
# Cache: the overlay keeps the last good list and says why it is stale
# --------------------------------------------------------------------------- #


def test_failed_refresh_keeps_last_good_list_with_typed_staleness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = parse_model_list(_recorded("models_client_0.157.1.json"), client_version="0.157.1")
    monkeypatch.setattr(md_codex, "fetch_direct_models", lambda **_k: listing)
    model_discovery.record_refresh(md_codex.discover_codex(credential_store=_Store()))  # type: ignore[arg-type]

    def _fail(**_k: Any) -> Any:
        raise CodexModelListError("codex_direct_transport_error", "ConnectError")

    monkeypatch.setattr(md_codex, "fetch_direct_models", _fail)
    wire = model_discovery.record_refresh(md_codex.discover_codex(credential_store=_Store()))  # type: ignore[arg-type]

    assert [row["id"] for row in wire["discovered"]] == _VISIBLE_0157
    assert wire["staleness"]["reason"] == "overlay_refresh_failed"
    assert wire["staleness"]["failed_reason"].startswith("codex_direct_transport_error")
    served = model_discovery.overlay_models_wire("codex", "codex")
    assert served is not None
    assert [row["id"] for row in served["models"]] == _VISIBLE_0157
    assert served["staleness"]["reason"] == "overlay_refresh_failed"


def test_sdk_recheck_is_due_only_after_the_ttl_from_the_last_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.provider_catalog import _sdk_recheck_due

    monkeypatch.setenv("CLIO_MODEL_CATALOG_TTL_S", "3600")
    now = datetime.now(timezone.utc)
    fresh = {"generated_at": (now - timedelta(minutes=5)).isoformat()}
    old = {"generated_at": (now - timedelta(hours=2)).isoformat()}
    # A recent FAILED ask resets the clock: a missing SDK is not re-asked per read.
    failed_recently = {
        "generated_at": (now - timedelta(hours=2)).isoformat(),
        "staleness": {"last_attempt_at": (now - timedelta(minutes=1)).isoformat()},
    }
    assert _sdk_recheck_due(fresh) is False
    assert _sdk_recheck_due(old) is True
    assert _sdk_recheck_due(failed_recently) is False
    assert _sdk_recheck_due({"generated_at": "garbage"}) is True
    monkeypatch.setenv("CLIO_MODEL_CATALOG_TTL_S", "0")
    assert _sdk_recheck_due(old) is False


# --------------------------------------------------------------------------- #
# SDK: the recorded model/list response
# --------------------------------------------------------------------------- #


class _RecordedSdk:
    """The ``AsyncCodex`` boundary replaying the recorded ``model/list`` response."""

    def __call__(self, *_a: object, **_k: object) -> "_RecordedSdk":
        return self

    async def __aenter__(self) -> "_RecordedSdk":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    async def account(self) -> Any:
        return type("Account", (), {"account": object()})()

    async def models(self) -> Any:
        from openai_codex.generated.v2_all import ModelListResponse

        return ModelListResponse.model_validate(_recorded("sdk_model_list_0.157.1.json"))


def test_sdk_recorded_model_list_is_labelled_and_lists_gpt_6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sdk_discovery, "AsyncCodex", _RecordedSdk())

    result = asyncio.run(sdk_discovery.discover_codex_sdk_async())

    assert result.failed_reason is None
    assert result.provider == "codex_sdk"
    assert result.source == model_discovery.CODEX_SDK_SOURCE == "codex_sdk_model_list"
    assert [row["id"] for row in result.discovered] == _VISIBLE_0157
    assert result.default_model == "gpt-6-astra"
    sol = next(row for row in result.discovered if row["id"] == "gpt-6-sol")
    # The SDK carries no file input: its rows never claim PDF.
    assert sol["capabilities"] == ["text", "image"]
    assert sol["supported_reasoning_efforts"][-1] == "ultra"
