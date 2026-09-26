"""Codex Direct model discovery: the backend's LIVE account model list.

The Direct transport's models come from the Codex backend itself
(:func:`clio_agent.providers.codex.model_list.fetch_direct_models`, the same
``GET /backend-api/codex/models`` the official Codex CLI reads), asked with
CLIO's own Codex credential. There is no compiled-in or maintained model list
behind it: a model the account cannot use never appears, and a new model
appears as soon as the backend lists it for the Codex version CLIO ships.

Each row carries only what the backend reported (id, name, context window,
input modalities, reasoning efforts) plus ONE transport fact the list does not
report: this transport delivers PDF attachments as Responses ``input_file``
items, so ``pdf`` is added with its own evidence source
(``codex_direct_input_file``). The output-token limit is not reported by the
list and stays unknown rather than guessed.

The result is recorded by the refresh overlay (the ONE model-catalog cache:
TTL, kept last-good list, typed staleness), exactly like the SDK transport's
``model/list`` result.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.codex.credentials import CodexCredentialStore
from clio_agent.providers.codex.errors import CODEX_AUTHENTICATION_ERROR_MESSAGE
from clio_agent.providers.codex.model_list import (
    CodexModelListError,
    DirectModel,
    fetch_direct_models,
)
from clio_agent.providers.model_discovery.modality_evidence import modality_evidence
from clio_agent.providers.model_discovery.overlay import CODEX_SOURCE, ProviderDiscoveryResult

#: Modalities the Direct transport delivers beyond what the model list reports,
#: each mapped to the modality-evidence source that proves it.
DIRECT_TRANSPORT_MODALITIES: dict[str, str] = {"pdf": "codex_direct_input_file"}


def _capability_row(model: DirectModel) -> dict[str, Any]:
    reported = list(model.input_modalities)
    if not reported:
        return {
            "capabilities": [],
            "capability_evidence": modality_evidence(
                source="codex_direct_model_list",
                reason="modality_unreported",
                unevidenced=("image", *DIRECT_TRANSPORT_MODALITIES),
            ),
        }
    added = {
        modality: source
        for modality, source in DIRECT_TRANSPORT_MODALITIES.items()
        if modality not in reported
    }
    return {
        "capabilities": [*reported, *added],
        "capability_evidence": modality_evidence(
            source="codex_direct_model_list", reason="modality_reported", added=added
        ),
    }


def _discovered_row(model: DirectModel) -> dict[str, Any]:
    return {
        "id": model.id,
        "name": model.name,
        "description": model.description,
        "context_window": model.context_window,
        # Not reported by the backend's model list: a confirmed unknown, never
        # a value from a community catalog guessing at this model id.
        "output_limit": None,
        "context_source": CODEX_SOURCE,
        **_capability_row(model),
        # The SAME field names the SDK transport's rows use, so the codex
        # thinking dialect reads both transports' rows identically.
        "supported_reasoning_efforts": list(model.reasoning_efforts),
        "default_reasoning_effort": model.default_reasoning_effort,
    }


def discover_codex(
    *, credential_store: CodexCredentialStore | None = None
) -> ProviderDiscoveryResult:
    """Ask the Codex backend for this account's Direct-transport models.

    Args:
        credential_store: Injection seam for tests.

    Returns:
        A result whose ``failed_reason`` is a typed
        :data:`~clio_agent.providers.codex.model_list.DIRECT_MODEL_LIST_REASONS`
        code (or the shared sign-in message when no credential is stored).
    """

    store = credential_store or CodexCredentialStore()
    if not store.is_signed_in():
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=CODEX_AUTHENTICATION_ERROR_MESSAGE,
        )
    try:
        listing = fetch_direct_models(store=store)
    except CodexModelListError as exc:
        return ProviderDiscoveryResult(
            provider="codex", discovered=[], source=CODEX_SOURCE, failed_reason=str(exc)
        )
    return ProviderDiscoveryResult(
        provider="codex",
        discovered=[_discovered_row(model) for model in listing.models],
        source=CODEX_SOURCE,
        default_model=listing.default_model,
    )


__all__ = ["DIRECT_TRANSPORT_MODALITIES", "discover_codex"]
