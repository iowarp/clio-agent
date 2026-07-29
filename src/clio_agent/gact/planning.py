"""Plan-mode variant guidance — the ``plan_workflow`` / ``plan_small`` scaffold shaping (P1.6a #1068).

Owner module for HOW the ``plan_workflow`` / ``plan_small`` enter_mode VARIANTS
(:mod:`clio_agent.gact.agents.skill_effects`) actually shape planning. Those variants enter plan
mode and record a ``plan_variant`` tag on ``session.metadata`` (:data:`PLAN_VARIANT_METADATA_KEY`);
before P1.6a the tag was consumed NOWHERE, so the variants changed nothing. This module is that
consumer: it maps a recorded tag to a :class:`PlanVariantGuidance` — the variant-specific pieces the
plan-mode reminder composer (:func:`clio_agent.gact.plan_mode.inject_plan_mode_reminder`) plugs into
the per-turn plan-mode reminder.

Three postures:

* **default** (no tag) — the pieces reproduce the pre-P1.6 reminder BYTE-FOR-BYTE (regression-locked
  in ``tests/test_gact/test_plan_variants.py``). An effect-less / variant-less plan session is
  unchanged.
* ``plan_workflow`` — a workflow-grade scaffold: an explicit numbered-steps + per-step-verification
  structure hint plus an added Risks & Dependencies plan section. Reminder cadence stays the default.
* ``plan_small`` — a lightweight scaffold: a short-plan structure hint, no extra sections, and a
  SPARSER full-reminder cadence (fewer full re-injects) so a small task carries almost no plan-mode
  overhead.

The tag is read ONLY from the session record (the #948 no-fifth-store projection) — this module
never invents a parallel store. It is also the planned home for P1.6b-d.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from clio_agent.runtime import trace

#: ``session.metadata`` key holding the plan variant tag recorded by
#: ``skill_effects._execute_plan_variant`` (no fifth store — rides the session record).
PLAN_VARIANT_METADATA_KEY = "plan_variant"

#: The recognised variant tags (any other value reads as "no variant" → default guidance).
PLAN_VARIANT_WORKFLOW = "workflow"
PLAN_VARIANT_SMALL = "small"

#: Default full-reminder cadence (turns between full re-injects) — the SINGLE source of truth for
#: the variant-less plan session's cadence, carried verbatim by :data:`DEFAULT_PLAN_VARIANT_GUIDANCE`
#: and consumed by ``plan_mode.inject_plan_mode_reminder`` via ``guidance.full_interval`` (there is
#: no peer constant to keep in agreement).
_DEFAULT_FULL_INTERVAL = 10

#: ``plan_small`` full-reminder cadence — SPARSER than the default (lightweight task, less nagging).
_SMALL_FULL_INTERVAL = 20

#: The default structure-hint bullet body — VERBATIM pre-P1.6 text (the regression-locked baseline).
_STRUCTURE_HINT_DEFAULT = (
    "Structure the plan to fit the task: Simple change → Changes + Verification; Standard "
    "task → Objective, Key Files & Context, Implementation Steps, Verification; Complex / "
    "architectural → Background, Scope, Proposed Solution, Alternatives, a phased Plan, "
    "Verification, Migration/Rollback."
)

#: ``plan_workflow`` structure hint — an explicit, numbered, per-step-verified implementation workflow.
_STRUCTURE_HINT_WORKFLOW = (
    "Structure the plan as an explicit, numbered implementation workflow: number every step in "
    "order, give each step its own concrete verification, and make cross-step ordering and "
    "dependencies explicit."
)

#: ``plan_small`` structure hint — short and lightweight, minimal scaffolding.
_STRUCTURE_HINT_SMALL = (
    "Keep the plan short and lightweight — a brief Changes list plus a one-line Verification; do "
    "not build heavy scaffolding for a small task."
)

#: ``plan_workflow`` extra FULL-reminder bullet — the added Risks & Dependencies plan section.
_WORKFLOW_RISK_BULLET = (
    "Add a Risks & Dependencies section to the plan: the risks, external/ordering dependencies, "
    "and a rollback or mitigation for each."
)


@dataclass(frozen=True)
class PlanVariantGuidance:
    """The variant-specific pieces the plan-mode reminder composer plugs in (P1.6a #1068).

    Attributes:
        variant: The resolved variant tag (``""`` for the default/no-variant posture).
        structure_hint: The body of the FULL reminder's "structure the plan" bullet.
        extra_full_bullets: Additional FULL-reminder bullet bodies (e.g. the workflow variant's
            Risks & Dependencies section); empty for the default and small variants.
        full_interval: Turns between FULL reminder re-injects for this variant (small = sparser).
    """

    variant: str
    structure_hint: str
    extra_full_bullets: tuple[str, ...]
    full_interval: int


#: The default (no-variant) guidance — its pieces reproduce the pre-P1.6 reminder byte-for-byte.
DEFAULT_PLAN_VARIANT_GUIDANCE = PlanVariantGuidance(
    variant="",
    structure_hint=_STRUCTURE_HINT_DEFAULT,
    extra_full_bullets=(),
    full_interval=_DEFAULT_FULL_INTERVAL,
)

_GUIDANCE_BY_VARIANT: dict[str, PlanVariantGuidance] = {
    PLAN_VARIANT_WORKFLOW: PlanVariantGuidance(
        variant=PLAN_VARIANT_WORKFLOW,
        structure_hint=_STRUCTURE_HINT_WORKFLOW,
        extra_full_bullets=(_WORKFLOW_RISK_BULLET,),
        full_interval=_DEFAULT_FULL_INTERVAL,
    ),
    PLAN_VARIANT_SMALL: PlanVariantGuidance(
        variant=PLAN_VARIANT_SMALL,
        structure_hint=_STRUCTURE_HINT_SMALL,
        extra_full_bullets=(),
        full_interval=_SMALL_FULL_INTERVAL,
    ),
}


#: Marker heading the plan-mode reminder block (stable + greppable; #881 discipline).
PLAN_MODE_REMINDER_MARKER = "## Plan Mode active — read-only except the plan file"


def plan_mode_reminder_block(
    *,
    full: bool,
    plan_file: str,
    exists: bool,
    guidance: PlanVariantGuidance | None = None,
    playbook: "Playbook | None" = None,
) -> str:
    """Compose the plan-mode reminder block (full contract or sparse one-liner).

    The FULL block carries the create-vs-edit branch (keyed on ``exists``), an adaptive-structure
    hint, the epistemic-ledger headers, the re-entry staleness note, the show-the-plan rule, the
    read-only restriction, and the turn-ending contract. The SPARSE block is a single line naming
    the restriction + the recorded plan path, so most turns cost almost nothing (do not bloat
    context). ``plan_file`` is the deterministic per-session path recorded on ``session.metadata``.

    ``guidance`` shapes the FULL block per plan VARIANT (P1.6a #1068): its ``structure_hint``
    replaces the structure bullet body and its ``extra_full_bullets`` are appended right after it
    (e.g. the ``plan_workflow`` Risks & Dependencies section). The default guidance carries the
    pre-P1.6 pieces verbatim, so a variant-less session's block is byte-for-byte unchanged.

    ``playbook`` (P1.6b #1068) is the ACTIVE operator playbook (or ``None``). When present its
    ordered, named steps REPLACE the structure bullet as the REQUIRED plan skeleton — the model
    fills the skeleton in rather than inventing structure. This wins over the variant's
    ``structure_hint`` (documented precedence: an operator-supplied skeleton is more specific than
    a variant's generic structure posture); the variant's ``extra_full_bullets`` and reminder
    cadence still compose. ``None`` leaves the block byte-for-byte unchanged.
    """

    guide = guidance if guidance is not None else DEFAULT_PLAN_VARIANT_GUIDANCE
    if not full:
        return (
            PLAN_MODE_REMINDER_MARKER
            + f" ({plan_file}). Keep writing your plan there; end your turn to hand it back "
            "for approval rather than executing it yourself."
        )
    if exists:
        create_or_edit = (
            f"A plan file already exists at {plan_file}. Make incremental edits to it as you learn."
        )
    else:
        create_or_edit = (
            f"No plan file exists yet. Create your plan at {plan_file} (write a *.md there — it is "
            "the ONLY writable path in plan mode)."
        )
    structure = (
        _playbook_structure_bullet(playbook)
        if playbook is not None
        else f"- {guide.structure_hint}"
    )
    bullets = [f"- {create_or_edit}", structure]
    bullets.extend(f"- {extra}" for extra in guide.extra_full_bullets)
    bullets.extend(
        (
            "- Keep an epistemic ledger of what you know vs. must find out, under the headers: "
            "Given / Learned / To look up / To derive.",
            "- If a plan already exists, evaluate whether it is still relevant to THIS task before "
            "editing; treat a new task as a fresh plan.",
            "- Show the plan to the user in your response — don't just write it to disk.",
            "- Turn-ending contract: when the plan is complete, END YOUR TURN and hand it back for "
            "approval — do NOT try to execute the plan while in plan mode.",
        )
    )
    return (
        PLAN_MODE_REMINDER_MARKER + "\n\n"
        "You are in PLAN MODE. Investigate freely, but do NOT modify the system: every write, "
        "edit, and file-mutating tool is blocked.\n" + "\n".join(bullets)
    )


def recorded_plan_variant(session: Any) -> str:
    """Return the plan variant tag recorded on ``session.metadata`` (``""`` when unset/unknown).

    Only the two recognised tags (:data:`PLAN_VARIANT_WORKFLOW` / :data:`PLAN_VARIANT_SMALL`)
    resolve; any other stored value reads as no-variant so a stale/garbage tag degrades to the
    default guidance rather than an error.
    """

    metadata = getattr(session, "metadata", None)
    if isinstance(metadata, Mapping):
        value = metadata.get(PLAN_VARIANT_METADATA_KEY)
        if isinstance(value, str) and value in _GUIDANCE_BY_VARIANT:
            return value
    return ""


def plan_variant_guidance(variant: str) -> PlanVariantGuidance:
    """Resolve the :class:`PlanVariantGuidance` for a variant tag (default for ``""``/unknown)."""

    return _GUIDANCE_BY_VARIANT.get(variant, DEFAULT_PLAN_VARIANT_GUIDANCE)


# =========================================================================== #
# P1.6b #1068 — operator playbooks (CUGA-style plan skeletons)                 #
# =========================================================================== #
#
# An operator supplies a declarative plan SKELETON via a skill that carries a planning effect: an
# ordered list of named steps, each optionally with guidance text and a ``tools_allowed`` list. The
# skeleton is parsed from the skill's TRUSTED frontmatter (never the body — mirroring the skill-effect
# injection-safety invariant), recorded on ``session.metadata`` (:data:`PLAN_PLAYBOOK_METADATA_KEY`,
# no fifth store, exactly like ``plan_variant``), presented by the plan-mode reminder as the required
# plan structure, and its per-step ``tools_allowed`` NARROWS the grant_resolver resolution
# (tighten-only, via :func:`plan_acl.playbook_step_matches`). A playbook clears when the session
# leaves plan mode (``plan_exit`` approve), symmetric with the variant tag.

#: ``session.metadata`` key holding the ACTIVE operator playbook (no fifth store — rides the session
#: record like :data:`PLAN_VARIANT_METADATA_KEY`). An empty ``{}`` (the clear-on-exit tombstone,
#: since a shallow ``sessions.update`` merge cannot delete a key) reads as ABSENT.
PLAN_PLAYBOOK_METADATA_KEY = "plan_playbook"

#: The recognised per-step fields. Any other key in a step mapping is a typed rejection (never a
#: silent drop) — the same total-validation posture as ``skill_effects._autonomy_params``.
_PLAYBOOK_STEP_FIELDS = frozenset({"name", "guidance", "tools_allowed"})


class PlaybookError(ValueError):
    """A playbook declaration could not be parsed/validated (typed reason).

    Carries a machine-readable ``reason`` (``malformed_playbook`` / ``unknown_playbook_field`` /
    ``playbook_step_missing_name`` / ``empty_playbook``) so callers (``skill_effects``) can re-raise
    it as a typed :class:`~clio_agent.gact.agents.skill_effects.SkillEffectError` that reaches
    trace/API — never a silent ignore of a malformed operator playbook.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class PlaybookStep:
    """One named step of an operator playbook (P1.6b #1068).

    Attributes:
        name: The step's short name (required) — becomes a plan section heading.
        guidance: Optional guidance text the reminder surfaces under the step.
        tools_allowed: Optional per-step tool allowlist (globs). When non-empty it NARROWS the
            grant resolution while this step is active (tighten-only); empty imposes no narrowing.
    """

    name: str
    guidance: str = ""
    tools_allowed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Playbook:
    """An operator-supplied plan skeleton: an ordered list of named steps (P1.6b #1068).

    Attributes:
        name: Optional playbook name (the declaring skill's id when recorded from a skill).
        steps: The ordered, non-empty tuple of :class:`PlaybookStep`.
        active_step: Index of the currently-active step (0 in this slice — execution-phase step
            ADVANCEMENT is typed groundwork, not yet driven; see the module notes).
    """

    name: str
    steps: tuple[PlaybookStep, ...]
    active_step: int = 0

    def active(self) -> PlaybookStep:
        """Return the currently-active step (clamped to a valid index)."""

        index = self.active_step if 0 <= self.active_step < len(self.steps) else 0
        return self.steps[index]

    def to_metadata(self) -> dict[str, Any]:
        """Render the JSON-safe ``session.metadata`` projection (no fifth store)."""

        return {
            "name": self.name,
            "active_step": self.active_step,
            "steps": [
                {"name": s.name, "guidance": s.guidance, "tools_allowed": list(s.tools_allowed)}
                for s in self.steps
            ],
        }

    @classmethod
    def from_metadata(cls, data: Any) -> "Playbook | None":
        """Reconstruct a :class:`Playbook` from its ``session.metadata`` projection (or ``None``)."""

        if not isinstance(data, Mapping):
            return None
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            return None
        steps = tuple(
            PlaybookStep(
                name=str(s.get("name") or ""),
                guidance=str(s.get("guidance") or ""),
                tools_allowed=tuple(str(t) for t in (s.get("tools_allowed") or []) if str(t)),
            )
            for s in raw_steps
            if isinstance(s, Mapping)
        )
        if not steps:
            return None
        raw_active = data.get("active_step")
        active_step = (
            raw_active if isinstance(raw_active, int) and not isinstance(raw_active, bool) else 0
        )
        return cls(name=str(data.get("name") or ""), steps=steps, active_step=active_step)


def _parse_playbook_step(item: Any, index: int) -> PlaybookStep:
    """Parse + validate one raw step mapping into a :class:`PlaybookStep` (typed error on bad)."""

    if not isinstance(item, Mapping):
        raise PlaybookError(
            f"playbook step {index} must be a mapping, got {type(item).__name__}",
            reason="malformed_playbook",
        )
    unknown = sorted(str(k) for k in item if str(k) not in _PLAYBOOK_STEP_FIELDS)
    if unknown:
        raise PlaybookError(
            f"playbook step {index} declares unknown field(s) {unknown} "
            f"(known: {sorted(_PLAYBOOK_STEP_FIELDS)})",
            reason="unknown_playbook_field",
        )
    name = str(item.get("name") or "").strip()
    if not name:
        raise PlaybookError(
            f"playbook step {index} has no 'name'", reason="playbook_step_missing_name"
        )
    raw_tools = item.get("tools_allowed")
    if raw_tools is None:
        tools: tuple[str, ...] = ()
    elif isinstance(raw_tools, (list, tuple)):
        tools = tuple(str(t).strip() for t in raw_tools if str(t).strip())
    elif isinstance(raw_tools, str):
        tools = tuple(t.strip() for t in raw_tools.split(",") if t.strip())
    else:
        raise PlaybookError(
            f"playbook step {index} 'tools_allowed' must be a list of tool names, "
            f"got {type(raw_tools).__name__}",
            reason="malformed_playbook",
        )
    return PlaybookStep(
        name=name, guidance=str(item.get("guidance") or "").strip(), tools_allowed=tools
    )


def parse_playbook(raw: Any, *, name: str = "") -> Playbook | None:
    """Parse + validate a declared operator playbook (typed reject on malformed; ``None`` if absent).

    Accepts the two TRUSTED frontmatter forms: a JSON array string (what the hand-rolled SKILL.md
    frontmatter parser stores for a ``playbook: [...]`` key — it must be single-line) or an
    already-parsed ``list``/``tuple`` of step mappings (a real YAML parser / a direct caller).
    ``None`` or a blank string means no playbook. Every failure raises a typed :class:`PlaybookError`
    (no silent drop): non-JSON, a non-list top level, an empty list, a step that is not a mapping,
    a step with an unknown field, or a step with no name.

    Args:
        raw: The declared playbook value (JSON string, list, or ``None``).
        name: Optional playbook name to stamp (e.g. the declaring skill id).

    Returns:
        A validated :class:`Playbook`, or ``None`` when nothing is declared.
    """

    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            data: Any = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise PlaybookError(
                f"playbook is not valid JSON: {exc}", reason="malformed_playbook"
            ) from None
    elif isinstance(raw, (list, tuple)):
        data = list(raw)
    else:
        raise PlaybookError(
            f"playbook must be a JSON array or list of steps, got {type(raw).__name__}",
            reason="malformed_playbook",
        )
    if not isinstance(data, list):
        raise PlaybookError(
            "playbook must be an ordered list of steps", reason="malformed_playbook"
        )
    steps = tuple(_parse_playbook_step(item, index) for index, item in enumerate(data))
    if not steps:
        raise PlaybookError("playbook declares no steps", reason="empty_playbook")
    return Playbook(name=str(name or "").strip(), steps=steps)


#: Top-level skill frontmatter key naming a SAVED-PLAN artifact to derive the playbook from
#: (P1.6c #1068). Owned here (the parse/placement owner); ``plan_reuse`` re-exports it and
#: resolves the reference at effect-apply time.
PLAYBOOK_FROM_PLAN_META_KEY = "playbook_from_plan"


def validate_plan_ref_placement(meta: Any, *, plan_entering: bool, inline: Any) -> None:
    """Typed placement guard for ``playbook_from_plan`` at PARSE time (never a silent ignore).

    Mirrors the inline playbook's misplacement guard: a ``playbook_from_plan`` reference on an
    effect-less or non-plan-entering skill would never reach ``plan_reuse.record_plan_playbook``
    and be silently dropped — a no-silent-fallback violation. Declaring it BESIDE an inline
    ``playbook`` is ambiguous (record would silently prefer the inline one) and likewise rejected.

    Args:
        meta: The skill frontmatter mapping (non-mappings impose nothing).
        plan_entering: Whether the carrying effect enters plan mode.
        inline: The raw inline ``playbook`` declaration (``None``/empty = absent).

    Raises:
        PlaybookError: ``playbook_requires_plan_mode`` on misplacement,
            ``conflicting_playbook_declarations`` when both forms are declared.
    """

    if not isinstance(meta, Mapping):
        return
    plan_ref = str(meta.get(PLAYBOOK_FROM_PLAN_META_KEY) or "").strip()
    if not plan_ref:
        return
    if not plan_entering:
        raise PlaybookError(
            f"playbook_from_plan {plan_ref!r} is only valid on a plan-entering effect "
            "(enter_mode:plan / plan_workflow / plan_small)",
            reason="playbook_requires_plan_mode",
        )
    if inline not in (None, ""):
        raise PlaybookError(
            f"skill declares BOTH an inline playbook and playbook_from_plan {plan_ref!r} — "
            "declare exactly one",
            reason="conflicting_playbook_declarations",
        )


def parse_effect_playbook(raw: Any, *, plan_entering: bool, meta: Any = None) -> Playbook | None:
    """Parse a skill-effect playbook declaration + enforce the plan-only rule (P1.6b #1068).

    Wraps :func:`parse_playbook` (typed :class:`PlaybookError` on malformed) and adds the guard that
    a playbook is a PLAN skeleton: declared beside a non-plan-entering effect it is rejected with a
    typed ``playbook_requires_plan_mode`` reason (never a silent drop). When ``meta`` is supplied,
    the ``playbook_from_plan`` placement guard (:func:`validate_plan_ref_placement`) runs first, so
    a misplaced/conflicting saved-plan reference is typed-rejected at the same seam (P1.6c).
    ``skill_effects`` re-raises the :class:`PlaybookError` as its own :class:`SkillEffectError`.

    Args:
        raw: The declared playbook value (JSON string / list / ``None``).
        plan_entering: Whether the carrying effect enters plan mode (enter_mode:plan / plan variant).
        meta: The full frontmatter mapping (enables the by-reference placement guard).

    Returns:
        The validated :class:`Playbook`, or ``None`` when none is declared.
    """

    validate_plan_ref_placement(meta, plan_entering=plan_entering, inline=raw)
    playbook = parse_playbook(raw)
    if playbook is not None and not plan_entering:
        raise PlaybookError(
            "an operator playbook is only valid on a plan-entering effect "
            "(enter_mode:plan / plan_workflow / plan_small)",
            reason="playbook_requires_plan_mode",
        )
    return playbook


def record_effect_playbook(
    app: Any, session_id: str, playbook: Playbook, *, default_name: str
) -> Playbook:
    """Record a skill-effect playbook, stamping ``default_name`` when unnamed, + emit a typed trace.

    The one recording entry for the skill-effect path: it names the playbook after the declaring
    skill when the declaration left it unnamed (so the reminder shows a meaningful label), persists
    it via :func:`record_playbook`, and emits a greppable trace so the privileged effect is never
    silent. Returns the recorded (possibly renamed) playbook.
    """

    recorded = playbook if playbook.name else replace(playbook, name=default_name)
    record_playbook(app, session_id, recorded)
    trace.event(
        "PLAN",
        "recorded operator playbook %r (%d steps) for %s",
        recorded.name,
        len(recorded.steps),
        session_id,
    )
    return recorded


def record_playbook(app: Any, session_id: str, playbook: Playbook) -> None:
    """Record ``playbook`` as the session's ACTIVE playbook on ``session.metadata`` (no fifth store).

    The whole projection is written under :data:`PLAN_PLAYBOOK_METADATA_KEY`; because
    ``SessionStore.update`` does a shallow merge, writing the whole dict replaces any prior playbook
    wholesale (no stale sub-keys), mirroring ``autonomous_loop._put_loop``.
    """

    app.state.sessions.update(
        session_id, metadata_patch={PLAN_PLAYBOOK_METADATA_KEY: playbook.to_metadata()}
    )


def recorded_playbook(session: Any) -> Playbook | None:
    """Return the ACTIVE playbook recorded on ``session.metadata`` (``None`` when unset/cleared).

    An empty ``{}`` (the clear-on-exit tombstone) reads as absent — matching how ``_get_loop`` /
    the plan-exit pending key treat ``{}`` as ABSENT, since a shallow update merge cannot delete a
    key.
    """

    metadata = getattr(session, "metadata", None)
    if isinstance(metadata, Mapping):
        value = metadata.get(PLAN_PLAYBOOK_METADATA_KEY)
        if isinstance(value, Mapping) and value:
            return Playbook.from_metadata(value)
    return None


def active_playbook_allowed_tools(session: Any) -> tuple[str, ...] | None:
    """Return the ACTIVE playbook step's ``tools_allowed`` allowlist, or ``None`` (no narrowing).

    ``None`` when no playbook is active OR the active step declares no ``tools_allowed`` (the
    optional per-step field) — both cases impose NO narrowing, leaving grant resolution unchanged.
    The gate passes this straight to :func:`grant_resolver.resolve` as ``playbook_allowed``.
    """

    playbook = recorded_playbook(session)
    if playbook is None:
        return None
    return playbook.active().tools_allowed or None


def clear_playbook(app: Any, session_id: str) -> None:
    """Clear the session's active playbook (writes the ``{}`` tombstone). Called on plan_exit approve."""

    app.state.sessions.update(session_id, metadata_patch={PLAN_PLAYBOOK_METADATA_KEY: {}})


# =========================================================================== #
# P1.6c #1068 — save-and-reuse: derive a Playbook skeleton from a saved plan   #
# =========================================================================== #
#
# The GENERALIZE half of save-and-reuse (the REGISTER + REUSE halves are the owner module
# gact/plan_reuse). :func:`playbook_from_saved_plan` is a PURE function: given the markdown body
# of a saved plan artifact, it lifts the plan's structure (numbered implementation steps, else
# section headings) into a :class:`Playbook` skeleton — keeping step names + verification intent,
# stripping session-specific literals (concrete file paths, backtick-quoted values) into
# placeholders where obvious, and dropping per-step tool narrowing (a reusable skeleton must not
# carry one session's concrete allowlist). No I/O, no app state — the same discipline as
# :func:`parse_playbook`.

#: Cap a derived step name so a runaway plan line cannot bloat the skeleton / reminder.
_PLAN_STEP_NAME_MAX = 120

#: A numbered list item (``1.`` / ``2)`` …) — the preferred step source (concrete implementation
#: steps) when the plan carries them.
_PLAN_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.+)$")

#: A section heading (``##``…``####``) — the fallback step source when the plan has no numbered list.
_PLAN_HEADING_RE = re.compile(r"^\s*#{2,4}\s+(.+?)\s*$")

#: A backtick-quoted span → a ``<value>`` placeholder (a session-specific literal value).
_PLAN_BACKTICK_RE = re.compile(r"`[^`]*`")

#: A concrete file path/name → a ``<path>`` placeholder: a token with a path separator, OR a
#: filename-with-known-extension token (targeted so ordinary prose like "e.g." is NOT genericized).
_PLAN_PATH_RE = re.compile(
    r"\S*[\\/]\S+"
    r"|[\w.\-]+\.(?:md|py|txt|json|ya?ml|toml|cfg|ini|sh|js|tsx?|csv|png|jpe?g|h5|parquet|sql|rs|go|cpp|hpp|ipynb)\b",
    re.IGNORECASE,
)


def _genericize_plan_step(text: str) -> str:
    """Strip a plan step's session-specific literals to placeholders (paths/values) + normalise ws.

    Backtick values first (so a path INSIDE backticks becomes one ``<value>``, not a nested
    replace), then bare path tokens, then whitespace collapse + stray markdown emphasis removal.
    """

    cleaned = _PLAN_BACKTICK_RE.sub("<value>", text)
    cleaned = _PLAN_PATH_RE.sub("<path>", cleaned)
    cleaned = cleaned.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def playbook_from_saved_plan(content: str, *, name: str = "") -> Playbook:
    """Derive a generalized :class:`Playbook` skeleton from a saved plan's markdown (P1.6c #1068).

    Pure + deterministic: prefers the plan's numbered implementation steps; falls back to its
    section headings (``##``…``####``) when it has none. Each step's text is genericized
    (:func:`_genericize_plan_step`) so no concrete file path / quoted value from the originating
    session leaks into the reusable skeleton, and per-step ``tools_allowed`` is dropped. Verification
    steps ("… verify …", "Verification") are kept verbatim by name — they carry the plan's intent.

    Args:
        content: The saved plan's markdown body.
        name: The playbook name to stamp (typically the saved plan artifact's name).

    Returns:
        A :class:`Playbook` whose steps mirror the plan's structure.

    Raises:
        PlaybookError: ``reason="unstructured_plan"`` when no numbered step or heading is present —
            a typed reject (never a silent empty playbook).
    """

    numbered: list[str] = []
    headings: list[str] = []
    for line in content.splitlines():
        m = _PLAN_NUMBERED_RE.match(line)
        if m is not None:
            numbered.append(m.group(1))
            continue
        h = _PLAN_HEADING_RE.match(line)
        if h is not None:
            headings.append(h.group(1))
    raw_steps = numbered if numbered else headings
    steps = tuple(
        PlaybookStep(name=cleaned[:_PLAN_STEP_NAME_MAX])
        for cleaned in (_genericize_plan_step(raw) for raw in raw_steps)
        if cleaned
    )
    if not steps:
        raise PlaybookError(
            "saved plan has no derivable step structure (no numbered steps or section headings)",
            reason="unstructured_plan",
        )
    return Playbook(name=str(name or "").strip(), steps=steps)


def _playbook_structure_bullet(playbook: Playbook) -> str:
    """Render the FULL-reminder bullet that presents the playbook skeleton as required structure."""

    label = f' "{playbook.name}"' if playbook.name else ""
    lines = [
        f"- Follow the operator playbook{label} as the REQUIRED plan structure — author one plan "
        "section per numbered step below, in order; do not substitute your own top-level structure:"
    ]
    for index, step in enumerate(playbook.steps, start=1):
        parts = [f"  {index}. {step.name}"]
        if step.guidance:
            parts.append(f" — {step.guidance}")
        if step.tools_allowed:
            parts.append(f" (tools_allowed: {', '.join(step.tools_allowed)})")
        lines.append("".join(parts))
    return "\n".join(lines)
