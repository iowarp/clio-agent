"""Refuse a thinking level the selected model does not offer (DIRECTIVES 15, F039).

The picker shows only the catalog's ``reasoning.levels`` for a model; a client
that sends another level through the API gets a typed 422
``thinking_level_unsupported`` instead of a silent accept. The levels come from
the same :func:`~clio_agent.gact.provider_catalog._reasoning_wire_block` the
catalog renders, so the picker and the guard cannot disagree. Only a KNOWN
thinking spec is enforced: an unknown model, an absent handshake, or a model
with no thinking record passes through unchanged.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, ModelRef


def catalog_reasoning_levels(app: Any, model: ModelRef) -> list[str] | None:
    """The catalog's levels for ``model``, or None when its thinking is unknown."""

    report = getattr(app.state, "lm_handshake_report", None)
    if report is None or not report.ok or model.provider_id not in {report.provider_id, ""}:
        return None
    discovered = report.model(model.model_id)
    if discovered is None:
        return None
    from clio_agent.gact.provider_catalog import _reasoning_wire_block  # noqa: PLC0415
    from clio_agent.gact.provider_catalog_snapshot import _resolve_presets  # noqa: PLC0415
    from clio_agent.providers.capabilities.accessor import (  # noqa: PLC0415
        get_effective_capabilities,
    )
    from clio_agent.providers.capabilities.endpoint import dialect_for_provider  # noqa: PLC0415

    effective = get_effective_capabilities(report.provider_id, report.api_base, discovered.id)
    if effective.thinking.spec is None:
        return None
    preset = _resolve_presets(app).get(report.provider_id)
    dialect = (
        dialect_for_provider(preset.provider, preset.litellm_prefix, preset.id)
        if preset is not None
        else dialect_for_provider(report.provider_kind, "", report.provider_id)
    )
    return list(_reasoning_wire_block(effective.thinking, discovered, dialect=dialect)["levels"])


def thinking_level_error(
    app: Any, model: ModelRef, level: str | None, *, session_id: str
) -> ErrorEnvelope | None:
    """A typed error when ``level`` is not one of ``model``'s known levels."""

    if not level:
        return None
    levels = catalog_reasoning_levels(app, model)
    if levels is None or level in levels:
        return None
    return ErrorEnvelope(
        error=ErrorInfo(
            error="thinking_level_unsupported",
            message=(
                f"Thinking level {level!r} is not offered by {model.model_id}; "
                + (f"choose one of {levels}." if levels else "this model has no thinking control.")
            ),
            details={
                "session_id": session_id,
                "provider_id": model.provider_id,
                "model_id": model.model_id,
                "requested": level,
                "supported_levels": levels,
            },
            recoverable=True,
        )
    )


def raise_if_selection_unusable(app: Any, model: ModelRef, level: str | None, sid: str) -> None:
    """Raise a typed 422 for a surrogate chat model or an unsupported thinking level."""

    from fastapi import HTTPException  # noqa: PLC0415

    from clio_agent.gact.model_selection import surrogate_selection_error  # noqa: PLC0415

    error = surrogate_selection_error(app, model.provider_id, model.model_id)
    error = error or thinking_level_error(app, model, level, session_id=sid)
    if error is not None:
        raise HTTPException(status_code=422, detail=error.model_dump(exclude_none=True))
