"""The DSPy adapters for CLIO's ``dspy.Predict``-style modules (not the agent loop).

The agent loop (:class:`~clio_agent.gact.agents.clio_react.ClioReAct`) calls the LM with a
typed ``dspy.lm15.Request`` and uses no adapter. Everything else that runs a signature
-- the goal judge, the compaction summarizer, AI review, type parsing, non-ReAct
blueprint agents, ``BestOfN``/``Refine`` variants -- goes through the adapter
:func:`create_chat_adapter` returns:

* default: DSPy's ``ChatAdapter`` with a pre-flight context check (an oversize prompt
  is a typed :class:`_ContextOverflowError` before the server sees it) and no JSON
  fallback -- a reply that does not parse is DSPy's typed ``AdapterParseError``, never
  repaired or re-sampled;
* guided output (``lm.guided_output`` / ``CLIO_LM_GUIDED_OUTPUT``, default off): a
  ``JSONAdapter`` that sends a field-name-pinned ``json_schema`` so the server
  constrains generation; it fails typed, never degrading silently.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from clio_agent.config import LMProviderConfig

logger = logging.getLogger(__name__)

_dspy_cache = None


def _dspy():  # type: ignore[no-untyped-def]
    """Return the dspy module, importing it on first call (memoised; boot path)."""
    global _dspy_cache  # noqa: PLW0603
    if _dspy_cache is None:
        import dspy  # noqa: PLC0415

        _dspy_cache = dspy
    return _dspy_cache


def _guided_output_enabled() -> bool:
    """Whether to use guided/structured output (dspy.JSONAdapter) instead of the
    text-protocol ChatAdapter.

    Guided output makes the provider CONSTRAIN generation to the signature's
    output schema (``response_format`` → json_schema when the signature allows,
    else json_object on LM Studio / vLLM), so the structured fields are valid by
    construction instead of relying on the model reproducing the
    ``[[ ## field ## ]]`` text format. This is the reasoning-model fix: qwopus
    drops fields (e.g. ReAct's ``next_tool_name``) under the text protocol; under
    guided output it emits schema-conformant JSON.

    Configurable (``lm.guided_output`` / ``CLIO_LM_GUIDED_OUTPUT``), default OFF
    so models that pass on the text protocol (gpt-oss/gemma/nemotron) are
    untouched; opt in per grind / per model.
    """
    try:
        from clio_agent import conf  # noqa: PLC0415

        return bool(
            conf.resolve(
                "lm.guided_output",
                env="CLIO_LM_GUIDED_OUTPUT",
                default=False,
                cast=conf.as_bool,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break adapter construction
        from clio_agent import conf  # noqa: PLC0415

        try:
            return conf.as_bool(os.environ.get("CLIO_LM_GUIDED_OUTPUT", ""))
        except ValueError:
            return False


def _fix_guided_schema(part: Any) -> None:
    """In-place: pin declared object keys (``additionalProperties=false`` so a
    native-tool-calling model can't substitute its own ``{tool, arguments}``
    shape) while leaving open-ended objects (e.g. ReAct's ``next_tool_args``)
    permissive. Recurses into properties/items/$defs."""
    if not isinstance(part, dict):
        return
    if part.get("type") == "object":
        props = part.get("properties")
        if props:
            part["additionalProperties"] = False
            for sub in props.values():
                _fix_guided_schema(sub)
        else:
            part["additionalProperties"] = True
    if part.get("type") == "array" and isinstance(part.get("items"), dict):
        _fix_guided_schema(part["items"])
    for key in ("$defs", "definitions"):
        for sub in (part.get(key) or {}).values():
            _fix_guided_schema(sub)


def _signature_strict_response_format(signature: Any) -> dict[str, Any]:
    """Build a ``json_schema`` response_format that PINS a DSPy signature's output
    field NAMES (required-as-declared, no extra keys), so a reasoning model that
    natively emits ``{tool, arguments}`` (qwopus) is forced into the requested
    ``{next_thought, next_tool_name, next_tool_args}`` shape.

    Reuses DSPy's pydantic-based schema generation (handles Literal/list/nested),
    but replaces DSPy's open-ended guard+enforce_required (which raises on, or
    over-constrains, ``dict[str, Any]`` leaves) with :func:`_fix_guided_schema`.
    ``strict: false`` because open-ended leaves keep ``additionalProperties:true``
    (incompatible with OpenAI strict mode); LM Studio honors it (verified live).
    """
    import pydantic  # noqa: PLC0415

    fields: dict[str, Any] = {}
    for name, field_info in signature.output_fields.items():
        annotation = field_info.annotation
        default = field_info.default if hasattr(field_info, "default") else ...
        fields[name] = (annotation, default)
    model = pydantic.create_model(
        "ClioGuidedOutputs",
        __config__=pydantic.ConfigDict(extra="forbid"),
        **fields,
    )
    schema = model.model_json_schema()
    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            prop.pop("json_schema_extra", None)
    _fix_guided_schema(schema)
    return {
        "type": "json_schema",
        "json_schema": {"name": "clio_output", "strict": False, "schema": schema},
    }


class _GuidedContextWindowError(ValueError):
    """The formatted guided-output prompt leaves no room for a completion."""


class _ContextOverflowError(ValueError):
    """Prompt exceeds the discovered context window on the non-guided path.

    Raised as a pre-flight check before the LM call so the overflow is a
    diagnosable, typed error ("prompt≈N tokens > context M") rather than an
    opaque HTTP 400 from the server. Set ``lm.context_window`` /
    ``CLIO_LM_CONTEXT_WINDOW`` to assert a larger window when vLLM's
    --max-model-len clips the model's native maximum.
    """


def _guided_prompt_tokens(adapter: Any, signature: Any, demos: Any, inputs: Any) -> int:
    """Conservatively estimate tokens in the exact messages the adapter formats.

    Self-hosted models do not expose their tokenizer through the OpenAI contract.
    Guided mode is opt-in and correctness-sensitive, so use three characters per
    token (including serialized message framing) rather than the runtime's looser
    four-character accounting estimate.
    """

    import json  # noqa: PLC0415

    rendered = adapter.format(signature, demos, inputs)
    payload = json.dumps(rendered, ensure_ascii=False, default=str)
    return max(1, (len(payload) + 2) // 3)


def _check_context_overflow(adapter: Any, signature: Any, demos: Any, inputs: Any) -> None:
    """Pre-flight context check for the non-guided chat path.

    Raises :class:`_ContextOverflowError` when the estimated prompt token count
    exceeds the adapter's ``_clio_context_window``. This surfaces a context
    overflow as a typed, self-explaining error before the server sees the
    request — replacing the opaque HTTP 400 vLLM returns.

    A no-op when the adapter carries no ``_clio_context_window`` (0 / unset).
    The check uses the same conservative 3 chars/token estimate as
    :func:`_guided_prompt_tokens` so both paths share one accounting rule.
    """
    try:
        context_window = int(getattr(adapter, "_clio_context_window", 0) or 0)
    except (TypeError, ValueError):
        context_window = 0
    if context_window <= 0:
        return
    prompt_tokens = _guided_prompt_tokens(adapter, signature, demos, inputs)
    if prompt_tokens >= context_window:
        raise _ContextOverflowError(
            f"prompt≈{prompt_tokens} tokens > context {context_window} "
            f"(model={getattr(adapter, '_clio_model', 'unknown')!r}); "
            "set lm.context_window / CLIO_LM_CONTEXT_WINDOW to override if "
            "the served window is smaller than the model's native maximum"
        )


def _bound_guided_output_kwargs(
    adapter: Any,
    lm: Any,
    lm_kwargs: dict[str, Any],
    signature: Any,
    demos: Any,
    inputs: Any,
) -> dict[str, Any]:
    """Cap guided output to the discovered context remaining after formatting."""

    try:
        context_window = int(getattr(adapter, "_clio_context_window", 0) or 0)
    except (TypeError, ValueError):
        context_window = 0
    if context_window <= 0:
        return lm_kwargs

    prompt_tokens = _guided_prompt_tokens(adapter, signature, demos, inputs)
    reserve = max(64, context_window // 100)
    available = context_window - prompt_tokens - reserve
    if available < 1:
        raise _GuidedContextWindowError(
            "guided-output prompt exceeds the discovered context window "
            f"(estimated_prompt_tokens={prompt_tokens} context_window={context_window} "
            f"reserve_tokens={reserve})"
        )

    defaults = getattr(lm, "kwargs", None)
    default_kwargs = defaults if isinstance(defaults, dict) else {}
    raw_requested = lm_kwargs.get("max_tokens", default_kwargs.get("max_tokens"))
    if raw_requested is None or raw_requested == 0:
        # The server owns the remaining-context budget for uncapped requests.
        return lm_kwargs
    try:
        requested = int(raw_requested) if raw_requested is not None else available
    except (TypeError, ValueError):
        requested = available
    bounded = min(max(1, requested), available)
    if bounded >= requested and "max_tokens" in lm_kwargs:
        return lm_kwargs

    logger.info(
        "guided output max_tokens bounded by discovered context "
        "requested=%d bounded=%d estimated_prompt_tokens=%d context_window=%d",
        requested,
        bounded,
        prompt_tokens,
        context_window,
    )
    return {**lm_kwargs, "max_tokens": bounded}


_STRICT_GUIDED_ADAPTER_CLS: Any = None


def _strict_guided_json_adapter_cls() -> Any:
    """Build (once) a JSONAdapter subclass that sends a field-name-pinned strict
    json_schema (see :func:`_signature_strict_response_format`) instead of DSPy's
    ``{"type":"json_object"}`` fallback.

    DSPy's JSONAdapter falls back to loose ``json_object`` for any signature with
    an open-ended field, and (a) LM Studio rejects that form, (b) loose lets the
    model emit its native ``{tool, arguments}`` keys -> 0 fields parsed. This
    subclass overrides __call__/acall to set our pinned schema and dispatch via
    ChatAdapter (which uses ``self.parse`` = JSONAdapter's JSON parse). An LM that
    does not take ``response_format`` gets stock JSONAdapter prompting; a schema-build
    failure is raised, never degraded silently.
    """
    global _STRICT_GUIDED_ADAPTER_CLS  # noqa: PLW0603
    if _STRICT_GUIDED_ADAPTER_CLS is not None:
        return _STRICT_GUIDED_ADAPTER_CLS
    dspy = _dspy()

    class StrictGuidedJSONAdapter(dspy.JSONAdapter):  # type: ignore[name-defined]
        def _call_preprocess(self, lm, lm_kwargs, signature, inputs):  # type: ignore[no-untyped-def]
            processed = super()._call_preprocess(lm, lm_kwargs, signature, inputs)
            # DSPy's JSONAdapter defaults native function calling ON. When the
            # selected LM does not support it, DSPy leaves a caller-supplied
            # tool_choice in place but adds no tools, which strict vLLM rejects.
            # Enforce the OpenAI request invariant at the final adapter boundary.
            if lm_kwargs.get("tool_choice") is not None and not lm_kwargs.get("tools"):
                lm_kwargs.pop("tool_choice", None)
                lm_kwargs.pop("parallel_tool_calls", None)
            return processed

        def __call__(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            lm_kwargs = _bound_guided_output_kwargs(self, lm, lm_kwargs, signature, demos, inputs)
            if "response_format" in getattr(lm, "supported_params", []):
                kwargs = {
                    **lm_kwargs,
                    "response_format": _signature_strict_response_format(signature),
                }
                return dspy.ChatAdapter.__call__(self, lm, kwargs, signature, demos, inputs)
            return dspy.JSONAdapter.__call__(self, lm, lm_kwargs, signature, demos, inputs)

        async def acall(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            lm_kwargs = _bound_guided_output_kwargs(self, lm, lm_kwargs, signature, demos, inputs)
            if "response_format" in getattr(lm, "supported_params", []):
                kwargs = {
                    **lm_kwargs,
                    "response_format": _signature_strict_response_format(signature),
                }
                return await dspy.ChatAdapter.acall(self, lm, kwargs, signature, demos, inputs)
            return await dspy.JSONAdapter.acall(self, lm, lm_kwargs, signature, demos, inputs)

    _STRICT_GUIDED_ADAPTER_CLS = StrictGuidedJSONAdapter
    return _STRICT_GUIDED_ADAPTER_CLS


_CHAT_ADAPTER_CLS: Any = None
_COMPLETED_MARKER_VARIANT = re.compile(r"(?m)^([ \t]*)\[\[ ## completed(?: ##)? ?\]\]([ \t]*)$")


def _chat_adapter_cls() -> Any:
    """Build (once) DSPy's ChatAdapter with CLIO's pre-flight context check."""
    global _CHAT_ADAPTER_CLS  # noqa: PLW0603
    if _CHAT_ADAPTER_CLS is not None:
        return _CHAT_ADAPTER_CLS
    dspy = _dspy()

    class ClioChatAdapter(dspy.ChatAdapter):  # type: ignore[name-defined]
        """``ChatAdapter`` with context checks and tolerant end-marker spacing."""

        def parse(self, signature: Any, completion: str) -> dict[str, Any]:
            """Accept Codex's spacing and missing-delimiter variants of DSPy's final marker.

            The marker is protocol framing, not field content. Keep every typed
            field and DSPy's normal validation unchanged.
            """
            normalized = _COMPLETED_MARKER_VARIANT.sub(r"\1[[ ## completed ## ]]\2", completion)
            return super().parse(signature, normalized)

        def __call__(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            _check_context_overflow(self, signature, demos, inputs)
            return super().__call__(lm, lm_kwargs, signature, demos, inputs)

        async def acall(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            _check_context_overflow(self, signature, demos, inputs)
            return await super().acall(lm, lm_kwargs, signature, demos, inputs)

    _CHAT_ADAPTER_CLS = ClioChatAdapter
    return _CHAT_ADAPTER_CLS


def create_chat_adapter(config: LMProviderConfig) -> Any:
    """The adapter for this provider's ``dspy.Predict``-style modules (module doc)."""
    adapter = (
        _strict_guided_json_adapter_cls()()
        if _guided_output_enabled()
        else _chat_adapter_cls()(use_json_adapter_fallback=False)
    )
    provider_id = str(getattr(config, "provider_id", "") or getattr(config, "provider", ""))
    if provider_id not in {"codex", "claude_code"}:
        # The discovered window drives the pre-flight overflow check (#1326).
        adapter._clio_context_window = int(
            getattr(config, "chosen_context", None) or getattr(config, "context_window", None) or 0
        )
        adapter._clio_model = str(getattr(config, "model", "") or "")
    return adapter
