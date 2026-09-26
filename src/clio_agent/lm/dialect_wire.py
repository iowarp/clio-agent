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
from clio_agent.providers.thinking_levels import LEVEL_BUDGET

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
#: all, and therefore must go through ``extra_body`` instead). openai/anthropic
#: are real LiteLLM providers that translate ``reasoning_effort``/``thinking``
#: natively; codex/claude_code are CustomLLM transports whose own
#: ``optional_params`` reader expects these exact top-level kwarg names
#: (never a JSON request body, so "extra_body" has no meaning for them at all).
_DIALECT_NATIVE_TOP_LEVEL_FIELDS: dict[str, frozenset[str]] = {
    "lm_studio": frozenset({"reasoning_effort"}),
    "openai": frozenset({"reasoning_effort"}),
    "anthropic": frozenset({"reasoning_effort", "thinking"}),
    "codex": frozenset({"codex_reasoning_effort"}),
    "claude_code": frozenset({"claude_code_thinking"}),
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


def _budget_for_level(decision: ThinkingDecision, level: str, explicit_budget: int) -> int:
    """The token budget to send for ``level`` on a ``budget_tokens`` mechanism.

    An explicit ``thinking_budget`` override (``config.thinking_budget``, a
    real user setting) always wins. Otherwise the model's OWN
    ``budget_range`` (when it reports one) supplies the ceiling; failing
    that, CLIO's generic level -> budget ladder
    (:data:`clio_agent.providers.thinking_levels.LEVEL_BUDGET`) is the
    documented default schedule, never a per-model guess.
    """

    if explicit_budget > 0:
        return explicit_budget
    spec = decision.spec
    assert spec is not None
    if spec.budget_range is not None:
        return spec.budget_range[1]
    return LEVEL_BUDGET.get(level, LEVEL_BUDGET["medium"])


def thinking_wire(
    dialect: str,
    decision: ThinkingDecision,
    *,
    level: str | None,
    lm_studio_allowed_options: tuple[str, ...] | None = None,
    budget_tokens: int = 0,
) -> dict[str, Any]:
    """Build the on/off/level thinking kwargs for one dialect (Part 7 item 4).

    ``decision`` is the effective :class:`ThinkingDecision`
    (:mod:`clio_agent.providers.capabilities.combine`): its ``control`` is
    ``None`` when the model needs no control at all (``none``/``always_on``),
    when nothing is known about the model's thinking mechanism yet (no
    handshake has linked a model record), or when nothing offered can carry
    the model's mechanism ("not controllable here", brief 5.5) -- all three
    cases return ``{}`` here, so a cold or unlinked deployment sends no
    thinking directive at all rather than guessing (fail closed).

    ``level`` is the user's/shipped CLIO level (``None``/``"off"`` means off).
    ``budget_tokens`` is an explicit ``config.thinking_budget`` override, used
    only by the ``budget_tokens``-mechanism dialects (anthropic, claude_code,
    vLLM).

    Covers EVERY dialect the request builder configures an LM for --
    llama.cpp/vLLM/Ollama/LM Studio/OpenRouter (:data:`HTTP_DIALECTS`) and
    codex/claude_code/anthropic/openai (real cloud APIs and CLI/SDK
    transports, still driven by their own real per-model ``ThinkingSpec``
    now, never a second, provider-name-keyed mapping table).
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
        if control == "thinking_token_budget":
            if off:
                return {"chat_template_kwargs": {"enable_thinking": False}}
            return {"thinking_token_budget": _budget_for_level(decision, wire_level, budget_tokens)}
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

    if dialect == "openai":
        # OpenAI reasoning models: reasoning_effort=<level>, or "none" only
        # when the model itself reports a "none"/off effort (some do not
        # accept an explicit off at all -- omitting the field is then correct).
        if off:
            if decision.spec.levels and "off" in decision.spec.levels:
                return {"reasoning_effort": "none"}
            return {}
        value = _effort_value(decision, wire_level)
        return {"reasoning_effort": value} if value is not None else {}

    if dialect == "anthropic":
        if off:
            return {}  # the API default is thinking off; nothing to send.
        if control == "reasoning_effort":
            value = _effort_value(decision, wire_level)
            return {"reasoning_effort": value} if value is not None else {}
        # control == "anthropic_thinking": a token budget (LiteLLM's own
        # `thinking={"type":"enabled","budget_tokens":N}` kwarg).
        budget = _budget_for_level(decision, wire_level, budget_tokens)
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}

    if dialect == "codex":
        # Off sends an explicit "none" (an omitted value inherits the ambient
        # config.toml effort rather than disabling it) -- but ONLY for a model
        # that lists "none": the backend refuses an unlisted effort outright
        # (live 2026-09-26: gpt-6-astra rejects 'none'). For such a model "off"
        # is not offered, and no level means the model's own default effort.
        if off:
            return {"codex_reasoning_effort": "none"} if "off" in decision.spec.levels else {}
        value = _effort_value(decision, wire_level)
        return {"codex_reasoning_effort": value} if value is not None else {}

    if dialect == "claude_code":
        if off:
            return {"claude_code_thinking": {"type": "disabled"}}
        if control == "effort":
            value = _effort_value(decision, wire_level)
            if value is None:
                return {}
            # "display": "summarized" un-redacts the CoT text -- the claude
            # CLI otherwise defaults the thinking display to signature-only.
            return {
                "claude_code_thinking": {
                    "type": "adaptive",
                    "display": "summarized",
                    "effort": value,
                }
            }
        # control == "claude_code_thinking": a token budget.
        budget = _budget_for_level(decision, wire_level, budget_tokens)
        return {
            "claude_code_thinking": {
                "type": "enabled",
                "budget_tokens": budget,
                "display": "summarized",
            }
        }

    return {}


#: The HTTP dialects (a real JSON request body) among the set
#: :func:`thinking_wire` covers -- codex/claude_code/anthropic/openai are ALSO
#: covered by :func:`thinking_wire` (see its docstring) but are not HTTP
#: dialects in this sense, so this set exists only for callers that
#: specifically need to distinguish "has a request body at all" (there are
#: none left in this package; kept for that future distinction rather than
#: deleted, since it is still an accurate, meaningful partition).
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
