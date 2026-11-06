"""Model selection: which model a session or call runs on, checked against the fleet.

Resolution order when a child session starts (design:
``docs/design/sdk/model-fleet.md``, "Design: selection"):

1. an explicit ``model`` on the call (``spawn_agent_task(..., model=)``,
   ``ctx.call(..., model=)``, a workflow step's ``model``);
2. the target's preferences: the subagent or agent, then its plugins;
3. the caller's model, which is what children got before selection existed.

Every candidate is checked against the target's effective
:class:`~clio_agent.sdk.model.Requires` and the fleet's ``available`` flag. An
explicit model that fails is a typed refusal returned to the caller so its
model can choose another; a failing preference moves down the order and the
child records why. Nothing picks a model the caller did not name: the agent
chooses by calling ``list_models`` and passing ``model``.

A requirement on a fact the fleet does not know (a context size no source
reported) is not a refusal; it is recorded as unverified in the choice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from clio_agent.gact.fleet.entries import FleetEntry
from clio_agent.sdk.model import Requires

ChoiceSource = Literal["explicit", "prefers", "inherited", "person"]

#: The session-metadata key a child's choice is recorded under.
MODEL_CHOICE_KEY = "model_choice"


class ModelSelectionRefused(ValueError):
    """A named model cannot run this target; ``code`` says why, the message says what to do."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = details


@dataclass(frozen=True)
class ModelChoice:
    """What was asked, what runs, why: stored on the session as ``model_choice``."""

    asked: str
    ran: dict[str, str]
    source: ChoiceSource
    #: Why each step down the order was taken, and any requirement left unverified.
    reasons: tuple[str, ...] = ()
    effort: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "reasons": list(self.reasons)}


@dataclass
class Candidate:
    ref: str
    source: ChoiceSource
    origin: str = ""
    effort: str = ""
    notes: list[str] = field(default_factory=list)


def _split(ref: str) -> tuple[str, str]:
    account, sep, model = ref.partition("/")
    return (account, model) if sep else ("", ref)


def resolve_reference(entries: list[FleetEntry], ref: str, *, caller_account: str) -> FleetEntry:
    """The fleet entry ``ref`` names: an exact ``account/model`` or a bare model id or alias.

    A bare id resolves to an account that serves it, preferring the caller's own.

    Raises:
        ModelSelectionRefused: ``model_unknown`` when nothing in the fleet serves it.
    """

    ref = ref.strip()
    exact = next((entry for entry in entries if entry.ref == ref), None)
    if exact is not None:
        return exact
    account, model = _split(ref)
    matches = [
        entry
        for entry in entries
        if (entry.model == model or model in entry.aliases)
        and (not account or entry.account == account)
    ]
    if not matches:
        # A bare id may itself contain a slash (``openai/gpt-oss-120b`` on OpenRouter).
        matches = [entry for entry in entries if entry.model == ref or ref in entry.aliases]
    if not matches:
        known = sorted({entry.ref for entry in entries if entry.available})
        raise ModelSelectionRefused(
            "model_unknown",
            f"no connected account serves {ref!r}; available: {', '.join(known) or 'none'}",
            asked=ref,
            available=known,
        )
    own = [entry for entry in matches if entry.account == caller_account]
    return (own or matches)[0]


def unmet_requirements(
    entry: FleetEntry, requires: Requires, attribution: dict[str, list[str]] | None = None
) -> tuple[list[str], list[str]]:
    """``(unmet, unverified)`` requirement descriptions for ``entry``.

    ``unmet`` names a requirement the model's known facts fail; ``unverified``
    names one the fleet has no fact for.
    """

    attribution = attribution or {}

    def why(key: str) -> str:
        sources = attribution.get(key) or []
        return f" (required by {', '.join(sources)})" if sources else ""

    unmet: list[str] = []
    unverified: list[str] = []
    checks: list[tuple[str, bool | None, str]] = []
    if requires.tool_calling:
        checks.append(("tool_calling", entry.tool_calling, "tool calling"))
    if requires.vision:
        checks.append(("vision", entry.vision, "image input"))
    for key, label, have in (
        ("context_tokens", "context", entry.context_tokens),
        ("output_tokens", "output", entry.output_tokens),
    ):
        need = getattr(requires, key)
        if need:
            ok = None if have is None else have >= need
            checks.append((key, ok, f"{label} of {need:,} tokens (has {have or 'unknown'})"))
    for key, ok, text in checks:
        if ok is None:
            unverified.append(f"{text}{why(key)}: not reported for {entry.ref}")
        elif not ok:
            unmet.append(f"{text}{why(key)}")
    return unmet, unverified


def check_candidate(
    entries: list[FleetEntry],
    candidate: Candidate,
    *,
    requires: Requires,
    attribution: dict[str, list[str]],
    caller_account: str,
) -> FleetEntry:
    """The entry ``candidate`` names, if it can run a target with ``requires``.

    Raises:
        ModelSelectionRefused: ``model_unknown``, ``model_unavailable`` or
            ``model_requirement_unmet``, each naming what to do instead.
    """

    entry = resolve_reference(entries, candidate.ref, caller_account=caller_account)
    if not entry.available:
        raise ModelSelectionRefused(
            "model_unavailable",
            f"{entry.ref} cannot run now ({entry.unavailable_reason}); choose another model",
            model=entry.ref,
            reason=entry.unavailable_reason,
        )
    unmet, unverified = unmet_requirements(entry, requires, attribution)
    if unmet:
        fitting = sorted(
            other.ref
            for other in entries
            if other.available and not unmet_requirements(other, requires, attribution)[0]
        )
        raise ModelSelectionRefused(
            "model_requirement_unmet",
            f"{entry.ref} lacks {'; '.join(unmet)}. Models that meet this target's "
            f"requirements: {', '.join(fitting) or 'none connected'}",
            model=entry.ref,
            unmet=unmet,
            fitting=fitting,
        )
    candidate.notes.extend(f"unverified: {text}" for text in unverified)
    return entry


def choose_model(
    entries: list[FleetEntry],
    *,
    explicit: str,
    prefers_chain: list[dict[str, Any]],
    requires: Requires,
    attribution: dict[str, list[str]],
    inherited: dict[str, str],
    explicit_source: ChoiceSource = "explicit",
) -> ModelChoice:
    """Choose the model a child runs on (the order in the module docstring).

    ``inherited`` is the caller's ``{provider_id, model_id}``; empty ids mean the
    caller runs on the server's active model, which a child then shares.
    ``explicit_source`` records who named ``explicit``: ``"person"`` when the
    person picked it in settings (a SPOTTER watcher's model), which the
    spending guard treats as already consented.

    Raises:
        ModelSelectionRefused: An explicit model, or the inherited one, cannot
            run this target.
    """

    caller_account = str(inherited.get("provider_id") or "")
    reasons: list[str] = []
    # A preference naming only an effort applies to whichever model runs, unless
    # the preference that chose the model names its own.
    effort_only = next((p for p in prefers_chain if not p.get("model") and p.get("effort")), {})
    effort, effort_origin = (
        str(effort_only.get("effort") or ""),
        str(effort_only.get("source") or ""),
    )
    if effort:
        reasons.append(f"effort {effort} from {effort_origin}")
    if explicit.strip():
        candidate = Candidate(explicit.strip(), explicit_source, effort=effort)
        entry = check_candidate(
            entries,
            candidate,
            requires=requires,
            attribution=attribution,
            caller_account=caller_account,
        )
        return _choice(explicit, entry, candidate, reasons)
    for preference in prefers_chain:
        ref = str(preference.get("model") or "")
        if not ref:
            continue
        provider = str(preference.get("provider") or "")
        if provider and "/" not in ref:
            ref = f"{provider}/{ref}"
        candidate = Candidate(
            ref,
            "prefers",
            origin=str(preference.get("source") or ""),
            effort=str(preference.get("effort") or effort),
        )
        try:
            entry = check_candidate(
                entries,
                candidate,
                requires=requires,
                attribution=attribution,
                caller_account=caller_account,
            )
        except ModelSelectionRefused as refusal:
            reasons.append(f"preference {ref} from {candidate.origin} not used: {refusal}")
            continue
        return _choice(ref, entry, candidate, reasons)
    provider_id, model_id = caller_account, str(inherited.get("model_id") or "")
    if not provider_id or not model_id:
        return ModelChoice(
            asked="", ran={}, source="inherited", reasons=tuple(reasons), effort=effort
        )
    candidate = Candidate(f"{provider_id}/{model_id}", "inherited", effort=effort)
    try:
        entry = check_candidate(
            entries,
            candidate,
            requires=requires,
            attribution=attribution,
            caller_account=caller_account,
        )
    except ModelSelectionRefused as refusal:
        if refusal.code != "model_requirement_unmet":
            # The caller's own model is not in the fleet's view (a model the
            # catalog has not listed): it keeps running there, and that is recorded.
            reasons.append(f"inherited model not checked: {refusal}")
            return ModelChoice(
                asked="",
                ran={"provider_id": provider_id, "model_id": model_id},
                source="inherited",
                reasons=tuple(reasons),
                effort=effort,
            )
        raise
    return _choice("", entry, candidate, reasons)


def _choice(asked: str, entry: FleetEntry, candidate: Candidate, reasons: list[str]) -> ModelChoice:
    note = [f"from {candidate.origin}"] if candidate.origin else []
    return ModelChoice(
        asked=asked,
        ran={"provider_id": entry.account, "model_id": entry.model},
        source=candidate.source,
        reasons=(*reasons, *note, *candidate.notes),
        effort=candidate.effort,
    )


__all__ = [
    "MODEL_CHOICE_KEY",
    "ModelChoice",
    "ModelSelectionRefused",
    "check_candidate",
    "choose_model",
    "resolve_reference",
    "unmet_requirements",
]
