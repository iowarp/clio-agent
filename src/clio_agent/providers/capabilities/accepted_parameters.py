"""The user-tunable request settings one model accepts, each with its evidence.

A settings form shows a control for a setting ONLY when the selected model and
its endpoint accept it. This module is the one place that answers "which
settings, with what range, and who says so": :func:`accepted_parameters`
projects the SAME accepted-parameter set the request builder gates on
(:func:`accepted_param_set`) onto :data:`TUNABLES`, the table of settings clio
can actually carry to a model, and returns
:class:`clio_schemas.AcceptedParameter` rows for the catalog.

Rules, all from evidence rather than a guess:

* A setting is offered only when one of its wire names is in the effective
  accepted set (LiteLLM's provider layer + the endpoint dialect supplement,
  narrowed by a route's own list such as OpenRouter ``supported_parameters``),
  or -- for a local server's load settings -- when clio itself applies it when
  loading the model (:data:`LOAD_SETTINGS_BY_DIALECT`). Unknown is not offered.
* Ranges are facts about the server software's API (``dialect`` evidence),
  narrowed by the model's own facts where one exists (``max_tokens`` up to the
  model's output ceiling, ``context_length`` up to its context window).
* ``default`` is the value clio actually sends when nothing is set: the model's
  recommended sampling (``overlay`` evidence), else ``null`` -- the provider's
  own default, nothing sent.
* ``stop`` and thinking controls are deliberately not tunables here: clio owns
  its stop sequences (the adapter's trajectory markers), and thinking has its
  own control (the catalog row's ``reasoning`` block). The two "unknown" rules
  differ on purpose: an unknown accepted set offers and sends no setting,
  while a requested effort for a model whose reasoning levels are unknown
  still passes through (``reasoning_levels_unknown``, lm.dialect_wire).

:func:`validate_settings` checks saved values against this same projection, so
a stored value outside an accepted setting's range is refused at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

from clio_schemas import AcceptedParameter
from clio_schemas.model_capabilities import EvidenceSource

from clio_agent.providers.capabilities import endpoint as capability_endpoint
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.identity import deployment_key, endpoint_key

if TYPE_CHECKING:  # pragma: no cover
    from clio_agent.providers.capabilities.combine import EffectiveCapabilities
    from clio_agent.providers.capabilities.records import Fact

_EVIDENCE_SOURCES: frozenset[str] = frozenset(get_args(EvidenceSource))

_OPENAI_RANGE = "OpenAI-compatible chat completions API"


@dataclass(frozen=True)
class Tunable:
    """One setting clio can carry to a model, and how to enter it.

    Attributes:
        name: The settings key (an ``LMProviderConfig`` / ``PUT
            /v1/providers/lm`` field of the same name).
        wire_names: Accepted-parameter names that evidence this setting (a
            dialect may spell it differently, e.g. ``repeat_penalty``).
        range_detail: Where ``minimum``/``maximum`` come from.
    """

    name: str
    label: str
    description: str
    group: Literal["sampling", "length", "advanced"]
    kind: Literal["number", "integer"]
    minimum: float | None
    maximum: float | None
    step: float
    wire_names: frozenset[str] = field(default_factory=frozenset)
    range_detail: str = ""


#: The settings clio can carry to a model, in display order. A setting absent
#: here is never offered, whatever the endpoint accepts.
TUNABLES: tuple[Tunable, ...] = (
    Tunable(
        "temperature",
        "Temperature",
        "Higher values make replies more varied; lower values more focused.",
        "sampling",
        "number",
        0.0,
        2.0,
        0.05,
        frozenset({"temperature"}),
        f"{_OPENAI_RANGE}: temperature 0 to 2",
    ),
    Tunable(
        "top_p",
        "Top P",
        "Sample only from the most likely tokens whose probabilities add up to this.",
        "sampling",
        "number",
        0.0,
        1.0,
        0.01,
        frozenset({"top_p"}),
        f"{_OPENAI_RANGE}: top_p 0 to 1",
    ),
    Tunable(
        "top_k",
        "Top K",
        "Sample only from this many of the most likely tokens (0 turns it off).",
        "sampling",
        "integer",
        0.0,
        None,
        1.0,
        frozenset({"top_k"}),
        "a token count; 0 disables it on llama.cpp, Ollama and vLLM",
    ),
    Tunable(
        "min_p",
        "Min P",
        "Drop tokens less likely than this fraction of the most likely one.",
        "sampling",
        "number",
        0.0,
        1.0,
        0.01,
        frozenset({"min_p"}),
        "a probability fraction, 0 to 1",
    ),
    Tunable(
        "presence_penalty",
        "Presence penalty",
        "Positive values push the model toward new topics.",
        "sampling",
        "number",
        -2.0,
        2.0,
        0.1,
        frozenset({"presence_penalty"}),
        f"{_OPENAI_RANGE}: presence_penalty -2 to 2",
    ),
    Tunable(
        "frequency_penalty",
        "Frequency penalty",
        "Positive values make the model repeat the same words less.",
        "sampling",
        "number",
        -2.0,
        2.0,
        0.1,
        frozenset({"frequency_penalty"}),
        f"{_OPENAI_RANGE}: frequency_penalty -2 to 2",
    ),
    Tunable(
        "repetition_penalty",
        "Repetition penalty",
        "Values above 1 discourage repeating recent tokens; 1 turns it off.",
        "sampling",
        "number",
        0.0,
        2.0,
        0.05,
        frozenset({"repetition_penalty", "repeat_penalty"}),
        "a multiplier; 1 is off (llama.cpp/Ollama repeat_penalty, vLLM/OpenRouter "
        "repetition_penalty)",
    ),
    Tunable(
        "max_tokens",
        "Longest reply",
        "The most tokens one reply may use.",
        "length",
        "integer",
        1.0,
        None,
        256.0,
        frozenset({"max_tokens", "max_completion_tokens"}),
        "a token count; capped at the model's output limit when a source states it",
    ),
    Tunable(
        "context_length",
        "Context size",
        "How many tokens the model can read at once on this server.",
        "length",
        "integer",
        512.0,
        None,
        1024.0,
        frozenset({"num_ctx"}),
        "a token count; capped at the model's context window when a source states it",
    ),
    Tunable(
        "seed",
        "Seed",
        "The same seed and settings repeat the same reply where the server supports it.",
        "advanced",
        "integer",
        0.0,
        None,
        1.0,
        frozenset({"seed"}),
        f"{_OPENAI_RANGE}: an integer seed",
    ),
    Tunable(
        "parallel",
        "Replies at once",
        "How many replies the server works on at the same time.",
        "advanced",
        "integer",
        1.0,
        None,
        1.0,
        frozenset(),
        "a slot count, at least 1",
    ),
)

TUNABLE_NAMES: frozenset[str] = frozenset(t.name for t in TUNABLES)

#: Dialect knowledge: a range the server software's own API states more
#: narrowly than the generic one.
RANGE_BY_DIALECT: dict[str, dict[str, tuple[float | None, float | None, str]]] = {
    "anthropic": {"temperature": (0.0, 1.0, "Anthropic Messages API: temperature 0 to 1")},
}

#: Dialect knowledge: settings clio applies when it LOADS the model on this
#: server (never a request field), with the server API that takes them.
LOAD_SETTINGS_BY_DIALECT: dict[str, dict[str, str]] = {
    "lm_studio": {
        "context_length": "LM Studio POST /api/v1/models/load context_length",
        "parallel": "LM Studio POST /api/v1/models/load parallel",
    },
}


def is_accepted(tunable: Tunable, accepted: frozenset[str]) -> bool:
    """Whether any of ``tunable``'s wire names is in the accepted set."""

    return bool(tunable.wire_names & accepted)


def tunable(name: str) -> Tunable | None:
    """The :class:`Tunable` named ``name``, or ``None``."""

    return next((t for t in TUNABLES if t.name == name), None)


def accepted_param_set(
    provider_id: str,
    api_base: str,
    model_id: str,
    *,
    dialect: str,
    litellm_prefix: str,
    effective: EffectiveCapabilities,
) -> tuple[frozenset[str], list[tuple[str, Fact[frozenset[str]]]]]:
    """The accepted-parameter set for one deployment, and the facts behind it.

    Local-first, record-refined (the request builder's own rule): the recorded
    effective set (endpoint, narrowed by a route's list, minus a model's
    forbidden parameters) when a handshake has recorded one, else the
    network-free endpoint lookup
    (:func:`~clio_agent.providers.capabilities.endpoint.resolve_accepted_params`).
    An unknown answer is the empty set -- nothing is offered or sent. Each
    fact comes labelled ``"endpoint"`` (what the server software accepts) or
    ``"route"`` (a per-model narrowing such as OpenRouter's list).
    """

    if effective.accepted_params.known and effective.accepted_params.value is not None:
        endpoint = invalidation.get_endpoint_capabilities(endpoint_key(provider_id, api_base))
        deployment = invalidation.get_deployment_capabilities(
            deployment_key(provider_id, api_base, model_id)
        )
        facts = [
            (kind, fact)
            for kind, fact in (
                ("endpoint", endpoint.accepted_params if endpoint else None),
                ("route", deployment.route_params if deployment else None),
            )
            if fact is not None and fact.known
        ]
        return effective.accepted_params.value, facts
    base = capability_endpoint.resolve_accepted_params(
        dialect, model_id, custom_llm_provider=litellm_prefix
    )
    if base.known and base.value:
        return base.value, [("endpoint", base)]
    return frozenset(), []


def _evidence(source: str, detail: str, observed_at: str = "") -> dict[str, str] | None:
    if source not in _EVIDENCE_SOURCES:
        return None
    return {"source": source, "detail": detail, "observed_at": observed_at}


def _membership_evidence(
    spec: Tunable,
    facts: list[tuple[str, Fact[frozenset[str]]]],
    *,
    dialect: str,
    litellm_prefix: str,
) -> list[dict[str, str]]:
    """Who states the endpoint accepts ``spec``: LiteLLM, the dialect supplement, a route."""

    supplement = capability_endpoint.supplement_for(dialect)
    rows: list[dict[str, str]] = []
    for kind, fact in facts:
        names = sorted(spec.wire_names & (fact.value or frozenset()))
        if not names:
            continue
        name = "/".join(names)
        if kind == "route":
            row = _evidence(
                fact.source, f"{name} in {fact.detail or 'the route list'}", fact.observed_at
            )
        elif supplement is not None and supplement.adds & set(names):
            row = _evidence("dialect", f"{name}: {supplement.fills_litellm_gap}", fact.observed_at)
        else:
            row = _evidence(
                fact.source,
                f"litellm get_supported_openai_params(custom_llm_provider={litellm_prefix!r}) "
                f"lists {name}",
                fact.observed_at,
            )
        if row is not None:
            rows.append(row)
    return rows


def _decision_evidence(decision: Any, detail: str) -> list[dict[str, str]]:
    """One row per source behind an effective :class:`Decision` (``+``-joined)."""

    rows = [
        _evidence(source, detail, decision.observed_at or "")
        for source in (decision.source or "").split("+")
    ]
    return [row for row in rows if row is not None]


def _limit(
    spec: Tunable, provider_id: str, api_base: str, model_id: str, effective: EffectiveCapabilities
) -> tuple[int | None, list[dict[str, str]]]:
    """The upper bound a model fact puts on ``spec``, with its evidence rows.

    ``max_tokens``: the effective output limit (model and route/deployment).
    ``context_length``: the MODEL's own context window, never the served one --
    a context size is a load-time choice up to what the weights support.
    """

    if spec.name == "max_tokens":
        decision = effective.output_max
        if not decision.known or not decision.value:
            return None, []
        detail = f"output limit {decision.value} ({decision.reason or decision.decided_by})"
        return decision.value, _decision_evidence(decision, detail)
    if spec.name != "context_length":
        return None, []
    deployment = invalidation.get_deployment_capabilities(
        deployment_key(provider_id, api_base, model_id)
    )
    key = deployment.model_key.value if deployment and deployment.model_key.known else model_id
    model = invalidation.get_model_capabilities(key)
    fact = model.context_max if model is not None else None
    if fact is None or not fact.known or not fact.value:
        return None, []
    detail = f"context window {fact.value} ({fact.detail or 'model context_max'})"
    row = _evidence(fact.source, detail, fact.observed_at)
    return (fact.value, [row]) if row is not None else (None, [])


def _recommended_default(
    spec: Tunable, effective: EffectiveCapabilities
) -> tuple[float | None, list[dict[str, str]]]:
    """The model's recommended value clio sends when the setting is left empty."""

    thinking_spec = effective.thinking.spec
    always_thinking = thinking_spec is not None and thinking_spec.mechanism == "always_on"
    decision = effective.sampling_thinking if always_thinking else effective.sampling_instruct
    if not decision.known or not decision.value or spec.name not in decision.value:
        return None, []
    value = float(decision.value[spec.name])
    mode = "thinking" if always_thinking else "instruct"
    rows = _decision_evidence(
        decision, f"model recommended sampling ({mode} mode): {spec.name}={value:g}"
    )
    return (value, rows) if rows else (None, [])


def accepted_parameters(
    provider_id: str,
    api_base: str,
    model_id: str,
    *,
    dialect: str,
    litellm_prefix: str,
    effective: EffectiveCapabilities,
) -> list[dict[str, Any]]:
    """The :class:`clio_schemas.AcceptedParameter` rows for one catalog model.

    Only settings with evidence appear; an empty list means the model accepts
    no user-tunable response settings (the settings form then shows none).
    """

    accepted, facts = accepted_param_set(
        provider_id,
        api_base,
        model_id,
        dialect=dialect,
        litellm_prefix=litellm_prefix,
        effective=effective,
    )
    load_settings = LOAD_SETTINGS_BY_DIALECT.get(dialect, {})
    rows: list[dict[str, Any]] = []
    for spec in TUNABLES:
        membership: list[dict[str, str] | None]
        if spec.name in load_settings:
            membership = [
                _evidence("dialect", f"clio applies it when loading: {load_settings[spec.name]}")
            ]
        elif is_accepted(spec, accepted):
            membership = list(
                _membership_evidence(spec, facts, dialect=dialect, litellm_prefix=litellm_prefix)
            )
        else:
            continue
        minimum, maximum, range_detail = RANGE_BY_DIALECT.get(dialect, {}).get(
            spec.name, (spec.minimum, spec.maximum, spec.range_detail)
        )
        evidence = [row for row in membership if row is not None]
        if not evidence:
            continue
        evidence.append({"source": "dialect", "detail": range_detail, "observed_at": ""})
        limit, limit_rows = _limit(spec, provider_id, api_base, model_id, effective)
        if limit is not None and limit_rows:
            maximum = float(limit)
            evidence.extend(limit_rows)
        default, default_rows = _recommended_default(spec, effective)
        evidence.extend(default_rows)
        rows.append(
            AcceptedParameter(
                name=spec.name,
                label=spec.label,
                description=spec.description,
                group=spec.group,
                kind=spec.kind,
                minimum=minimum,
                maximum=maximum,
                step=spec.step,
                default=default,
                evidence=evidence,  # type: ignore[arg-type]  # validated by the record
            ).model_dump(mode="json")
        )
    return rows


def accepted_parameters_for(
    preset_id: str,
    provider_kind: str,
    provider_id: str,
    api_base: str,
    model_id: str,
    effective: EffectiveCapabilities,
) -> list[dict[str, Any]]:
    """:func:`accepted_parameters` for a catalog preset, resolving its dialect.

    The dialect and LiteLLM provider come from the preset's own catalog row
    (``litellm_prefix``), the same resolution the request builder uses.
    """

    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

    preset = get_provider(preset_id)
    litellm_prefix = preset.litellm_prefix if preset is not None else provider_kind
    return accepted_parameters(
        provider_id,
        api_base,
        model_id,
        dialect=capability_endpoint.dialect_for_provider(provider_kind, litellm_prefix, preset_id),
        litellm_prefix=litellm_prefix,
        effective=effective,
    )


def validate_settings(
    settings: dict[str, float | int | None], parameters: list[dict[str, Any]]
) -> list[str]:
    """Problems with ``settings`` against the model's accepted ``parameters``.

    A value for an ACCEPTED setting must be the right kind and inside its
    range. A value for a setting the model does not accept is kept (it may
    apply to the next model) and never sent -- that is not a problem here.
    ``None`` means unset.
    """

    by_name = {row["name"]: row for row in parameters}
    problems: list[str] = []
    for name, value in settings.items():
        row = by_name.get(name)
        if row is None or value is None:
            continue
        if row["kind"] == "integer" and not float(value).is_integer():
            problems.append(f"{name}={value} is not a whole number")
            continue
        if row["minimum"] is not None and value < row["minimum"]:
            problems.append(f"{name}={value} is below {row['minimum']:g}")
        elif row["maximum"] is not None and value > row["maximum"]:
            problems.append(f"{name}={value} is above {row['maximum']:g}")
    return problems


__all__ = [
    "LOAD_SETTINGS_BY_DIALECT",
    "RANGE_BY_DIALECT",
    "TUNABLES",
    "TUNABLE_NAMES",
    "Tunable",
    "accepted_param_set",
    "accepted_parameters",
    "accepted_parameters_for",
    "is_accepted",
    "tunable",
    "validate_settings",
]
