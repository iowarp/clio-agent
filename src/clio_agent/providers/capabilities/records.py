"""The three capability records (model-capabilities brief Part 4).

Capabilities come from three different places, and each has a different owner
and a different way of going stale:

* :class:`ModelCapabilities` -- what can these weights do? Shared across every
  endpoint that serves the same model. Never contains a deployment fact (the
  overlay's context value is the model's maximum, never what some server
  loaded).
* :class:`EndpointCapabilities` -- what does this server SOFTWARE accept?
  Keyed by ``(provider_id, api_base)`` (:mod:`clio_agent.providers.identity`).
* :class:`DeploymentCapabilities` -- how is this model loaded on THIS server?
  Keyed by ``(provider_id, api_base, model_id)``. Always live: server
  self-report and probes, never the overlay or catalogs.

Every field on these records is wrapped in a :class:`Fact`, so a value can
never be presented without saying where it came from and when it was last
observed (brief ground rule: "show provenance, don't tell it"). A boolean
:class:`Fact` is three-valued: ``True``, ``False``, or unknown (``value is
None``). A restriction that genuinely does not apply at a layer (a cloud API
has no vision projector to forget) is recorded as ``True`` with
``source="dialect"``, NOT left unknown -- see :func:`no_restriction` and
brief Part 4.2. This distinction feeds the three-valued AND in
:mod:`clio_agent.providers.capabilities.combine`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

T = TypeVar("T")

#: Where a :class:`Fact`'s value came from. Ordered here the same way the
#: model-record precedence in brief 5.1 reads (highest first); endpoint/
#: deployment facts use the subset that applies to them. ``"unknown"`` is not
#: in the brief's literal list -- it exists so a fact that fell through every
#: source can still be a real :class:`Fact` (never a bare ``None``) and be told
#: apart, in tests and provenance UI, from a fact nobody tried to resolve yet.
FactSource = Literal[
    "user",
    "overlay",
    "server_report",
    "hf_repo",
    "models.dev",
    "litellm",
    "db",
    "openrouter",
    "dialect",
    "probe",
    # A compiled-in registry default (e.g. a CLI provider's documented model
    # catalog) -- a candidate, never live evidence. Distinct from
    # "server_report" (a real probe answered) the same way
    # HandshakeReport.models_source distinguishes "static" from "live".
    "catalog",
    "unknown",
]


@dataclass(frozen=True)
class Fact(Generic[T]):
    """One value plus its provenance. ``value is None`` means unknown.

    Attributes:
        value: The fact's value, or ``None`` when unknown.
        source: Where the value came from (or, for an unknown fact, an empty
            attempt marker such as ``"unknown"``).
        observed_at: ISO-8601 timestamp of the EVIDENCE (e.g. the handshake
            probe that produced it), not of whatever code happens to read it.
        detail: Optional human-readable context, e.g. ``"llama.cpp /props
            chat_template_caps.supports_tools"`` or ``"probe: accepted
            silently"``.
    """

    value: T | None
    source: FactSource
    observed_at: str
    detail: str = ""

    @property
    def known(self) -> bool:
        """Whether this fact carries an actual value (as opposed to unknown)."""
        return self.value is not None


def unknown(detail: str = "") -> Fact[Any]:
    """An unknown fact: no source has established a value yet."""
    return Fact(value=None, source="unknown", observed_at="", detail=detail)


def no_restriction(value: T, *, observed_at: str, detail: str) -> Fact[T]:
    """A fact recorded as "this layer does not restrict it" (brief Part 4.2).

    Used for a boolean/set fact that genuinely cannot narrow anything at this
    layer -- for example a cloud endpoint has no vision projector to forget,
    so its deployment's ``modalities_enabled`` is recorded as "every modality"
    with ``source="dialect"`` rather than left unknown. Keeping "we don't
    know" (``unknown()``) separate from "this layer doesn't limit it"
    (``no_restriction(...)``) is what makes the three-valued AND in
    :mod:`combine` correct: an unknown must never silently pass a
    restriction, but a genuine non-restriction must not either.
    """
    return Fact(value=value, source="dialect", observed_at=observed_at, detail=detail)


#: Capability-list strings (Ollama/LM Studio's own vocabulary, an overlay row's
#: persisted list, ...) that normalize to each CLIO modality. Shared so every
#: adapter that reports a capability LIST (rather than a dedicated field)
#: normalizes it identically -- one dict instead of the same if/elif chain
#: copied into each adapter.
_MODALITY_ALIASES: dict[str, tuple[str, ...]] = {
    "image": ("vision", "image", "images", "image_input"),
    "pdf": ("pdf", "document", "documents", "pdf_input"),
    "audio": ("audio", "audio_input"),
    "video": ("video", "video_input"),
}


def modalities_from_capabilities(capabilities: object) -> frozenset[str]:
    """Normalize a provider's raw capability-string list into CLIO's modality vocabulary.

    Every model implicitly accepts ``"text"``. Anything not in
    :data:`_MODALITY_ALIASES` (case/dash-insensitive) is ignored rather than
    guessed at -- an unrecognized capability string names a capability this
    layer has no modality opinion about, not evidence of a new modality.
    """

    normalized = {"text"}
    if isinstance(capabilities, (list, tuple, set, frozenset)):
        for capability in capabilities:
            value = str(capability).strip().lower().replace("-", "_")
            for modality, aliases in _MODALITY_ALIASES.items():
                if value in aliases:
                    normalized.add(modality)
                    break
    return frozenset(normalized)


#: How a model's thinking/reasoning is switched and configured.
ThinkingMechanism = Literal["none", "always_on", "on_off", "effort_levels", "budget_tokens"]


@dataclass(frozen=True)
class ThinkingSpec:
    """A model's own thinking mechanism (never a server's wire spelling of it).

    Attributes:
        mechanism: How thinking is switched for this model.
        levels: CLIO's own levels (``"low"``, ``"medium"``, ...) the model
            supports, when ``mechanism == "effort_levels"``.
        effort_by_level: CLIO level -> the literal value the model's own
            template/API accepts for it (e.g. gpt-oss's ``"low"``/``"medium"``/
            ``"high"``).
        budget_range: ``(min_tokens, max_tokens)`` for ``mechanism ==
            "budget_tokens"``.
        template_kwarg: The chat-template kwarg name that switches thinking
            when the model is driven through a template (e.g.
            ``"enable_thinking"``), independent of which wire control an
            endpoint uses to set it.
    """

    mechanism: ThinkingMechanism = "none"
    levels: tuple[str, ...] = ()
    effort_by_level: dict[str, str] = field(default_factory=dict)
    budget_range: tuple[int, int] | None = None
    template_kwarg: str | None = None


def _unknown_field() -> Any:
    """``default_factory`` for a ``Fact`` field defaulting to unknown."""
    return unknown()


@dataclass(frozen=True)
class ModelCapabilities:
    """What the model's weights can do, shared by every endpoint serving them.

    ``model_key`` is the canonical model identity (brief Part 3 /
    :mod:`clio_agent.providers.identity.ModelKey`): a Hugging Face repo id, a
    cloud model id, or an overlay family name.
    """

    model_key: str
    context_max: Fact[int] = field(default_factory=_unknown_field)
    output_max: Fact[int] = field(default_factory=_unknown_field)
    input_modalities: Fact[frozenset[str]] = field(default_factory=_unknown_field)
    tools: Fact[bool] = field(default_factory=_unknown_field)
    parallel_tool_calls: Fact[bool] = field(default_factory=_unknown_field)
    structured_output: Fact[bool] = field(default_factory=_unknown_field)
    thinking: Fact[ThinkingSpec] = field(default_factory=_unknown_field)
    forbidden_params: Fact[frozenset[str]] = field(default_factory=_unknown_field)
    sampling_thinking: Fact[dict[str, float]] = field(default_factory=_unknown_field)
    sampling_instruct: Fact[dict[str, float]] = field(default_factory=_unknown_field)


@dataclass(frozen=True)
class EndpointCapabilities:
    """What this server SOFTWARE accepts. Keyed by ``(provider_id, api_base)``."""

    provider_id: str
    api_base: str
    dialect: str = ""
    server_version: Fact[str] = field(default_factory=_unknown_field)
    accepted_params: Fact[frozenset[str]] = field(default_factory=_unknown_field)
    thinking_controls: Fact[frozenset[str]] = field(default_factory=_unknown_field)
    structured_output_modes: Fact[frozenset[str]] = field(default_factory=_unknown_field)
    multi_model: bool = False
    #: Invalidation key (brief 5.6): changes when the server itself changes
    #: (llama.cpp ``build_info``, vLLM ``/version``, Ollama ``/api/version``, a
    #: cloud catalog version). Empty when nothing establishes one yet.
    fingerprint: str = ""


@dataclass(frozen=True)
class DeploymentCapabilities:
    """How a model is loaded on one server. Keyed by ``(provider_id, api_base, model_id)``.

    ``model_id`` is the id the SERVER itself uses on the wire (never an alias
    the UI/config uses). Deployment facts come only from the live server or an
    active probe -- never the overlay or a community catalog.
    """

    provider_id: str
    api_base: str
    model_id: str
    #: Link to a :class:`ModelCapabilities.model_key`, decided by
    #: :mod:`clio_agent.providers.capabilities.link` (brief 5.4). Unknown
    #: (``value=None``) means "no link" -- the deployment still works on its
    #: own facts and probes, but model-level facts stay unknown.
    model_key: Fact[str] = field(default_factory=_unknown_field)
    context_served: Fact[int] = field(default_factory=_unknown_field)
    output_max: Fact[int] = field(default_factory=_unknown_field)
    slots: Fact[int] = field(default_factory=_unknown_field)
    modalities_enabled: Fact[frozenset[str]] = field(default_factory=_unknown_field)
    tools_enabled: Fact[bool] = field(default_factory=_unknown_field)
    reasoning_enabled: Fact[bool] = field(default_factory=_unknown_field)
    #: A dialect's own verbatim capability blob, VERBATIM -- llama.cpp's
    #: ``chat_template_caps`` (all-bool), LM Studio's ``reasoning.allowed_options``
    #: (a list of accepted effort-level strings under a synthetic key), etc.
    #: ``Any`` values because this bag is intentionally dialect-shaped, not one
    #: fixed schema; a reader (:mod:`clio_agent.providers.capabilities.combine`,
    #: the Part 7 request builder) knows what its OWN dialect put here.
    template_caps: Fact[dict[str, Any]] = field(default_factory=_unknown_field)
    default_template_kwargs: Fact[dict[str, Any]] = field(default_factory=_unknown_field)
    #: Extra per-route narrowing (e.g. OpenRouter ``supported_parameters``).
    route_params: Fact[frozenset[str]] = field(default_factory=_unknown_field)
    #: Invalidation key (brief 5.6): changes when the LOADED model changes
    #: (llama.cpp ``model_path``, vLLM's ``/v1/models`` row, Ollama ``digest``
    #: + loaded context, LM Studio's model key + loaded context length).
    fingerprint: str = ""


__all__ = [
    "DeploymentCapabilities",
    "EndpointCapabilities",
    "Fact",
    "FactSource",
    "ModelCapabilities",
    "ThinkingMechanism",
    "ThinkingSpec",
    "modalities_from_capabilities",
    "no_restriction",
    "unknown",
]
