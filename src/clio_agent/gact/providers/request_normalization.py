"""Normalize LM provider bind requests against the provider catalog."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from fastapi import HTTPException

from clio_agent.gact.providers.config import _provider_runtime_kind
from clio_agent.gact.types import LMProviderPreset, LMProviderRequest


def normalize_lm_provider_request(
    req: LMProviderRequest,
    presets: Sequence[LMProviderPreset],
    default_model_for: Callable[[LMProviderPreset], str],
) -> LMProviderRequest:
    """Resolve catalog ids, provider options, and an omitted default model."""

    requested_id = req.provider_id or req.provider
    preset = next((item for item in presets if item.id == requested_id), None)
    if preset is None:
        from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

        catalog_provider = get_provider(requested_id)
        if catalog_provider is not None:
            preset = next((item for item in presets if item.id == catalog_provider.id), None)
    if preset is None:
        return req

    from clio_agent.providers.catalog import normalize_provider_options  # noqa: PLC0415

    try:
        provider_options = normalize_provider_options(preset.id, req.provider_options)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    provider_kind = _provider_runtime_kind(preset.id)
    default_model = req.model or default_model_for(preset)
    if (
        provider_kind == req.provider
        and preset.id == req.provider_id
        and default_model == req.model
        and provider_options == req.provider_options
    ):
        return req
    return req.model_copy(
        update={
            "provider_id": preset.id,
            "provider": provider_kind,
            "api_base": req.api_base or preset.api_base,
            "model": default_model,
            "provider_options": provider_options,
        }
    )
