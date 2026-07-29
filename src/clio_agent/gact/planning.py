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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
    bullets = [f"- {create_or_edit}", f"- {guide.structure_hint}"]
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
