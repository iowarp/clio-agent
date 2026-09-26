"""The response settings a ``PUT /v1/providers/lm`` carries: store, echo, validate.

A person's saved settings (temperature, the rest of the sampling surface,
output length, a local server's context size and parallel slots) travel as one
set: every field named by :data:`SETTING_FIELDS` is stored on the bound
configuration and echoed back on ``GET /v1/providers/lm``, so a client can show
what is saved -- including a value the current model does not accept, which is
kept (it may apply to the next model) and never sent (the request builder gates
every field on the model's accepted set).

Before a bind, :func:`validate_request` checks each value against the target
model's accepted parameters (:mod:`clio_agent.providers.capabilities.
accepted_parameters`, the same projection the catalog serves): a value for an
accepted setting outside its range is refused with a typed 422
(``response_setting_out_of_range``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:  # pragma: no cover
    from clio_agent.gact.lm_provider_types import LMProviderRequest

#: Optional sampling fields (``None`` = unset: the provider/model default applies).
SAMPLING_FIELDS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "frequency_penalty",
    "repetition_penalty",
    "seed",
)

#: Every response setting a PUT carries (sizes use ``0`` = unset).
SETTING_FIELDS: tuple[str, ...] = (*SAMPLING_FIELDS, "max_tokens", "context_length", "parallel")

#: The fields ``LMProviderConfig`` takes (``parallel`` is a load-only setting).
_CONFIG_FIELDS: tuple[str, ...] = (*SAMPLING_FIELDS, "max_tokens", "context_length")


def config_kwargs(req: LMProviderRequest) -> dict[str, Any]:
    """The ``LMProviderConfig`` keyword arguments for ``req``'s response settings."""

    return {name: getattr(req, name) for name in _CONFIG_FIELDS}


def stored(req: LMProviderRequest) -> dict[str, Any]:
    """Every response setting on ``req``, for ``app.state.lm_config``/status/events."""

    return {name: getattr(req, name) for name in SETTING_FIELDS}


def echo(cfg: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
    """The ``LMProviderInfo`` sampling/slot fields: a pending bind's, else the bound ones.

    ``temperature``/``max_tokens``/``context_length`` keep their own long-standing
    echo rules in the route; this covers the rest of :data:`SETTING_FIELDS`.
    """

    out: dict[str, Any] = {}
    for name in (*SAMPLING_FIELDS[1:], "parallel"):
        value = pending.get(name) if pending.get(name) is not None else cfg.get(name)
        if value is not None:
            out[name] = value
    return out


def _settings(req: LMProviderRequest) -> dict[str, float | int | None]:
    values: dict[str, float | int | None] = {name: getattr(req, name) for name in SAMPLING_FIELDS}
    for name in ("max_tokens", "context_length", "parallel"):
        size = int(getattr(req, name) or 0)
        values[name] = size if size > 0 else None
    return values


def validate_request(req: LMProviderRequest) -> None:
    """Refuse (typed 422) a value outside the range of a setting the target model accepts.

    Uses whatever capability records are already known for the target
    deployment (the catalog the person picked from recorded them); a setting
    with no evidence is not accepted, so it is stored and never sent rather
    than validated.
    """

    from clio_agent.providers.capabilities import accepted_parameters  # noqa: PLC0415
    from clio_agent.providers.capabilities.accessor import (  # noqa: PLC0415
        get_effective_capabilities,
    )
    from clio_agent.providers.capabilities.endpoint import dialect_for_provider  # noqa: PLC0415
    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

    provider_id = req.provider_id or req.provider
    preset = get_provider(provider_id)
    litellm_prefix = preset.litellm_prefix if preset is not None else req.provider
    parameters = accepted_parameters.accepted_parameters(
        provider_id,
        req.api_base,
        req.model,
        dialect=dialect_for_provider(req.provider, litellm_prefix, provider_id),
        litellm_prefix=litellm_prefix,
        effective=get_effective_capabilities(provider_id, req.api_base, req.model),
    )
    problems = accepted_parameters.validate_settings(_settings(req), parameters)
    if not problems:
        return
    raise HTTPException(
        status_code=422,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="response_setting_out_of_range",
                message=f"{req.model} does not accept: " + "; ".join(problems),
                details={"problems": problems, "model": req.model},
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


__all__ = [
    "SAMPLING_FIELDS",
    "SETTING_FIELDS",
    "config_kwargs",
    "echo",
    "stored",
    "validate_request",
]
