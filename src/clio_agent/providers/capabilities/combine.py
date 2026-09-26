"""Combine the three capability records into one effective view (brief 5.5).

Pure and cheap: :func:`combine_capabilities` takes the three records (any of
which may be ``None`` when that layer has nothing recorded yet) and returns an
:class:`EffectiveCapabilities`. Nobody edits its result by hand -- callers that
need a fresh view call it again, and :mod:`clio_agent.providers.capabilities.
accessor` is the one place that caches it, keyed by the identity plus the
contributing records' fingerprints (:mod:`clio_agent.providers.capabilities.
invalidation`).

Every field of the result is a :class:`Decision`, which names WHICH record(s)
decided it (``decided_by``) and WHY (``reason``) -- never a bare value. This is
what lets the UI (P7) show "vision: False (deployment: no mmproj loaded)"
instead of a flag with no story.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingMechanism,
    ThinkingSpec,
)

T = TypeVar("T")

#: A thinking mechanism that needs no runtime control at all: nothing to
#: negotiate with the endpoint/deployment.
_NO_CONTROL_NEEDED: frozenset[ThinkingMechanism] = frozenset({"none", "always_on"})

#: For each controllable mechanism, the endpoint "thinking control" names that
#: can carry it, in preference order. This is the "choosing the first control
#: that can carry the model's mechanism" rule (brief 5.5) made explicit and
#: testable. It is dialect-*generic*: knowing that ``reasoning_effort`` can
#: express an effort level is a fact about the WIRE FIELD, not about any one
#: model or server, so it belongs here rather than duplicated per dialect.
#: Building the exact per-dialect request (Part 7) is a later slice; this
#: table only decides WHICH control wins when more than one is offered.
_CONTROL_PRIORITY: dict[ThinkingMechanism, tuple[str, ...]] = {
    "on_off": ("chat_template_kwargs", "think", "reasoning_object"),
    "effort_levels": ("reasoning_effort", "reasoning_object", "chat_template_kwargs", "think"),
    "budget_tokens": ("thinking_token_budget", "anthropic_thinking", "chat_template_kwargs"),
}


@dataclass(frozen=True)
class Decision(Generic[T]):
    """One effective value plus which record(s) decided it, and why.

    Attributes:
        value: The combined value, or ``None`` when no record establishes it.
        decided_by: ``"model"``, ``"endpoint"``, ``"deployment"``, a
            ``"+"``-joined combination of those when more than one
            contributed, or ``"unknown"`` when nothing did.
        reason: A short human-readable explanation, e.g. ``"deployment: no
            mmproj loaded"`` or ``"model: forbidden by gpt-5"``.
        source: The contributing :class:`~clio_agent.providers.capabilities.
            records.Fact`'s own ``source`` (or a ``"+"``-joined set of them,
            when more than one fact agreed), for UI provenance display (brief
            "show provenance, don't tell it" / P7).
        observed_at: The most recent contributing fact's ``observed_at``.
    """

    value: T | None
    decided_by: str
    reason: str = ""
    source: str = ""
    observed_at: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None


def _unknown(reason: str = "no contributing record") -> Decision[T]:
    return Decision(value=None, decided_by="unknown", reason=reason)


def _provenance(*facts: Fact | None) -> tuple[str, str]:
    """``(source, observed_at)`` for a :class:`Decision`, from its winning facts.

    ``source`` is every distinct contributing fact's own source, ``"+"``-joined
    in a stable (sorted) order; ``observed_at`` is the most recent of them
    (their timestamps are all ISO-8601 UTC, so lexicographic max is
    chronological max). Facts that are ``None`` or unknown are ignored.
    """

    known = [fact for fact in facts if fact is not None and fact.known]
    if not known:
        return "", ""
    sources = sorted({fact.source for fact in known})
    observed_at = max((fact.observed_at for fact in known if fact.observed_at), default="")
    return "+".join(sources), observed_at


@dataclass(frozen=True)
class ThinkingDecision:
    """The effective thinking surface: the model's spec plus the chosen wire control.

    ``control`` is the endpoint/deployment control (e.g.
    ``"reasoning_effort"``) chosen to carry ``spec.mechanism``, or ``None``
    when the mechanism needs no control (``none``/``always_on``) or when
    nothing offered can carry it ("not controllable here" -- the UI then shows
    no thinking toggle at all, brief 5.5).
    """

    spec: ThinkingSpec | None
    control: str | None
    decided_by: str
    reason: str = ""
    source: str = ""
    observed_at: str = ""

    @property
    def known(self) -> bool:
        return self.spec is not None


@dataclass(frozen=True)
class EffectiveCapabilities:
    """The combined view of a model/endpoint/deployment triple (brief 5.5)."""

    model_key: str | None
    task: Decision[str]
    context: Decision[int]
    output_max: Decision[int]
    input_modalities: Decision[frozenset[str]]
    tools: Decision[bool]
    parallel_tool_calls: Decision[bool]
    structured_output: Decision[bool]
    accepted_params: Decision[frozenset[str]]
    thinking: ThinkingDecision
    #: Recommended sampling for EACH mode, already narrowed to
    #: ``accepted_params`` -- picking which mode currently applies (thinking
    #: on/off) is a runtime/request-builder decision (Part 7), not this
    #: record's job.
    sampling_thinking: Decision[dict[str, float]] = field(
        default_factory=lambda: _unknown("no model sampling record")
    )
    sampling_instruct: Decision[dict[str, float]] = field(
        default_factory=lambda: _unknown("no model sampling record")
    )
    #: What the model produces (a model-record fact).
    output_modalities: Decision[frozenset[str]] = field(
        default_factory=lambda: _unknown("no source states output modalities")
    )
    #: Subject domains the model is built for (a model-record fact).
    domains: Decision[frozenset[str]] = field(
        default_factory=lambda: _unknown("no source states domains")
    )
    #: Endpoint pricing / cost / routing facts -- deployment-record facts only
    #: (what THIS endpoint charges and whether this id is a router), never a
    #: property of the weights.
    pricing: Decision[dict[str, str]] = field(
        default_factory=lambda: _unknown("no pricing reported")
    )
    free: Decision[bool] = field(default_factory=lambda: _unknown("no pricing reported"))
    router: Decision[bool] = field(default_factory=lambda: _unknown("no router evidence"))


def _tri_and(*facts: tuple[Fact[bool] | None, str]) -> Decision[bool]:
    """Three-valued AND across named facts (brief 5.5): any False wins, else
    True only if every known fact is True, else unknown."""
    known = [(fact.value, owner, fact) for fact, owner in facts if fact is not None and fact.known]
    if not known:
        return _unknown("no boolean evidence from any record")
    false_owners = [(owner, fact) for value, owner, fact in known if value is False]
    if false_owners:
        source, observed_at = _provenance(*(fact for _, fact in false_owners))
        owners = "+".join(owner for owner, _ in false_owners)
        return Decision(False, owners, f"{owners}: not supported", source, observed_at)
    true_owners = [(owner, fact) for value, owner, fact in known if value is True]
    if len(true_owners) == len(known):
        source, observed_at = _provenance(*(fact for _, fact in true_owners))
        owners = "+".join(owner for owner, _ in true_owners)
        return Decision(True, owners, f"{owners}: supported", source, observed_at)
    return _unknown("boolean evidence disagreed in kind (non-bool present)")


def _min_known(*values: tuple[Fact[int] | None, str]) -> Decision[int]:
    """The smaller of the known limits (brief 5.5 context/output rules)."""
    known = [(fact.value, owner, fact) for fact, owner in values if fact is not None and fact.known]
    if not known:
        return _unknown("no limit recorded by any record")
    if len(known) == 1:
        value, owner, fact = known[0]
        source, observed_at = _provenance(fact)
        return Decision(value, owner, f"only {owner} known", source, observed_at)
    minimum = min(value for value, _, _ in known if value is not None)
    winners = [(owner, fact) for value, owner, fact in known if value == minimum]
    reason = f"{'+'.join(o for o, _ in winners)} is the smaller of " + "/".join(
        f"{o}={v}" for v, o, _ in known
    )
    source, observed_at = _provenance(*(fact for _, fact in winners))
    return Decision(minimum, "+".join(o for o, _ in winners), reason, source, observed_at)


def _intersect_modalities(
    model: Fact[frozenset[str]] | None, deployment: Fact[frozenset[str]] | None
) -> Decision[frozenset[str]]:
    model_known = model is not None and model.known
    deploy_known = deployment is not None and deployment.known
    if model_known and deploy_known:
        assert model is not None and deployment is not None and model.value is not None
        assert deployment.value is not None
        combined = model.value & deployment.value
        source, observed_at = _provenance(model, deployment)
        return Decision(
            combined,
            "model+deployment",
            "intersection of model and deployment",
            source,
            observed_at,
        )
    if model_known:
        assert model is not None and model.value is not None
        source, observed_at = _provenance(model)
        return Decision(model.value, "model", "deployment modalities unknown", source, observed_at)
    if deploy_known:
        assert deployment is not None and deployment.value is not None
        source, observed_at = _provenance(deployment)
        return Decision(
            deployment.value, "deployment", "model modalities unknown", source, observed_at
        )
    return _unknown("neither model nor deployment reports modalities")


def _single(fact: Fact[T] | None, owner: str, missing: str) -> Decision[T]:
    """A fact only one record carries, passed through with its provenance."""
    if fact is None or not fact.known:
        return _unknown((fact.detail if fact is not None else "") or missing)
    source, observed_at = _provenance(fact)
    return Decision(fact.value, owner, fact.detail or f"{owner}: reported", source, observed_at)


def _task(model: ModelCapabilities | None) -> Decision[str]:
    """The model's task is a model-record fact alone: no endpoint or deployment narrows it."""
    if model is None or not model.task.known:
        return _unknown(
            (model.task.detail if model is not None else "")
            or "no source states the model task"
        )
    source, observed_at = _provenance(model.task)
    return Decision(
        model.task.value,
        "model",
        model.task.detail or f"model: {model.task.value}",
        source,
        observed_at,
    )


def _effective_params(
    endpoint: Fact[frozenset[str]] | None,
    route: Fact[frozenset[str]] | None,
    forbidden: Fact[frozenset[str]] | None,
) -> Decision[frozenset[str]]:
    if endpoint is None or not endpoint.known:
        return _unknown("endpoint does not report accepted parameters")
    assert endpoint.value is not None
    result = set(endpoint.value)
    owners = ["endpoint"]
    contributing = [endpoint]
    if route is not None and route.known:
        assert route.value is not None
        result &= route.value
        owners.append("deployment route_params")
        contributing.append(route)
    forbidden_hit: set[str] = set()
    if forbidden is not None and forbidden.known:
        assert forbidden.value is not None
        forbidden_hit = result & forbidden.value
        result -= forbidden.value
        if forbidden_hit:
            owners.append("model forbidden_params")
            contributing.append(forbidden)
    reason = f"{'+'.join(owners)}"
    if forbidden_hit:
        reason += f" (removed {sorted(forbidden_hit)})"
    source, observed_at = _provenance(*contributing)
    return Decision(frozenset(result), "+".join(owners), reason, source, observed_at)


def _thinking_decision(
    model: ModelCapabilities | None,
    endpoint: EndpointCapabilities | None,
    deployment: DeploymentCapabilities | None,
) -> ThinkingDecision:
    if model is None or not model.thinking.known:
        return ThinkingDecision(None, None, "unknown", "no model thinking record")
    spec = model.thinking.value
    assert spec is not None
    model_source, model_observed_at = _provenance(model.thinking)
    if spec.mechanism in _NO_CONTROL_NEEDED:
        return ThinkingDecision(
            spec,
            None,
            "model",
            f"mechanism={spec.mechanism}, no control needed",
            model_source,
            model_observed_at,
        )

    offered: set[str] = set()
    owners: list[str] = ["model"]
    contributing: list[Fact[Any]] = [model.thinking]
    if endpoint is not None and endpoint.thinking_controls.known:
        assert endpoint.thinking_controls.value is not None
        offered |= set(endpoint.thinking_controls.value)
        owners.append("endpoint")
        contributing.append(endpoint.thinking_controls)
    # A deployment's template_caps may explicitly deny a control the endpoint
    # otherwise offers (e.g. llama.cpp loaded without --reasoning support);
    # it never GRANTS a control the endpoint doesn't already offer.
    denied: set[str] = set()
    if deployment is not None and deployment.template_caps.known:
        assert deployment.template_caps.value is not None
        caps = deployment.template_caps.value
        if caps.get("supports_reasoning_effort") is False:
            denied.add("reasoning_effort")
        if caps.get("supports_preserve_reasoning") is False:
            denied.add("chat_template_kwargs")
        owners.append("deployment")
        contributing.append(deployment.template_caps)
    usable = offered - denied
    source, observed_at = _provenance(*contributing)

    priority = _CONTROL_PRIORITY.get(spec.mechanism, ())
    for control in priority:
        if control in usable:
            return ThinkingDecision(
                spec,
                control,
                "+".join(owners),
                f"chose {control!r} for {spec.mechanism}",
                source,
                observed_at,
            )
    if not offered:
        return ThinkingDecision(
            spec,
            None,
            "model",
            "not controllable here: endpoint reports no thinking controls",
            model_source,
            model_observed_at,
        )
    return ThinkingDecision(
        spec,
        None,
        "+".join(owners),
        f"not controllable here: none of {sorted(usable) or sorted(offered)} carries {spec.mechanism}",
        source,
        observed_at,
    )


def _filtered_sampling(
    sampling: Fact[dict[str, float]] | None, accepted: Decision[frozenset[str]]
) -> Decision[dict[str, float]]:
    if sampling is None or not sampling.known:
        return _unknown("no model sampling record")
    assert sampling.value is not None
    if not accepted.known:
        # Nothing established the accepted-parameter set, so nothing can be
        # confirmed sendable; fail closed rather than assert every field.
        return _unknown("effective parameter set unknown; sampling withheld")
    assert accepted.value is not None
    narrowed = {key: value for key, value in sampling.value.items() if key in accepted.value}
    source, observed_at = _provenance(sampling)
    return Decision(
        narrowed,
        f"model+{accepted.decided_by}",
        "narrowed to the effective parameter set",
        source,
        observed_at,
    )


def combine_capabilities(
    model: ModelCapabilities | None,
    endpoint: EndpointCapabilities | None,
    deployment: DeploymentCapabilities | None,
) -> EffectiveCapabilities:
    """Combine one model/endpoint/deployment triple into an :class:`EffectiveCapabilities`.

    Any of the three may be ``None`` (no record yet for that layer); the
    result degrades to ``unknown``/no-restriction accordingly rather than
    raising, since a missing record is exactly the "we don't know yet" case
    Part 4.2 and 5.5 describe.
    """

    tools = _tri_and(
        (model.tools if model else None, "model"),
        (deployment.tools_enabled if deployment else None, "deployment"),
    )
    parallel = _tri_and(
        (model.parallel_tool_calls if model else None, "model"),
    )
    structured = _tri_and(
        (model.structured_output if model else None, "model"),
        (
            Fact(
                value=bool(endpoint.structured_output_modes.value)
                if endpoint and endpoint.structured_output_modes.known
                else None,
                source=endpoint.structured_output_modes.source if endpoint else "unknown",
                observed_at=endpoint.structured_output_modes.observed_at if endpoint else "",
            )
            if endpoint
            else None,
            "endpoint",
        ),
    )
    context = _min_known(
        (model.context_max if model else None, "model"),
        (deployment.context_served if deployment else None, "deployment"),
    )
    output_max = _min_known(
        (model.output_max if model else None, "model"),
        (deployment.output_max if deployment else None, "deployment"),
    )
    modalities = _intersect_modalities(
        model.input_modalities if model else None,
        deployment.modalities_enabled if deployment else None,
    )
    accepted = _effective_params(
        endpoint.accepted_params if endpoint else None,
        deployment.route_params if deployment else None,
        model.forbidden_params if model else None,
    )
    thinking = _thinking_decision(model, endpoint, deployment)
    sampling_thinking = _filtered_sampling(model.sampling_thinking if model else None, accepted)
    sampling_instruct = _filtered_sampling(model.sampling_instruct if model else None, accepted)

    model_key = None
    if deployment is not None and deployment.model_key.known:
        model_key = deployment.model_key.value
    elif model is not None:
        model_key = model.model_key

    return EffectiveCapabilities(
        model_key=model_key,
        task=_task(model),
        context=context,
        output_max=output_max,
        input_modalities=modalities,
        tools=tools,
        parallel_tool_calls=parallel,
        structured_output=structured,
        accepted_params=accepted,
        thinking=thinking,
        sampling_thinking=sampling_thinking,
        sampling_instruct=sampling_instruct,
        output_modalities=_single(
            model.output_modalities if model else None, "model", "no source states output modalities"
        ),
        domains=_single(model.domains if model else None, "model", "no source states domains"),
        pricing=_single(
            deployment.pricing if deployment else None, "deployment", "no pricing reported"
        ),
        free=_single(deployment.free if deployment else None, "deployment", "no pricing reported"),
        router=_single(deployment.router if deployment else None, "deployment", "no router evidence"),
    )


__all__ = [
    "Decision",
    "EffectiveCapabilities",
    "ThinkingDecision",
    "combine_capabilities",
]
