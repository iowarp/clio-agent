"""The generation settings of an explicit ``dspy.lm15.Request``.

DSPy 3.4 does not merge an LM's constructor kwargs into an explicit request
("Generation options belong in Request.config", ``dspy/clients/execution.py``), so a
caller that builds its own ``Request`` (the agent loop) states them itself. This maps
CLIO's LM kwargs (:func:`clio_agent.lm.request_builder.build_request_kwargs`) onto the
universal :class:`dspy.lm15.Config` fields; provider-shaped extras ride
``Config.extensions`` (forwarded verbatim into the provider payload). A kwarg this
module does not know is a typed error, never silently dropped.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dspy.lm15 import Config, Reasoning

__all__ = ["UnmappedGenerationSetting", "config_from_lm_kwargs"]

#: Settings the engine or the LM object own -- not per-request generation options.
_NOT_GENERATION = frozenset(
    {
        "model",
        "model_type",
        "cache",
        "num_retries",
        "api_key",
        "api_base",
        "rollout_id",
    }
)
_UNIVERSAL = ("temperature", "top_p", "top_k", "seed", "frequency_penalty", "presence_penalty")


class UnmappedGenerationSetting(ValueError):
    """An LM kwarg has no ``Request.config`` spelling (it would be silently dropped)."""


def config_from_lm_kwargs(kwargs: Mapping[str, Any]) -> Config:
    """Build the request ``Config`` for an LM's generation kwargs.

    Raises:
        UnmappedGenerationSetting: a kwarg with no ``Config`` field or extension door.
    """
    fields: dict[str, Any] = {}
    extensions: dict[str, Any] = {}
    unmapped: list[str] = []
    for name, value in kwargs.items():
        if value is None or name in _NOT_GENERATION:
            continue
        if name in _UNIVERSAL:
            fields[name] = value
        elif name in ("max_tokens", "max_completion_tokens"):
            fields["max_tokens"] = value
        elif name == "stop":
            fields["stop"] = (value,) if isinstance(value, str) else tuple(value)
        elif name == "reasoning_effort":
            fields["reasoning"] = Reasoning(effort=str(value))
        elif name == "extra_body" and isinstance(value, Mapping):
            # OpenAI-compatible servers read these at the payload top level; lm15
            # forwards extensions verbatim into the payload.
            extensions.update(value)
        else:
            unmapped.append(name)
    if unmapped:
        raise UnmappedGenerationSetting(
            f"LM kwargs with no Request.config spelling: {sorted(unmapped)}"
        )
    if extensions:
        fields["extensions"] = extensions
    return Config(**fields)
