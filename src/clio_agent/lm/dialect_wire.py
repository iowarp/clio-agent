"""Per-dialect wire placement/spelling for the request builder (model-capabilities
brief Part 7).

This module holds ONLY dialect knowledge -- facts about server SOFTWARE (which
request field a server recognizes, and where it must sit in the JSON body),
never a fact about any one model. It is the single place
:mod:`clio_agent.lm.request_builder` asks "how do I actually send this field
to THIS dialect", after the request builder has already decided WHICH fields
are effective (:mod:`clio_agent.providers.capabilities.accessor`).

**Placement.** Every field this module places lands in one of three spots:

* ``"top"`` -- a first-class ``dspy.LM``/LiteLLM kwarg (``top_p``,
  ``presence_penalty``, ``stop``, and, for the dialects LiteLLM has a real
  provider translator for, ``reasoning_effort``).
* ``"extra_body"`` -- nested under the ``extra_body`` kwarg, which every
  OpenAI-compatible LiteLLM transport (``openai``, ``hosted_vllm``,
  ``lm_studio``) forwards verbatim into the outgoing JSON body. This is where
  every field the brief's supplement table adds (``top_k``, ``min_p``,
  ``repeat_penalty``/``repetition_penalty``, ``chat_template_kwargs``) has
  always been sent (:mod:`clio_agent.lm.factory`'s prior ``_provider_lm_kwargs``
  used exactly this placement for ``top_k``/``min_p``), and it is also where
  this module places a dialect's own non-OpenAI-standard thinking field
  (llama.cpp/vLLM ``reasoning_effort``/``chat_template_kwargs``, Ollama
  ``think``, OpenRouter ``reasoning``/``provider``) -- LiteLLM has no
  dedicated translator for any of these fields on these dialects, so
  ``extra_body`` is the only placement LiteLLM is documented to forward
  as-is. This is a documented assumption (no live server call is in scope for
  this slice's resource budget); Part 9.3's live verification matrix is the
  place to confirm it against a real server and adjust one dialect's
  placement here if it turns out wrong -- nothing else in the request
  builder depends on which spot a field lands in.

Nothing here reaches the network and nothing here reads the effective
capabilities cache; every function is a pure ``(dialect, ...) -> value``
mapping so it is unit-testable without any handshake state.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.capabilities.combine import ThinkingDecision
from clio_agent.providers.capabilities.dialects.openrouter import REQUIRE_PARAMETERS_FLAG

#: Optional fields the brief names (Part 7 item 2) whose WIRE NAME differs by
#: dialect. A field absent from the inner mapping keeps its clio-internal name
#: unchanged. This is the "spelling" half of "the dialect decides placement
#: and spelling" -- vLLM spells the penalty ``repetition_penalty``, llama.cpp
#: spells the same concept ``repeat_penalty``.
PARAM_SPELLING_BY_DIALECT: dict[str, dict[str, str]] = {
    "llama_cpp": {"repetition_penalty": "repeat_penalty"},
    "vllm": {},  # "repetition_penalty" is already vLLM's own spelling.
}

#: The OpenAI-standard optional fields every dialect sends top-level -- LiteLLM's
#: own translator already knows their shape for any dialect.
_TOP_LEVEL_STANDARD_FIELDS: frozenset[str] = frozenset({"top_p", "presence_penalty", "stop"})

#: Fields a SPECIFIC dialect's own LiteLLM translator recognizes top-level
#: beyond the OpenAI-standard set -- e.g. LM Studio's real ``lm_studio``
#: custom_llm_provider already maps ``reasoning_effort`` natively (verified:
#: it appears in ``get_supported_openai_params(custom_llm_provider="lm_studio")``
#: with no supplement-table addition needed, unlike llama.cpp/vLLM where the
#: SAME field name is a supplement-table addition with no native translator at
#: all, and therefore must go through ``extra_body`` instead).
_DIALECT_NATIVE_TOP_LEVEL_FIELDS: dict[str, frozenset[str]] = {
    "lm_studio": frozenset({"reasoning_effort"}),
}


def place_optional_param(extras: dict[str, Any], dialect: str, field: str, value: Any) -> None:
    """Mutate ``extras`` in place, placing one already-gated optional field.

    ``field`` is clio's own internal name (e.g. ``"repetition_penalty"``); this
    spells it for ``dialect`` and puts it top-level (the OpenAI-standard set,
    or a dialect's own natively-translated field,
    :data:`_DIALECT_NATIVE_TOP_LEVEL_FIELDS`) or under ``extra_body`` (every
    other field -- see the module docstring). Callers gate ``field`` against
    the effective accepted-parameter set BEFORE calling this -- this function
    only decides placement/spelling, never whether to send it.
    """

    wire_name = PARAM_SPELLING_BY_DIALECT.get(dialect, {}).get(field, field)
    top_level = (
        field in _TOP_LEVEL_STANDARD_FIELDS
        or field in _DIALECT_NATIVE_TOP_LEVEL_FIELDS.get(dialect, frozenset())
    )
    if top_level:
        extras[wire_name] = value
        return
    body = dict(extras.get("extra_body") or {})
    body[wire_name] = value
    extras["extra_body"] = body


def openrouter_require_parameters(extras: dict[str, Any]) -> None:
    """Set OpenRouter's ``provider: {require_parameters: true}`` (brief 7.3).

    Sent whenever an optional parameter reaches an OpenRouter request, so
    OpenRouter routes only to upstreams that actually honor every field this
    request sends, instead of silently dropping one at a routed provider that
    doesn't support it. :data:`REQUIRE_PARAMETERS_FLAG` is the dialect
    adapter's own constant (:mod:`clio_agent.providers.capabilities.dialects.
    openrouter`) so the spelling is written once.
    """

    body = dict(extras.get("extra_body") or {})
    body.update(REQUIRE_PARAMETERS_FLAG)
    extras["extra_body"] = body


def _effort_value(decision: ThinkingDecision, level: str) -> str | None:
    """The model's own literal wire value for CLIO level ``level``, or ``None``.

    ``None`` (fail closed, never guessed) when the model reports a non-empty
    ``levels`` set and ``level`` isn't in it -- the model told us exactly which
    levels it supports and this isn't one of them. Otherwise
    ``effort_by_level`` is the model's reported CLIO-level -> literal-value
    mapping (brief 4.1 ``ThinkingSpec``); a model with no specific mapping for
    this level is asked for the CLIO level name verbatim -- the common case
    for a served model whose template accepts the same
    ``low``/``medium``/``high`` vocabulary CLIO itself uses.
    """

    spec = decision.spec
    assert spec is not None
    if spec.levels and level not in spec.levels:
        return None
    return spec.effort_by_level.get(level, level)


def thinking_wire(
    dialect: str,
    decision: ThinkingDecision,
    *,
    level: str | None,
    lm_studio_allowed_options: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build the on/off/level thinking kwargs for one HTTP dialect (Part 7 item 4).

    ``decision`` is the effective :class:`ThinkingDecision`
    (:mod:`clio_agent.providers.capabilities.combine`): its ``control`` is
    ``None`` when the model needs no control at all (``none``/``always_on``),
    when nothing is known about the model's thinking mechanism yet (no
    handshake has linked a model record), or when nothing offered can carry
    the model's mechanism ("not controllable here", brief 5.5) -- all three
    cases return ``{}`` here, so a cold or unlinked deployment sends no
    thinking directive at all rather than guessing (fail closed).

    ``level`` is the user's/shipped CLIO level (``None``/``"off"`` means off).
    Only CODEX/CLAUDE_CODE/ANTHROPIC/OPENAI are NOT handled here -- those are
    SDK/CLI transports with their own established, live-verified mapping
    (:func:`clio_agent.providers.thinking.resolve_thinking`); see
    :mod:`clio_agent.lm.request_builder` for why that split is deliberate.
    """

    if decision.spec is None or decision.control is None:
        return {}
    control = decision.control
    off = level is None or level == "off"
    # Guarded by `off` above: every branch below that reaches an
    # `_effort_value` call has a real, non-"off" level string in hand.
    wire_level = "" if off else str(level)

    if dialect == "llama_cpp":
        if control == "reasoning_effort":
            if off:
                return {"reasoning_effort": "none"}
            value = _effort_value(decision, wire_level)
            return {"reasoning_effort": value} if value is not None else {}
        return {"chat_template_kwargs": {"enable_thinking": not off}}

    if dialect == "vllm":
        if control == "thinking_token_budget" and decision.spec.budget_range is not None:
            if off:
                return {"chat_template_kwargs": {"enable_thinking": False}}
            _lo, hi = decision.spec.budget_range
            return {"thinking_token_budget": hi}
        if control == "chat_template_kwargs":
            kwarg = decision.spec.template_kwarg or "enable_thinking"
            if off:
                return {"chat_template_kwargs": {kwarg: False}}
            if kwarg == "enable_thinking":
                return {"chat_template_kwargs": {kwarg: True}}
            # e.g. gpt-oss's own template kwarg IS its effort spelling.
            value = _effort_value(decision, wire_level)
            return {"chat_template_kwargs": {kwarg: value}} if value is not None else {}
        return {}

    if dialect == "ollama":
        if off:
            return {"think": False}
        if decision.spec.levels:
            value = _effort_value(decision, wire_level)
            return {"think": value} if value is not None else {}
        return {"think": True}

    if dialect == "lm_studio":
        if control != "reasoning_effort":
            return {}
        if off:
            return {"reasoning_effort": "none"}
        value = _effort_value(decision, wire_level)
        if value is None:
            return {}
        if lm_studio_allowed_options is not None and value not in lm_studio_allowed_options:
            # Fail closed: LM Studio told us exactly which values this model
            # accepts and this one isn't among them.
            return {}
        return {"reasoning_effort": value}

    if dialect == "openrouter":
        if off:
            return {"reasoning": {"enabled": False}}
        if decision.spec.budget_range is not None and control == "thinking_token_budget":
            _lo, hi = decision.spec.budget_range
            return {"reasoning": {"max_tokens": hi}}
        value = _effort_value(decision, wire_level)
        return {"reasoning": {"effort": value}} if value is not None else {}

    return {}


#: Dialects :func:`thinking_wire` owns directly. Every other configured
#: provider (codex, claude_code, anthropic, openai) keeps the existing
#: SDK/CLI-transport mapping in :mod:`clio_agent.providers.thinking`.
HTTP_DIALECTS: frozenset[str] = frozenset(
    {"llama_cpp", "vllm", "ollama", "lm_studio", "openrouter"}
)


def apply_thinking_wire(extras: dict[str, Any], dialect: str, wire: dict[str, Any]) -> None:
    """Merge one dialect's thinking kwargs (:func:`thinking_wire`) into ``extras``.

    ``wire``'s top-level keys are placed via :func:`place_optional_param`
    (top-level for the OpenAI-standard set or a dialect's own natively-
    translated field, :data:`_DIALECT_NATIVE_TOP_LEVEL_FIELDS` -- e.g. LM
    Studio's ``reasoning_effort`` -- otherwise ``extra_body``), except
    ``chat_template_kwargs`` (merged, since thinking and a user's own
    ``chat_template_kwargs`` override may both want to contribute keys),
    ``thinking_token_budget`` (a LiteLLM-recognized top-level budget kwarg,
    never a clio-invented field), and OpenRouter's ``reasoning``/``provider``
    keys (always ``extra_body`` -- OpenRouter-specific JSON body extensions
    with no OpenAI-standard shape at all).
    """

    for key, value in wire.items():
        if key == "chat_template_kwargs":
            body = dict(extras.get("extra_body") or {})
            merged = dict(body.get("chat_template_kwargs") or {})
            merged.update(value)
            body["chat_template_kwargs"] = merged
            extras["extra_body"] = body
        elif key == "thinking_token_budget":
            # A LiteLLM-recognized top-level budget kwarg (brief 7.4's own
            # control name), not a dialect-invented field -- never extra_body.
            extras[key] = value
        elif dialect == "openrouter":
            body = dict(extras.get("extra_body") or {})
            body[key] = value
            extras["extra_body"] = body
        else:
            place_optional_param(extras, dialect, key, value)


__all__ = [
    "HTTP_DIALECTS",
    "PARAM_SPELLING_BY_DIALECT",
    "apply_thinking_wire",
    "openrouter_require_parameters",
    "place_optional_param",
    "thinking_wire",
]
