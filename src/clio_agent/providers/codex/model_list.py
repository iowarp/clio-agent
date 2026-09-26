"""The Codex Direct transport's LIVE model list (``GET /backend-api/codex/models``).

The official Codex CLI reads its account model list from the ChatGPT Codex
backend (``codex-rs/codex-api/src/endpoint/models.rs``, ``ModelsClient``:
``GET {base}/models?client_version=<v>`` with the ChatGPT bearer token and the
``chatgpt-account-id`` header). This module asks the same endpoint with CLIO's
OWN OAuth credential (:class:`~clio_agent.providers.codex.credentials.
CodexCredentialStore`) -- never ``~/.codex/auth.json`` -- so the Direct half of
the picker shows exactly what the signed-in account can use, the way the SDK
half does through the SDK's ``model/list`` RPC.

**The client version decides the list.** The backend gates each model on its
``minimal_client_version`` (verified live 2026-09-26: ``client_version=0.147.0``
returns no ``gpt-6-*`` rows, ``0.155.1`` and later return them). CLIO presents
the version of the Codex runtime it ships (:data:`~clio_agent.providers.codex.
constants.CODEX_CLIENT_DISTRIBUTION`) -- the same version the SDK half's
app-server presents -- so both transports are gated identically.

Caching is NOT done here: the result becomes a
:class:`~clio_agent.providers.model_discovery.overlay.ProviderDiscoveryResult`
that the refresh overlay records -- the ONE model-catalog cache, with its TTL
(``providers.model_catalog_ttl_s``), its kept last-good list on a failed
refresh, and its typed staleness reasons. A second disk cache for the same
rows would be a parallel store of the same facts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import metadata
from typing import Any

import httpx

from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex.credentials import CodexCredentialStore
from clio_agent.providers.codex.errors import CodexAuthError, CodexCredentialMissingError
from clio_agent.providers.codex.login_flow import CodexCredential

logger = logging.getLogger(__name__)

#: Typed reasons, in the ``stream_fallback`` reason-catalog style: the code is
#: the queryable fact, the sentence is what a person reads.
DIRECT_MODEL_LIST_REASONS: dict[str, str] = {
    "codex_direct_client_version_unknown": (
        "the installed Codex runtime version could not be read, so the backend cannot be "
        "asked for the models that version supports"
    ),
    "codex_direct_signed_out": "no Codex credential is stored; sign in to Codex first",
    "codex_direct_auth_rejected": (
        "the Codex backend rejected CLIO's Codex credential even after a token refresh; "
        "sign in to Codex again"
    ),
    "codex_direct_transport_error": "the Codex backend's model list could not be reached",
    "codex_direct_http_error": "the Codex backend's model list answered with an error status",
    "codex_direct_invalid_response": "the Codex backend's model list response was malformed",
    "codex_direct_zero_models": "the Codex backend listed no picker-visible models",
}

_TIMEOUT_S = 20.0
#: The picker-visible visibility value (``ModelVisibility::List``); ``hide``
#: rows (internal reviewers, reserve models) are what the SDK's
#: ``model/list`` also omits unless ``include_hidden`` is asked for.
_VISIBLE = "list"


class CodexModelListError(RuntimeError):
    """The Direct model list could not be read; ``code`` is a :data:`DIRECT_MODEL_LIST_REASONS` key."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = f"{code}: {DIRECT_MODEL_LIST_REASONS[code]}"
        super().__init__(f"{message} ({detail})" if detail else message)


@dataclass(frozen=True)
class DirectModel:
    """One picker-visible row of the backend's model list, reduced to what CLIO uses."""

    id: str
    name: str
    description: str
    context_window: int | None
    input_modalities: list[str]
    reasoning_efforts: list[str]
    default_reasoning_effort: str
    priority: int


@dataclass(frozen=True)
class DirectModelList:
    """The parsed list plus the facts needed to reproduce the request."""

    models: list[DirectModel]
    default_model: str
    client_version: str
    etag: str


def codex_client_version() -> str:
    """The Codex client version CLIO presents (the shipped runtime's own version).

    Raises:
        CodexModelListError: ``codex_direct_client_version_unknown`` when the
            distribution is not installed.
    """

    try:
        return metadata.version(c.CODEX_CLIENT_DISTRIBUTION)
    except metadata.PackageNotFoundError as exc:
        raise CodexModelListError(
            "codex_direct_client_version_unknown", c.CODEX_CLIENT_DISTRIBUTION
        ) from exc


def _headers(credential: CodexCredential) -> dict[str, str]:
    """Model-list request headers. Never log these: they carry the bearer token."""

    return {
        "Authorization": f"Bearer {credential.access_token}",
        "chatgpt-account-id": credential.account_id,
        "originator": c.ORIGINATOR,
        "User-Agent": "clio-agent",
        "accept": "application/json",
    }


def _efforts(row: dict[str, Any]) -> list[str]:
    levels = row.get("supported_reasoning_levels")
    if not isinstance(levels, list):
        raise CodexModelListError(
            "codex_direct_invalid_response", f"{row.get('slug')!r} supported_reasoning_levels"
        )
    return [
        str(level["effort"])
        for level in levels
        if isinstance(level, dict) and isinstance(level.get("effort"), str) and level["effort"]
    ]


def _parse_row(row: Any) -> DirectModel | None:
    """One ``ModelInfo`` row -> :class:`DirectModel`, or ``None`` for a hidden row."""

    if not isinstance(row, dict):
        raise CodexModelListError("codex_direct_invalid_response", "non-object model row")
    slug = row.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        raise CodexModelListError("codex_direct_invalid_response", "model row without a slug")
    if row.get("visibility") != _VISIBLE:
        return None
    modalities = row.get("input_modalities")
    context_window = row.get("context_window")
    priority = row.get("priority")
    return DirectModel(
        id=slug,
        name=str(row.get("display_name") or slug),
        description=str(row.get("description") or ""),
        context_window=(
            context_window if isinstance(context_window, int) and context_window > 0 else None
        ),
        # ``input_modalities`` defaults to text+image in the CLI's own serde
        # schema when absent; CLIO records only what the wire carried.
        input_modalities=(
            [str(value) for value in modalities if isinstance(value, str)]
            if isinstance(modalities, list)
            else []
        ),
        reasoning_efforts=_efforts(row),
        default_reasoning_effort=str(row.get("default_reasoning_level") or ""),
        priority=priority if isinstance(priority, int) else 0,
    )


def parse_model_list(payload: Any, *, client_version: str, etag: str = "") -> DirectModelList:
    """Validate a decoded ``{"models": [ModelInfo, ...]}`` body (pure, no I/O).

    The default model follows the CLI's own rule
    (``ModelPreset::mark_default_by_picker_visibility``): the first
    picker-visible model in ``priority`` order.

    Raises:
        CodexModelListError: ``codex_direct_invalid_response`` or
            ``codex_direct_zero_models``.
    """

    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise CodexModelListError("codex_direct_invalid_response", "no models array")
    models = [model for model in (_parse_row(row) for row in rows) if model is not None]
    if not models:
        raise CodexModelListError("codex_direct_zero_models", f"client_version={client_version}")
    models.sort(key=lambda model: model.priority)
    return DirectModelList(
        models=models, default_model=models[0].id, client_version=client_version, etag=etag
    )


def _get(client: httpx.Client, credential: CodexCredential, client_version: str) -> httpx.Response:
    try:
        return client.get(
            c.CODEX_MODELS_URL,
            params={"client_version": client_version},
            headers=_headers(credential),
        )
    except httpx.HTTPError as exc:
        raise CodexModelListError("codex_direct_transport_error", type(exc).__name__) from exc


def fetch_direct_models(
    *,
    store: CodexCredentialStore | None = None,
    client: httpx.Client | None = None,
) -> DirectModelList:
    """Ask the Codex backend for this account's models with CLIO's own credential.

    A 401 refreshes the credential once and retries (the transports' A.7 rule).

    Raises:
        CodexModelListError: every failure, with a typed ``code``.
    """

    client_version = codex_client_version()
    credential_store = store or CodexCredentialStore()
    try:
        credential = credential_store.get_valid_credential()
    except CodexCredentialMissingError as exc:
        raise CodexModelListError("codex_direct_signed_out") from exc
    except CodexAuthError as exc:
        raise CodexModelListError("codex_direct_auth_rejected", str(exc)) from exc

    owns_client = client is None
    http = client or httpx.Client(timeout=_TIMEOUT_S, follow_redirects=False)
    try:
        response = _get(http, credential, client_version)
        if response.status_code == 401:
            try:
                credential = credential_store.mark_invalid_after_401()
            except CodexAuthError as exc:
                raise CodexModelListError("codex_direct_auth_rejected", str(exc)) from exc
            response = _get(http, credential, client_version)
            if response.status_code == 401:
                raise CodexModelListError("codex_direct_auth_rejected", "401 after refresh")
    finally:
        if owns_client:
            http.close()
    if response.status_code >= 400:
        raise CodexModelListError("codex_direct_http_error", f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise CodexModelListError("codex_direct_invalid_response", "body is not JSON") from exc
    result = parse_model_list(
        payload, client_version=client_version, etag=response.headers.get("etag") or ""
    )
    logger.info(
        "codex direct model list: client_version=%s models=%d default=%s",
        client_version,
        len(result.models),
        result.default_model,
    )
    return result


__all__ = [
    "DIRECT_MODEL_LIST_REASONS",
    "CodexModelListError",
    "DirectModel",
    "DirectModelList",
    "codex_client_version",
    "fetch_direct_models",
    "parse_model_list",
]
