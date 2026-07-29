"""Skill privileged-effects substrate (P1.0 #1062, campaign #1057).

Skills are procedural knowledge loaded as TEXT (``skill_runtime.load_skill`` returns the
SKILL.md body). This module adds a **declared, runtime-executed privileged EFFECT** to the
skill contract: a structured capability the RUNTIME performs when a skill is invoked, in
addition to / instead of returning the body.

The effect is **authored metadata (trusted)** — it is read ONLY from the skill's YAML
frontmatter (:attr:`SkillRef.meta`), NEVER from the skill BODY text, a read file, or model
output. A body containing the string ``effect: enter_mode`` therefore has ZERO runtime
effect (injection-safety): only the DECLARED ``effect:`` frontmatter key of an *invoked*
skill triggers the runtime action.

Two effects shipped in P1.0:

* ``enter_mode`` — transition the session mode via the real session-update path
  (:meth:`SessionStore.update`), after which the plan-mode machinery (P1.1/1.2/1.3) takes
  over. **Security: enter_mode may only TIGHTEN** — enter a MORE-restrictive posture
  (edit → architect → plan). It can NEVER relax/leave a restrictive mode (e.g. plan → edit):
  exiting plan mode is the user-gated ``plan_exit`` approval flow (P1.4). An enter_mode
  effect that would weaken the current mode is REJECTED with a typed reason, mode unchanged.
* ``spawn_subagent_with_skill`` — run the skill in a FRESH subagent (a real child turn via
  the spawn substrate) seeded with the skill body, instead of inlining the body into the
  caller's context (parity with Claude Code / Codex skill-as-subagent). Returns the task
  handle; the body is NOT inlined.

P4.4 (#1082) GENERALIZES this vocabulary with the **autonomy effects** — each a declared
frontmatter bundle over the SAME primitives the ``/loop``·``/goal``·``/cron`` surfaces use,
so "run skill X daily until Y holds" is ONE declared, injection-safe skill:

* ``loop`` — arm a self-paced cross-turn loop for this skill's work via
  :func:`clio_agent.gact.autonomous_loop.start_loop` (#P4.1) with the declared typed bounds
  (``max_iters`` / ``interval_s`` / ``max_wallclock_s`` / ``max_tokens`` / ``max_usd``). STILL
  bounded — an unset bound resolves to a finite hard default and the delay is clamped.
* ``set_goal`` — arm a run-until-``<condition>`` goal via :func:`clio_agent.gact.goal.arm_goal`
  (#P4.2). The SANCTIONED skill-arming door (a DECLARED, trusted effect like ``/goal``; NOT model
  self-arming per #1080). Completion is decided by the bounded LLM judge and the typed loop bounds
  are the hard stops (the deterministic goal-predicate tier was deleted, A4 #1057).
* ``schedule`` — register a cron / ``run_at`` / ``delay_s`` schedule via the P4.3
  :meth:`ScheduleStore.create` (#P4.3). STILL clamped — a sub-floor cron raises the typed
  :class:`~clio_agent.gact.scheduler.CronError` (min-interval), surfaced as a :class:`SkillEffectError`.
* ``plan_workflow`` / ``plan_small`` — ``enter_mode`` VARIANTS (P1.6): enter plan mode with a
  variant tag (a different attachment/thinking budget). Because plan is the strictest mode, they
  inherit the enter_mode no-relax guard verbatim — they can only ENTER plan, never escape.

**The P1.0 invariants apply VERBATIM to every autonomy effect.** INJECTION-SAFE: the effect is
read ONLY from the DECLARED ``effect:`` frontmatter of an invoked skill — a body / read file /
model output containing ``effect: loop`` has ZERO runtime effect. NO PRIVILEGE ESCALATION: a
``loop`` / ``schedule`` / ``set_goal`` effect neither touches the session mode nor over-grants
(its re-driven/scheduled turns run in the SAME session under the SAME mode gate) and routes
through the SAME clamped infra (#P4.1/#P4.2/#P4.3 + the goal two-tier gate), so it cannot evade
the anti-runaway. A ``plan_workflow`` / ``plan_small`` effect is a plan-mode enter, never a relax.

Validation is typed and total: an unknown ``kind`` (or a malformed spec) raises
:class:`SkillEffectError` — never a silent ignore (no-silent-fallback ground rule).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.gact import context as _ctx
from clio_agent.gact import plan_reuse, planning
from clio_agent.gact.skills import read_skill_body
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.planning import Playbook
    from clio_agent.gact.skills import SkillRef

#: The declared effect kinds. An ``effect.kind`` outside this set is a typed validation
#: error, never a silent skip. P1.0 shipped ``enter_mode`` + ``spawn_subagent_with_skill``;
#: P4.4 (#1082) adds the autonomy effects ``loop`` / ``set_goal`` / ``schedule`` and the
#: ``plan_workflow`` / ``plan_small`` enter_mode variants.
EFFECT_ENTER_MODE: Literal["enter_mode"] = "enter_mode"
EFFECT_SPAWN_SUBAGENT: Literal["spawn_subagent_with_skill"] = "spawn_subagent_with_skill"
EFFECT_LOOP: Literal["loop"] = "loop"
EFFECT_SET_GOAL: Literal["set_goal"] = "set_goal"
EFFECT_SCHEDULE: Literal["schedule"] = "schedule"
EFFECT_PLAN_WORKFLOW: Literal["plan_workflow"] = "plan_workflow"
EFFECT_PLAN_SMALL: Literal["plan_small"] = "plan_small"

#: The ``enter_mode`` VARIANTS (P1.6): each enters PLAN mode carrying a variant tag (a
#: different attachment / thinking budget). The tag rides through as
#: :attr:`SkillEffect.plan_variant` and is recorded on the session + provenance event; the
#: transition itself reuses the tighten-only ``enter_mode`` path (plan is strictest, so a
#: variant can never relax a restrictive mode).
_PLAN_VARIANTS: dict[str, str] = {
    # Values are the consumer's own constants (gact.planning, the tag READER) so the
    # writer and reader can never drift apart silently.
    EFFECT_PLAN_WORKFLOW: planning.PLAN_VARIANT_WORKFLOW,
    EFFECT_PLAN_SMALL: planning.PLAN_VARIANT_SMALL,
}

_KNOWN_EFFECT_KINDS = frozenset(
    {
        EFFECT_ENTER_MODE,
        EFFECT_SPAWN_SUBAGENT,
        EFFECT_LOOP,
        EFFECT_SET_GOAL,
        EFFECT_SCHEDULE,
        EFFECT_PLAN_WORKFLOW,
        EFFECT_PLAN_SMALL,
    }
)

#: Session modes and their restrictiveness RANK (higher = MORE restrictive). ``enter_mode``
#: may only move to an equal-or-more-restrictive posture (target rank >= current rank); a
#: strictly-lower target would RELAX the mode and is rejected (the no-escape guard). ``plan``
#: is strictest (read-only + sole plan-file write, turn-ending); ``architect`` proposes diffs
#: (no direct writes); ``edit`` has full write authority (least restrictive). These are the
#: three valid session modes after P1.1 deleted ``chat`` (see ``sessions.Session.mode``).
_MODE_RESTRICTIVENESS: dict[str, int] = {"edit": 0, "architect": 1, "plan": 2}


class SkillEffectError(RuntimeError):
    """A declared skill effect could not be validated or performed (typed reason).

    Carries a machine-readable ``reason`` (e.g. ``unknown_effect_kind``,
    ``unknown_effect_param``, ``invalid_mode``, ``no_active_session``, ``mode_update_failed``,
    ``spawn_refused``) so callers/audit can branch without string-matching the message.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class SkillModeTransitionError(SkillEffectError):
    """An ``enter_mode`` effect was rejected because it would RELAX the current mode.

    The no-escape invariant: a skill effect can only tighten the posture, never leave/weaken
    a restrictive mode (exiting plan mode is the user-gated ``plan_exit`` flow, P1.4).
    """

    def __init__(self, current: str, target: str) -> None:
        super().__init__(
            f"enter_mode effect refused: cannot relax mode {current!r} -> {target!r} "
            "(a skill effect may only ENTER a more-restrictive mode; leaving plan mode is "
            "the user-gated plan_exit approval flow)",
            reason="mode_relax_denied",
        )
        self.current = current
        self.target = target


_EffectKind = Literal[
    "enter_mode",
    "spawn_subagent_with_skill",
    "loop",
    "set_goal",
    "schedule",
    "plan_workflow",
    "plan_small",
]


@dataclass(frozen=True)
class SkillEffect:
    """A validated, declared skill effect (parsed from trusted frontmatter metadata).

    The autonomy effects (``loop`` / ``set_goal`` / ``schedule``) carry their already-typed,
    already-validated declared arguments in :attr:`params` (numeric bounds coerced to int /
    float at parse time, so a malformed value is a typed error BEFORE any infra is armed)."""

    kind: _EffectKind
    mode: str = ""  # enter_mode target mode
    agent: str = ""  # spawn_subagent_with_skill: optional declared child expert id
    plan_variant: str = ""  # plan_workflow/plan_small variant tag
    params: Mapping[str, Any] = field(default_factory=dict)  # loop/set_goal/schedule args
    playbook: Playbook | None = None  # operator playbook skeleton (plan-entering effects only)


@dataclass(frozen=True)
class SkillEffectOutcome:
    """The result of performing a declared skill effect."""

    kind: str
    detail: str
    #: True when the effect REPLACES the body (spawn): the caller must NOT inline the body.
    replaces_body: bool = False
    # enter_mode / plan variants
    mode: str = ""
    previous_mode: str = ""
    plan_variant: str = ""
    # spawn_subagent_with_skill
    task_id: str = ""
    child_session_id: str = ""
    # loop / set_goal / schedule (autonomy effects — the armed handle)
    loop_id: str = ""
    goal_id: str = ""
    schedule_id: str = ""
    next_fire_at: str = ""


# ---- parsing / validation --------------------------------------------------------------


def _parse_inline_object(text: str) -> dict[str, str]:
    """Parse a tiny inline ``{k: v, k2: v2}`` frontmatter object into a flat str->str map.

    The hand-rolled SKILL.md frontmatter parser (``skills._parse_skill_frontmatter``) stores
    an inline ``effect: {kind: "enter_mode", mode: "plan"}`` value as the literal string
    ``{kind: "enter_mode", mode: "plan"}``. This recovers the pairs without pulling in a YAML
    dependency — the shapes we accept are flat scalars only.
    """

    inner = text.strip()
    if inner.startswith("{"):
        inner = inner[1:]
    if inner.endswith("}"):
        inner = inner[:-1]
    out: dict[str, str] = {}
    for pair in inner.split(","):
        if ":" not in pair:
            continue
        key, _, value = pair.partition(":")
        key = key.strip().strip("\"'")
        value = value.strip().strip("\"'")
        if key:
            out[key] = value
    return out


def _coerce_effect_spec(meta: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the raw effect spec dict declared in ``meta``, or ``None`` when none is declared.

    Accepts three authored forms, all from the frontmatter (trusted) — never from the body:

    * a mapping ``effect: {kind: ..., mode: ...}`` (a real YAML parser),
    * the inline-object string the built-in parser produces for the same source,
    * the flat form ``effect: <kind>`` plus sibling ``effect_<field>:`` keys.
    """

    raw = meta.get("effect")
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.startswith("{"):
            return _parse_inline_object(text)
        spec: dict[str, Any] = {"kind": text}
        prefix = "effect_"
        for key, value in meta.items():
            if isinstance(key, str) and key.startswith(prefix) and len(key) > len(prefix):
                spec[key[len(prefix) :]] = value
        return spec
    raise SkillEffectError(
        f"skill effect declaration must be a mapping or string, got {type(raw).__name__}",
        reason="malformed_effect",
    )


def _coerce_int(spec: Mapping[str, Any], key: str) -> int | None:
    """Coerce ``spec[key]`` to an int (``None`` when absent/blank; typed error when malformed).

    Frontmatter is stringly-typed, so a declared bound arrives as ``"5"``; a non-numeric value
    is a :class:`SkillEffectError` (``malformed_effect``) at PARSE time — never a silent 0."""

    raw = spec.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise SkillEffectError(
            f"effect field {key!r} must be an integer, got {raw!r}", reason="malformed_effect"
        ) from None


def _coerce_float(spec: Mapping[str, Any], key: str) -> float | None:
    """Coerce ``spec[key]`` to a float (``None`` when absent/blank; typed error when malformed)."""

    raw = spec.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        raise SkillEffectError(
            f"effect field {key!r} must be a number, got {raw!r}", reason="malformed_effect"
        ) from None


def _coerce_bool(spec: Mapping[str, Any], key: str) -> bool | None:
    """Coerce ``spec[key]`` to a bool (``None`` when absent; typed error when unrecognised).

    Handles the stringly-typed frontmatter case (``"false"`` is FALSE — ``bool("false")`` is
    True, a classic footgun) as well as a real YAML bool."""

    raw = spec.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    raise SkillEffectError(
        f"effect field {key!r} must be a boolean, got {raw!r}", reason="malformed_effect"
    )


def _autonomy_params(
    spec: Mapping[str, Any],
    *,
    str_keys: tuple[str, ...] = (),
    int_keys: tuple[str, ...] = (),
    float_keys: tuple[str, ...] = (),
    bool_keys: tuple[str, ...] = (),
    passthrough: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a typed, validated params dict for an autonomy effect (absent keys omitted).

    Numerics are coerced (malformed → typed error); ``passthrough`` keys are carried verbatim.
    Unset bounds stay unset.

    Validation is TOTAL over the declared spec: any key outside the effect's known parameter
    set (plus ``kind``) is a typed :class:`SkillEffectError` (``unknown_effect_param``) — never
    a silent drop (no-silent-fallback ground rule). This is what stops a deleted-tier gate from
    arming a *different* effect: a skill declaring the removed deterministic goal-predicate keys
    (``effect_predicate_*`` → ``predicate_*`` in the spec) is REJECTED with a typed reason that
    reaches trace/API, rather than silently arming a semantically different NL-judge goal."""

    known = {"kind", *str_keys, *int_keys, *float_keys, *bool_keys, *passthrough}
    unknown = sorted(str(k) for k in spec if str(k) not in known)
    if unknown:
        raise SkillEffectError(
            f"skill effect declares unknown parameter(s) {unknown} "
            f"(known for this effect: {sorted(known - {'kind'})})",
            reason="unknown_effect_param",
        )
    out: dict[str, Any] = {}
    for k in str_keys:
        v = spec.get(k)
        if v is not None and str(v).strip():
            out[k] = str(v).strip()
    for k in int_keys:
        iv = _coerce_int(spec, k)
        if iv is not None:
            out[k] = iv
    for k in float_keys:
        fv = _coerce_float(spec, k)
        if fv is not None:
            out[k] = fv
    for k in bool_keys:
        bv = _coerce_bool(spec, k)
        if bv is not None:
            out[k] = bv
    for k in passthrough:
        if k in spec and spec[k] not in (None, ""):
            out[k] = spec[k]
    return out


def _parse_playbook_field(raw: Any, *, plan_entering: bool, meta: Any = None) -> "Playbook | None":
    """Parse a declared playbook (+ ``playbook_from_plan`` placement), typed — never a silent drop."""

    try:
        return planning.parse_effect_playbook(raw, plan_entering=plan_entering, meta=meta)
    except planning.PlaybookError as exc:
        raise SkillEffectError(str(exc), reason=exc.reason) from exc


def parse_skill_effect(meta: Mapping[str, Any]) -> SkillEffect | None:
    """Parse + validate the declared effect on a skill's frontmatter ``meta`` (typed).

    Returns ``None`` when the skill declares no effect (the common, backward-compatible
    case — an effect-less skill is unchanged). Raises :class:`SkillEffectError` for a
    malformed spec or an unknown ``kind`` (no silent ignore). A ``playbook:`` declared on
    an effect-LESS skill is MISPLACED (a playbook only rides a plan-entering effect): it is
    typed-rejected (``playbook_requires_plan_mode`` / ``malformed_playbook``), never dropped.
    """

    if not isinstance(meta, Mapping):
        return None
    spec = _coerce_effect_spec(meta)
    if spec is None:
        # An orphan playbook on an effect-less skill is misplaced -> typed reject (never dropped).
        _parse_playbook_field(meta.get("playbook"), plan_entering=False, meta=meta)
        return None
    kind = str(spec.get("kind") or "").strip()
    if not kind:
        raise SkillEffectError("skill effect declares no kind", reason="missing_effect_kind")
    if kind not in _KNOWN_EFFECT_KINDS:
        raise SkillEffectError(
            f"unknown skill effect kind {kind!r} (known: {sorted(_KNOWN_EFFECT_KINDS)})",
            reason="unknown_effect_kind",
        )
    plan_entering = kind in _PLAN_VARIANTS or (
        kind == EFFECT_ENTER_MODE and str(spec.get("mode") or "").strip() == "plan"
    )
    pb_raw = spec.get("playbook", meta.get("playbook"))
    playbook = _parse_playbook_field(pb_raw, plan_entering=plan_entering, meta=meta)
    if kind == EFFECT_ENTER_MODE:
        mode = str(spec.get("mode") or "").strip()
        if mode not in _MODE_RESTRICTIVENESS:
            raise SkillEffectError(
                f"enter_mode effect declares invalid mode {mode!r} "
                f"(valid: {sorted(_MODE_RESTRICTIVENESS)})",
                reason="invalid_mode",
            )
        return SkillEffect(kind=EFFECT_ENTER_MODE, mode=mode, playbook=playbook)
    if kind in _PLAN_VARIANTS:
        # A plan_workflow / plan_small VARIANT (P1.6): enter PLAN with a variant tag. The
        # transition reuses the tighten-only enter_mode path (plan is strictest → never
        # relaxes a restrictive mode). Kept minimal: enter_mode:plan + the variant tag.
        return SkillEffect(
            kind=kind, mode="plan", plan_variant=_PLAN_VARIANTS[kind], playbook=playbook
        )  # type: ignore[arg-type]
    if kind == EFFECT_LOOP:
        params = _autonomy_params(
            spec,
            str_keys=("prompt",),
            int_keys=("interval_s", "max_iters", "max_tokens"),
            float_keys=("max_wallclock_s", "max_usd"),
        )
        return SkillEffect(kind=EFFECT_LOOP, params=params)
    if kind == EFFECT_SET_GOAL:
        params = _autonomy_params(
            spec,
            str_keys=("condition",),
            int_keys=("max_goal_iters", "max_tokens"),
            float_keys=("max_wallclock_s",),
        )
        if not params.get("condition"):
            raise SkillEffectError(
                "set_goal effect declares no condition to gate completion on",
                reason="goal_missing_condition",
            )
        return SkillEffect(kind=EFFECT_SET_GOAL, params=params)
    if kind == EFFECT_SCHEDULE:
        params = _autonomy_params(
            spec,
            str_keys=("prompt", "cron", "run_at", "timezone"),
            int_keys=("delay_s",),
            bool_keys=("recurring",),
        )
        if not (params.get("cron") or params.get("run_at") or int(params.get("delay_s") or 0) > 0):
            raise SkillEffectError(
                "schedule effect needs a trigger: one of cron / run_at / delay_s",
                reason="schedule_missing_trigger",
            )
        return SkillEffect(kind=EFFECT_SCHEDULE, params=params)
    # spawn_subagent_with_skill: ``agent`` is optional (defaults to a self-directed
    # subagent running the caller's own expert seeded with the skill body).
    agent = str(spec.get("agent") or "").strip()
    return SkillEffect(kind=EFFECT_SPAWN_SUBAGENT, agent=agent)


# ---- execution -------------------------------------------------------------------------


def _execute_enter_mode(effect: SkillEffect, app: Any, session_id: str) -> SkillEffectOutcome:
    """Perform the ``enter_mode`` effect via the real session-update path, tighten-only."""

    sess = app.state.sessions.get(session_id)
    if sess is None:
        raise SkillEffectError(
            f"enter_mode effect has no session to transition ({session_id!r})",
            reason="no_active_session",
        )
    current = str(getattr(sess, "mode", "") or "edit")
    target = effect.mode
    current_rank = _MODE_RESTRICTIVENESS.get(current, 0)
    target_rank = _MODE_RESTRICTIVENESS[target]
    # The no-escape guard: a skill effect may only ENTER a more-(or equally-)restrictive
    # posture. A strictly-lower target would relax the mode (e.g. plan -> edit) — refused.
    if target_rank < current_rank:
        raise SkillModeTransitionError(current, target)
    updated = app.state.sessions.update(session_id, mode=target)
    if updated is None or str(getattr(updated, "mode", "")) != target:
        raise SkillEffectError(
            f"session mode update to {target!r} did not take effect",
            reason="mode_update_failed",
        )
    return SkillEffectOutcome(
        kind=EFFECT_ENTER_MODE,
        detail=f"entered {target} mode (was {current})",
        mode=target,
        previous_mode=current,
    )


def _execute_plan_variant(effect: SkillEffect, app: Any, session_id: str) -> SkillEffectOutcome:
    """Perform a ``plan_workflow`` / ``plan_small`` variant: enter PLAN + record the tag.

    Reuses the tighten-only :func:`_execute_enter_mode` (target ``plan`` is strictest, so the
    no-relax guard passes when the session is edit/architect/plan and REFUSES nothing weaker) —
    a variant can never escape a restrictive mode. The variant tag is persisted on the session
    (``metadata["plan_variant"]``, no fifth store) so the plan-mode attachment/budget machinery
    can read which variant was requested."""

    base = _execute_enter_mode(SkillEffect(kind=EFFECT_ENTER_MODE, mode="plan"), app, session_id)
    app.state.sessions.update(session_id, metadata_patch={"plan_variant": effect.plan_variant})
    return SkillEffectOutcome(
        kind=effect.kind,
        detail=f"entered plan mode ({effect.plan_variant} variant, was {base.previous_mode})",
        mode="plan",
        previous_mode=base.previous_mode,
        plan_variant=effect.plan_variant,
    )


def _default_skill_prompt(ref: "SkillRef") -> str:
    """The re-drive/scheduled prompt for a loop/schedule effect that declares none."""

    return f"Continue the procedure defined by skill {ref.id!r} to completion."


def _execute_loop(
    effect: SkillEffect, ref: "SkillRef", app: Any, session_id: str
) -> SkillEffectOutcome:
    """Perform the ``loop`` effect: arm a self-paced loop via :func:`start_loop` (#P4.1).

    The declared typed bounds pass straight through; :func:`start_loop` resolves any unset
    bound to a finite hard default and clamps the interval, so a skill CANNOT arm an
    unbounded/too-fast loop (the anti-runaway holds). A :class:`LoopError` (e.g. an empty
    prompt) is re-raised as a :class:`SkillEffectError` carrying its typed reason."""

    from clio_agent.gact.autonomous_loop import LoopError, start_loop  # noqa: PLC0415

    p = effect.params
    prompt = str(p.get("prompt") or "").strip() or _default_skill_prompt(ref)
    try:
        summary = start_loop(
            app,
            session_id,
            prompt=prompt,
            interval_s=int(p.get("interval_s", 0) or 0),
            max_iters=int(p.get("max_iters", 0) or 0),
            max_wallclock_s=float(p.get("max_wallclock_s", 0.0) or 0.0),
            max_tokens=int(p.get("max_tokens", 0) or 0),
            max_usd=float(p.get("max_usd", 0.0) or 0.0),
        )
    except LoopError as exc:
        raise SkillEffectError(
            f"loop effect for skill {ref.id!r} rejected: {exc}", reason=exc.reason
        ) from exc
    return SkillEffectOutcome(
        kind=EFFECT_LOOP,
        detail=(
            f"armed loop {summary['loop_id']} (interval {summary['interval_s']}s, "
            f"max_iters {summary['max_iters']}, next wake {summary['next_fire_at']})"
        ),
        loop_id=str(summary["loop_id"]),
        next_fire_at=str(summary["next_fire_at"]),
    )


def _execute_set_goal(
    effect: SkillEffect, ref: "SkillRef", app: Any, session_id: str
) -> SkillEffectOutcome:
    """Perform the ``set_goal`` effect: arm a goal via :func:`arm_goal` (#P4.2).

    The SANCTIONED skill-arming door (a declared, trusted effect like ``/goal`` — NOT the
    model self-arming; goal stays non-model-tool-armable per #1080). Completion is decided by
    the bounded LLM judge at the finalize boundary and the typed loop bounds are the hard stops
    (the deterministic goal-predicate tier was deleted, A4 #1057) — arming only sets the
    run-until condition. A :class:`GoalError` (empty condition) is re-raised as a typed
    :class:`SkillEffectError`."""

    from clio_agent.gact.goal import GoalError, arm_goal  # noqa: PLC0415

    p = effect.params
    try:
        summary = arm_goal(
            app,
            session_id,
            condition=str(p.get("condition") or ""),
            max_goal_iters=int(p.get("max_goal_iters", 0) or 0),
            max_wallclock_s=float(p.get("max_wallclock_s", 0.0) or 0.0),
            max_tokens=int(p.get("max_tokens", 0) or 0),
        )
    except GoalError as exc:
        raise SkillEffectError(
            f"set_goal effect for skill {ref.id!r} rejected: {exc}", reason=exc.reason
        ) from exc
    return SkillEffectOutcome(
        kind=EFFECT_SET_GOAL,
        detail=(
            f"armed goal {summary['goal_id']} gating completion on {summary['condition']!r} "
            f"(LLM judge, max_goal_iters {summary['max_goal_iters']})"
        ),
        goal_id=str(summary["goal_id"]),
    )


def _execute_schedule(
    effect: SkillEffect, ref: "SkillRef", app: Any, session_id: str
) -> SkillEffectOutcome:
    """Perform the ``schedule`` effect: register a cron / one-shot via ``ScheduleStore.create``.

    Arms the SAME scheduler the ``cron_create`` tool / ``/cron`` command use, so the declared
    schedule is subject to the SAME anti-runaway clamps: a sub-floor recurring cron raises the
    typed :class:`CronError` (min-interval floor), surfaced here as a :class:`SkillEffectError`
    the caller can read — a skill cannot over-schedule."""

    from clio_agent.gact.scheduler import CronError  # noqa: PLC0415

    p = effect.params
    prompt = str(p.get("prompt") or "").strip() or _default_skill_prompt(ref)
    try:
        sch = app.state.schedules.create(
            session_id=session_id,
            question=prompt,
            cron=str(p.get("cron", "") or ""),
            run_at=str(p.get("run_at", "") or ""),
            delay_s=int(p.get("delay_s", 0) or 0),
            recurring=bool(p.get("recurring", True)),
            timezone_name=str(p.get("timezone", "") or ""),
        )
    except CronError as exc:
        raise SkillEffectError(
            f"schedule effect for skill {ref.id!r} rejected: {exc}", reason=exc.reason
        ) from exc
    return SkillEffectOutcome(
        kind=EFFECT_SCHEDULE,
        detail=(
            f"registered schedule {sch.id} (cron {sch.cron!r} run_at {sch.run_at!r} "
            f"recurring {sch.recurring}, next fire {sch.next_fire_at})"
        ),
        schedule_id=str(sch.id),
        next_fire_at=str(sch.next_fire_at or ""),
    )


def _execute_spawn(
    effect: SkillEffect, ref: "SkillRef", app: Any, session_id: str, agent_id: str
) -> SkillEffectOutcome:
    """Perform the ``spawn_subagent_with_skill`` effect: spawn a child turn seeded with the
    skill body (the body is NOT inlined into the caller). Returns the task handle."""

    from clio_agent.gact.turn_spawn import (  # noqa: PLC0415
        SpawnError,
        TaskSpec,
        spawn_child_turn_threadsafe,
    )

    body = read_skill_body(ref)  # fresh read at invocation time
    seed = f"# Skill: {ref.id}\n\n{body}"
    # A declared ``agent`` routes to that child (keeping the declared-child guard); an absent
    # agent is a SELF-directed subagent (the caller's own expert in a fresh context) — that is
    # not a routing decision to a different capability, so the declared-child guard is skipped
    # for it (documented seam, not a silent bypass).
    child_expert = effect.agent or agent_id
    spec = TaskSpec(
        child_expert_id=child_expert,
        task_text=(
            f"Follow the procedure defined by the seeded skill {ref.id!r} to completion, "
            "then report your result."
        ),
        parent_session_id=session_id,
        requesting_expert_id=agent_id,
        seed_context=seed,
        skip_declared_check=not effect.agent,
        mode="async",
    )
    try:
        task = spawn_child_turn_threadsafe(app, spec)
    except SpawnError as exc:
        raise SkillEffectError(
            f"spawn_subagent_with_skill refused for skill {ref.id!r}: {exc}",
            reason=exc.reason,
        ) from exc
    return SkillEffectOutcome(
        kind=EFFECT_SPAWN_SUBAGENT,
        detail=f"spawned subagent {child_expert!r} seeded with skill {ref.id!r}",
        replaces_body=True,
        task_id=task.task_id,
        child_session_id=task.child_session_id,
    )


def _emit_skill_effect(
    app: Any, session_id: str, ref: "SkillRef", outcome: SkillEffectOutcome, agent_id: str
) -> None:
    """Emit a typed ``skill.effect`` provenance event on the semantic highway (best-effort).

    Every privileged runtime action is queryable after the fact (RULE 4 / no-silent-fallback);
    capture must never break an effect that already succeeded (the react-step pattern)."""

    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _active_semantic_trace_id,
        _active_semantic_turn_id,
        _emit_semantic_event,
    )

    payload: dict[str, Any] = {
        "skill_id": ref.id,
        "scope": ref.scope,
        "path": ref.path,
        "checksum": ref.checksum,
        "effect_kind": outcome.kind,
        "agent_id": agent_id,
    }
    if outcome.mode:
        payload["mode"] = outcome.mode
        payload["previous_mode"] = outcome.previous_mode
    if outcome.plan_variant:
        payload["plan_variant"] = outcome.plan_variant
    if outcome.task_id:
        payload["task_id"] = outcome.task_id
        payload["child_session_id"] = outcome.child_session_id
    if outcome.loop_id:
        payload["loop_id"] = outcome.loop_id
    if outcome.goal_id:
        payload["goal_id"] = outcome.goal_id
    if outcome.schedule_id:
        payload["schedule_id"] = outcome.schedule_id
    if outcome.next_fire_at:
        payload["next_fire_at"] = outcome.next_fire_at
    try:
        _emit_semantic_event(
            app,
            session_id,
            "skill.effect",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status="completed",
            summary=f"{agent_id} skill {ref.id} effect {outcome.kind}: {outcome.detail}",
            actor={"agent_id": agent_id, "role": "expert"},
            subject={"skill_id": ref.id, "scope": ref.scope},
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 - capture never breaks a performed effect
        trace.event("SKILLS", "skill.effect emit failed for %s: %s", ref.id, exc)


def maybe_apply_skill_effect(ref: "SkillRef", *, agent_id: str) -> str | None:
    """If ``ref`` declares a privileged effect, PERFORM it and return the tool observation.

    Returns ``None`` when the skill declares no effect — the caller then loads the body
    normally (backward-compatible). Otherwise the RUNTIME performs the declared effect and
    returns the tool-observation string:

    * ``enter_mode`` / ``plan_workflow`` / ``plan_small`` → a confirmation + the skill body
      (the entered mode's instructions);
    * ``loop`` / ``set_goal`` / ``schedule`` → a confirmation (the armed loop/goal/schedule
      handle) + the skill body (the procedure the autonomy is armed around);
    * ``spawn_subagent_with_skill`` → the spawned task handle (the body is NOT inlined).

    Raises :class:`SkillEffectError` for a malformed/unknown effect, a missing session
    context, or a rejected (mode-relaxing / refused-spawn / clamp-tripped) effect — never a
    silent ignore.
    """

    effect = parse_skill_effect(ref.meta)
    if effect is None:
        return None
    app = _ctx.active_app()
    session_id = _ctx.active_session_id()
    if app is None or not session_id:
        raise SkillEffectError(
            f"skill {ref.id!r} declares a {effect.kind} effect but no active session context",
            reason="no_active_session",
        )
    if effect.kind == EFFECT_ENTER_MODE:
        outcome = _execute_enter_mode(effect, app, session_id)
    elif effect.kind in _PLAN_VARIANTS:
        outcome = _execute_plan_variant(effect, app, session_id)
    elif effect.kind == EFFECT_LOOP:
        outcome = _execute_loop(effect, ref, app, session_id)
    elif effect.kind == EFFECT_SET_GOAL:
        outcome = _execute_set_goal(effect, ref, app, session_id)
    elif effect.kind == EFFECT_SCHEDULE:
        outcome = _execute_schedule(effect, ref, app, session_id)
    else:
        outcome = _execute_spawn(effect, ref, app, session_id, agent_id)
    # P1.6b/c #1068: a plan-entering effect records its operator playbook — inline (P1.6b) OR by
    # reference to a saved plan artifact (P1.6c playbook_from_plan) — as the ACTIVE playbook.
    if outcome.mode == "plan":
        plan_reuse.record_plan_playbook(app, session_id, ref, effect.playbook, default_name=ref.id)
    _emit_skill_effect(app, session_id, ref, outcome, agent_id)
    trace.event(
        "SKILLS", "agent %s skill %s effect %s (%s)", agent_id, ref.id, outcome.kind, outcome.detail
    )
    if outcome.replaces_body:
        return json.dumps(
            {
                "skill_effect": outcome.kind,
                "status": "spawned",
                "task_id": outcome.task_id,
                "child_session_id": outcome.child_session_id,
                "detail": outcome.detail,
            },
            sort_keys=True,
        )
    body = read_skill_body(ref)
    return f"[skill effect: {outcome.detail}]\n\n# Skill: {ref.id}\n{body}"
