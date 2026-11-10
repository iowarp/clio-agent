"""Diffusion streaming on the request side (Mercury design: "Show diffusion").

A diffusion model refines a whole answer over a series of denoising steps. When
asked to (Inception's ``diffusing: true``), it streams every step as a SNAPSHOT
of the full current canvas rather than an appended delta. Two facts decide
whether CLIO asks:

* the person's ``show_diffusion`` provider option (off unless set ``on``), and
* the model record's ``stream_shape`` (:attr:`EffectiveCapabilities.stream_shape`)
  -- only a model stated to stream in ``snapshot`` shape gets ``diffusing``.

The model capability drives it, never the provider id, so any route serving a
diffusion model (the direct ``inception`` provider, OpenRouter's
``inception/mercury-3``) behaves the same. An option that is on but cannot be
honoured is logged with a typed reason, never dropped silently. Whether the
request actually asked for diffusion is read back off the LM's own kwargs by the
stream path (:func:`diffusion_requested`), so the request and the stream handling
cannot disagree.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from clio_agent.providers.catalog import SHOW_DIFFUSION_OPTION

if TYPE_CHECKING:  # pragma: no cover
    from clio_agent.config import LMProviderConfig
    from clio_agent.providers.capabilities.combine import EffectiveCapabilities

logger = logging.getLogger(__name__)

#: The request-body flag Inception's chat completions read.
DIFFUSING_FIELD = "diffusing"

#: Typed reasons a ``show_diffusion=on`` option is not sent.
DIFFUSION_NOT_SENT_REASONS: dict[str, str] = {
    "model_stream_shape_unknown": "no source states how this model streams",
    "model_stream_shape_append": "this model streams appended text, not snapshots",
}


def apply_show_diffusion(
    extras: dict[str, Any],
    config: "LMProviderConfig",
    dialect: str,
    effective: "EffectiveCapabilities",
) -> None:
    """Translate the ``show_diffusion`` provider option in ``extras`` in place.

    The option itself never reaches the wire. When it is ``on`` and the model
    streams in snapshot shape, ``extra_body.diffusing`` is set (a request-body
    field on every OpenAI-compatible route). Otherwise a typed reason is logged.
    """

    raw = extras.pop(SHOW_DIFFUSION_OPTION, None)
    if str(raw or "").strip().lower() != "on":
        return
    shape = effective.stream_shape.value
    if shape == "snapshot":
        body = dict(extras.get("extra_body") or {})
        body[DIFFUSING_FIELD] = True
        extras["extra_body"] = body
        logger.info(
            "diffusion_requested provider=%s dialect=%s model=%s stream_shape=snapshot source=%s",
            config.provider_id or config.provider,
            dialect,
            config.model,
            effective.stream_shape.source,
        )
        return
    reason = "model_stream_shape_unknown" if shape is None else "model_stream_shape_append"
    logger.warning(
        "diffusion_not_sent provider=%s dialect=%s model=%s reason=%s detail=%s",
        config.provider_id or config.provider,
        dialect,
        config.model,
        reason,
        DIFFUSION_NOT_SENT_REASONS[reason],
    )


def diffusion_requested(lm: Any) -> bool:
    """Whether ``lm``'s own request kwargs ask the model to stream its denoising steps."""

    kwargs = getattr(lm, "kwargs", None)
    if not isinstance(kwargs, dict):
        return False
    body = kwargs.get("extra_body")
    return isinstance(body, dict) and body.get(DIFFUSING_FIELD) is True


__all__ = [
    "DIFFUSING_FIELD",
    "DIFFUSION_NOT_SENT_REASONS",
    "apply_show_diffusion",
    "diffusion_requested",
]
