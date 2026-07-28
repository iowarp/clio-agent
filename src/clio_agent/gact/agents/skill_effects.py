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

Two effects ship in P1.0:

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

Validation is typed and total: an unknown ``kind`` (or a malformed spec) raises
:class:`SkillEffectError` — never a silent ignore (no-silent-fallback ground rule).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.gact import context as _ctx
from clio_agent.gact.skills import read_skill_body
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.skills import SkillRef

#: The declared effect kinds P1.0 ships. An ``effect.kind`` outside this set is a typed
#: validation error, never a silent skip.
EFFECT_ENTER_MODE: Literal["enter_mode"] = "enter_mode"
EFFECT_SPAWN_SUBAGENT: Literal["spawn_subagent_with_skill"] = "spawn_subagent_with_skill"
_KNOWN_EFFECT_KINDS = frozenset({EFFECT_ENTER_MODE, EFFECT_SPAWN_SUBAGENT})

#: Session modes and their restrictiveness RANK (higher = MORE restrictive). ``enter_mode``
#: may only move to an equal-or-more-restrictive posture (target rank >= current rank); a
#: strictly-lower target would RELAX the mode and is rejected (the no-escape guard). ``plan``
#: is strictest (read-only + sole plan-file write, turn-ending); ``architect`` proposes diffs
#: (no direct writes); ``edit`` has full write authority (least restrictive). These are the
#: three valid session modes after P1.1 deleted ``chat`` (see ``sessions.Session.mode``).
_MODE_RESTRICTIVENESS: dict[str, int] = {"edit": 0, "architect": 1, "plan": 2}


class SkillEffectError(RuntimeError):
    """A declared skill effect could not be validated or performed (typed reason).

    Carries a machine-readable ``reason`` (e.g. ``unknown_effect_kind``, ``invalid_mode``,
    ``no_active_session``, ``mode_update_failed``, ``spawn_refused``) so callers/audit can
    branch without string-matching the message.
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


@dataclass(frozen=True)
class SkillEffect:
    """A validated, declared skill effect (parsed from trusted frontmatter metadata)."""

    kind: Literal["enter_mode", "spawn_subagent_with_skill"]
    mode: str = ""  # enter_mode target mode
    agent: str = ""  # spawn_subagent_with_skill: optional declared child expert id


@dataclass(frozen=True)
class SkillEffectOutcome:
    """The result of performing a declared skill effect."""

    kind: str
    detail: str
    #: True when the effect REPLACES the body (spawn): the caller must NOT inline the body.
    replaces_body: bool = False
    # enter_mode
    mode: str = ""
    previous_mode: str = ""
    # spawn_subagent_with_skill
    task_id: str = ""
    child_session_id: str = ""


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


def parse_skill_effect(meta: Mapping[str, Any]) -> SkillEffect | None:
    """Parse + validate the declared effect on a skill's frontmatter ``meta`` (typed).

    Returns ``None`` when the skill declares no effect (the common, backward-compatible
    case — an effect-less skill is unchanged). Raises :class:`SkillEffectError` for a
    malformed spec or an unknown ``kind`` (no silent ignore).
    """

    if not isinstance(meta, Mapping):
        return None
    spec = _coerce_effect_spec(meta)
    if spec is None:
        return None
    kind = str(spec.get("kind") or "").strip()
    if not kind:
        raise SkillEffectError("skill effect declares no kind", reason="missing_effect_kind")
    if kind not in _KNOWN_EFFECT_KINDS:
        raise SkillEffectError(
            f"unknown skill effect kind {kind!r} (known: {sorted(_KNOWN_EFFECT_KINDS)})",
            reason="unknown_effect_kind",
        )
    if kind == EFFECT_ENTER_MODE:
        mode = str(spec.get("mode") or "").strip()
        if mode not in _MODE_RESTRICTIVENESS:
            raise SkillEffectError(
                f"enter_mode effect declares invalid mode {mode!r} "
                f"(valid: {sorted(_MODE_RESTRICTIVENESS)})",
                reason="invalid_mode",
            )
        return SkillEffect(kind=EFFECT_ENTER_MODE, mode=mode)
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
    if outcome.kind == EFFECT_ENTER_MODE:
        payload["mode"] = outcome.mode
        payload["previous_mode"] = outcome.previous_mode
    else:
        payload["task_id"] = outcome.task_id
        payload["child_session_id"] = outcome.child_session_id
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

    * ``enter_mode`` → a confirmation line + the skill body (the entered mode's instructions);
    * ``spawn_subagent_with_skill`` → the spawned task handle (the body is NOT inlined).

    Raises :class:`SkillEffectError` for a malformed/unknown effect, a missing session
    context, or a rejected (mode-relaxing / refused-spawn) effect — never a silent ignore.
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
    else:
        outcome = _execute_spawn(effect, ref, app, session_id, agent_id)
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
