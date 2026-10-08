"""``/v1/providers/{provider_id}/working-context`` -- a model's working context (model semantics).

For a model CLIO binds but does not run (a cloud model, someone else's
server), the context control is a number and a Max button: the working context
the agent loop and auto-compaction budget against, bounded by the model's
reported maximum, saved per provider and model
(:mod:`clio_agent.gact.providers.working_context`). Fit to GPU is reported as
unavailable, with the reason.

* ``GET ?model=<id>[&api_base=<url>]`` -- the control
  (:class:`clio_agent.context_sizing.controls.ContextControls`);
* ``PUT`` ``{"model", "choice": "max" | "number", "tokens"?, "api_base"?}`` --
  save it; a bound model applies it from its next turn.

Model ids carry slashes (``Qwen/Qwen3-4B``), so the model travels as a query
parameter or body field rather than a path segment.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from clio_agent.gact.providers import working_context


class WorkingContextRequest(BaseModel):
    """Body of ``PUT /v1/providers/{provider_id}/working-context``."""

    model_config = ConfigDict(extra="forbid")

    model: str
    choice: Literal["max", "number"]
    tokens: int | None = None
    api_base: str = ""


def _bound(app: FastAPI) -> Any:
    """The live agent's ``LMProviderConfig``, or None when nothing is bound."""

    return getattr(getattr(app.state, "agent", None), "_provider_config", None)


def _maximum(app: FastAPI, provider_id: str, model: str, api_base: str) -> tuple[int | None, str]:
    """The model's reported maximum context and where it comes from."""

    bound = _bound(app)
    if (
        bound is not None
        and str(getattr(bound, "provider_id", "")) == provider_id
        and str(getattr(bound, "model", "")) == model
        and (not api_base or api_base == getattr(bound, "api_base", ""))
    ):
        served = getattr(bound, "context_window", None) or getattr(
            bound, "native_context_window", None
        )
        if served:
            return int(served), "the bound deployment's handshake"
        api_base = str(getattr(bound, "api_base", "") or "")
    if not api_base:
        from clio_agent.gact.local_server_store import saved_address_for_preset  # noqa: PLC0415
        from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

        preset = get_provider(provider_id)
        api_base = saved_address_for_preset(provider_id) or (preset.api_base if preset else "")
    from clio_agent.providers.capabilities.accessor import (  # noqa: PLC0415
        get_effective_capabilities,
    )

    context = get_effective_capabilities(provider_id, api_base, model).context
    if context.known and context.value:
        return int(context.value), context.reason or context.decided_by
    return None, "no source states this model's maximum context yet"


def _refresh_bound(app: FastAPI, provider_id: str, model: str) -> None:
    """Re-derive the bound configuration's ``chosen_context`` after a change."""

    bound = _bound(app)
    if bound is None or (str(getattr(bound, "provider_id", "")), str(bound.model)) != (
        provider_id,
        model,
    ):
        return
    from clio_agent import conf  # noqa: PLC0415

    override = conf.resolve(
        "lm.context_window", env="CLIO_LM_CONTEXT_WINDOW", default=0, cast=conf.as_int
    )
    fallback = override if override and override > 0 else getattr(bound, "context_window", None)
    bound.chosen_context = working_context.working_context_for(bound) or fallback
    lm_config = getattr(app.state, "lm_config", None)
    if isinstance(lm_config, dict):
        lm_config["chosen_context"] = bound.chosen_context


def register_working_context_routes(app: FastAPI) -> None:
    """Register the working-context control routes on ``app``."""

    @app.get("/v1/providers/{provider_id}/working-context")
    async def get_working_context(
        provider_id: str, model: str, api_base: str = ""
    ) -> dict[str, Any]:
        """The working-context control of ``model`` on ``provider_id``."""

        maximum, reason = _maximum(app, provider_id, model, api_base)
        return working_context.model_context_controls(
            provider_id, model, maximum=maximum, maximum_reason=reason
        ).model_dump(mode="json")

    @app.put("/v1/providers/{provider_id}/working-context")
    async def put_working_context(
        provider_id: str, request: WorkingContextRequest
    ) -> dict[str, Any]:
        """Save "Max" or a number as the working context of ``request.model``."""

        maximum, reason = _maximum(app, provider_id, request.model, request.api_base)
        tokens: int | None = None
        if request.choice == "number":
            if request.tokens is None:
                raise HTTPException(status_code=422, detail="tokens is required for a number")
            if maximum and request.tokens > maximum:
                raise HTTPException(
                    status_code=422,
                    detail=f"{request.tokens} is above the model's maximum context {maximum}",
                )
            tokens = request.tokens
        try:
            working_context.save_working_context(provider_id, request.model, tokens)
        except working_context.WorkingContextStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _refresh_bound(app, provider_id, request.model)
        return working_context.model_context_controls(
            provider_id, request.model, maximum=maximum, maximum_reason=reason
        ).model_dump(mode="json")


__all__ = ["WorkingContextRequest", "register_working_context_routes"]
