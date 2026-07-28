"""GOAL conditions — run-until-a-predicate, two-tier evaluation (P4.2, #1080).

A GOAL gates completion: when a turn would settle, the goal is evaluated at the finalize
boundary; if unsatisfied the session RE-DRIVES one more turn (the same #1031 loop-inbox
seam the bounded Stop-loop rides), and the goal AUTO-CLEARS when satisfied. The clio
analog of the Claude Code ``/goal`` session Stop-hook condition, hardened per the survey:

* **Two-tier evaluation** (never LLM-only for a consequential halt). :func:`run_llm_judge`
  is a bounded cheap-model FIRST PASS — a separate judge LM (via ``dspy.context``) that only
  *reads* the transcript and PROPOSES "is ``<condition>`` satisfied?"; it never acts.
  :func:`run_deterministic_gate` is the **authoritative HARD GATE** — a
  :class:`~clio_agent.gact.workflows.StatePredicate` over ``workflow_state`` (or a
  file-exists check), the reality-surfacing clio principle #2. A present predicate WINS: a
  false LLM "met" is OVERRIDDEN (typed ``goal_llm_overridden``) and the turn re-drives. A
  NL-only goal (no predicate) falls back to the LLM tier — the flagged *weaker* mode.

* **First-class typed bounds** back the loop against runaway (``max_goal_iters`` +
  ``max_wallclock_s`` / ``max_tokens``): a tripped bound settles DONE with a typed reason
  (:data:`GOAL_OUTCOME_REASONS`) — never an infinite loop, never a silent stop.

* **Injection-safe — the model can NEVER arm or self-satisfy a goal.** There is NO
  ``set_goal`` / ``goal_clear`` tool (a self-armed halt is the self-grading anti-pattern ⚑
  RULE 1 bans). A goal is armed ONLY by the USER (``/goal``) or a declared skill-effect
  (#1082), both via :func:`arm_goal`; the model gets a READ-ONLY
  :func:`build_goal_status_tool` and may PROPOSE readiness, but the HALT is clio infra.

Goal state lives on ``session.metadata["goal"]`` (no fifth store, RULE 4). A leaf module:
it never imports :mod:`clio_agent.gact.app` / :mod:`~clio_agent.gact.autonomous_loop` (the
loop composes with the goal via :func:`loop_goal_satisfied`, one-directional).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from clio_agent import conf
from clio_agent.gact import context as _ctx
from clio_agent.gact.workflows import StatePredicate, _field_lookup
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

#: ``session.metadata`` key holding the goal record (no fifth store).
GOAL_METADATA_KEY = "goal"

#: Typed goal-outcome reasons (the ``stream_fallback`` catalog convention): every goal
#: settlement / re-drive records exactly one so audit + trace branch on a code, never on
#: prose.
GOAL_OUTCOME_REASONS = (
    "goal_met",  # the two-tier gate confirmed satisfaction -> settle + auto-clear
    "goal_max_iters",  # the max_goal_iters backstop tripped -> settle done
    "goal_budget",  # the wall-clock / token budget was exhausted -> settle done
    "goal_abandoned",  # cleared without satisfaction (user /goal clear or session end)
    "goal_redrive",  # unmet -> re-drive one more turn (the continue reason, not a stop)
    "goal_llm_overridden",  # the LLM proposed met but the deterministic gate OVERRODE it
)

#: Hard iteration ceiling when ``max_goal_iters`` is unset (0) — a goal always terminates.
DEFAULT_MAX_GOAL_ITERS = 25


class GoalError(ValueError):
    """A goal arm / command was rejected with a machine-readable ``reason``.

    Callers branch on ``reason`` (``goal_missing_condition`` / ``goal_bad_predicate``)
    rather than string-matching the message — never a silent coercion."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# Goal record on session.metadata["goal"] (no fifth store).
def _get_goal(app: Any, sid: str) -> dict[str, Any]:
    """Read the goal record off ``session.metadata`` (``{}`` when absent)."""

    sess = app.state.sessions.get(sid)
    if sess is None:
        return {}
    meta = getattr(sess, "metadata", None) or {}
    goal = meta.get(GOAL_METADATA_KEY) if isinstance(meta, Mapping) else None
    return dict(goal) if isinstance(goal, Mapping) else {}


def _put_goal(app: Any, sid: str, goal: dict[str, Any]) -> None:
    """Persist the goal record as a whole under ``metadata["goal"]``.

    ``SessionStore.update`` does a SHALLOW merge, so writing the whole ``goal`` dict
    replaces the prior one wholesale (no stale sub-keys)."""

    app.state.sessions.update(sid, metadata_patch={GOAL_METADATA_KEY: goal})


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _budget_spent(app: Any, sid: str, goal: Mapping[str, Any]) -> tuple[float, int]:
    """Return ``(elapsed_wallclock_s, tokens_spent)`` since the goal was armed.

    ``tokens_spent`` is the DELTA against the rollup snapshotted at arm time
    (``tokens_at_start``) — the bound measures goal-driven spend, not the lifetime total."""

    created = _parse_iso(goal.get("created_at"))
    elapsed = (datetime.now(timezone.utc) - created).total_seconds() if created else 0.0
    sess = app.state.sessions.get(sid)
    tokens_spent = 0
    if sess is not None:
        used = int(getattr(sess, "tokens_input", 0)) + int(getattr(sess, "tokens_output", 0))
        tokens_spent = max(0, used - int(goal.get("tokens_at_start", 0) or 0))
    return elapsed, tokens_spent


# Deterministic hard gate (authoritative) — reuse the StatePredicate vocab.
def _validate_predicate(predicate: Any) -> dict[str, Any]:
    """Validate + normalize a deterministic predicate spec (typed error, no silent skip).

    Kinds (⚑ #2 — reality checks): ``state`` — a StatePredicate over ``workflow_state``
    (``{kind:"state", field_path:"a.b", check:"exists", exists:true}`` or
    ``{... check:"equals", equals:<v>}``); ``file_exists`` — ``{kind:"file_exists", path:...}``.
    """

    if not isinstance(predicate, Mapping):
        raise GoalError(
            "goal predicate must be a mapping (e.g. {kind: 'state', field_path: ...})",
            reason="goal_bad_predicate",
        )
    kind = str(predicate.get("kind") or "state").strip()
    if kind == "state":
        field_path = str(predicate.get("field_path") or "").strip()
        if not field_path:
            raise GoalError(
                "state predicate needs a non-empty field_path", reason="goal_bad_predicate"
            )
        check = str(
            predicate.get("check") or ("equals" if "equals" in predicate else "exists")
        ).strip()
        if check == "exists":
            return {
                "kind": "state",
                "field_path": field_path,
                "check": "exists",
                "exists": bool(predicate.get("exists", True)),
            }
        if check == "equals":
            if "equals" not in predicate:
                raise GoalError(
                    "equals predicate needs an 'equals' value", reason="goal_bad_predicate"
                )
            return {
                "kind": "state",
                "field_path": field_path,
                "check": "equals",
                "equals": predicate["equals"],
            }
        raise GoalError(
            f"state predicate 'check' must be 'exists' or 'equals', got {check!r}",
            reason="goal_bad_predicate",
        )
    if kind == "file_exists":
        path = str(predicate.get("path") or "").strip()
        if not path:
            raise GoalError("file_exists predicate needs a 'path'", reason="goal_bad_predicate")
        return {"kind": "file_exists", "path": path}
    raise GoalError(
        f"unknown goal predicate kind {kind!r} (known: 'state', 'file_exists')",
        reason="goal_bad_predicate",
    )


def _session_workflow_state(app: Any, sid: str) -> dict[str, Any]:
    """The session's current typed ``workflow_state`` (for the deterministic gate).

    ``session.metadata["workflow_state"]`` (the accumulated typed landing), else the most-
    recent typed landing on the session's own event bus — the same store, no fifth history."""

    sess = app.state.sessions.get(sid)
    meta = getattr(sess, "metadata", None) or {}
    ws = meta.get("workflow_state") if isinstance(meta, Mapping) else None
    if isinstance(ws, Mapping) and ws:
        return dict(ws)
    bus = getattr(app.state, "bus", None)
    if bus is None or not hasattr(bus, "session_events_since"):
        return {}
    from clio_agent.gact.agents.observe_runtime import _find_workflow_state  # noqa: PLC0415

    latest: dict[str, Any] = {}
    try:
        for event in bus.session_events_since(sid, cursor=1):
            if getattr(event, "type", "") != "semantic.event":
                continue
            body = getattr(event, "payload", None) or {}
            if not isinstance(body, Mapping):
                continue
            found = _find_workflow_state(body.get("payload"))
            if found:
                latest = found  # keep the highest-id (most recent) non-empty landing
    except Exception:  # noqa: BLE001 - a bus read must never break the goal gate
        logger.warning("goal workflow_state bus read failed", exc_info=True)
    return latest


def _state_predicate_satisfied(pred: StatePredicate, state: Mapping[str, Any]) -> bool:
    """Whether one :class:`StatePredicate` holds over ``state`` (reuse ``_field_lookup``)."""

    found, value = _field_lookup(state, pred.field_path)
    if pred.kind == "exists":
        return found == bool(pred.exists)
    return bool(found) and value == pred.equals


def run_deterministic_gate(app: Any, sid: str, goal: Mapping[str, Any]) -> Optional[bool]:
    """Evaluate the goal's deterministic predicate. ``None`` when the goal is NL-only.

    The AUTHORITATIVE tier: a present predicate overrides the LLM judge (``True``/``False``);
    ``None`` when no predicate is declared (the weaker NL-only mode, LLM tier decides)."""

    predicate = goal.get("predicate")
    if not isinstance(predicate, Mapping) or not predicate:
        return None
    kind = str(predicate.get("kind") or "state")
    if kind == "state":
        state = _session_workflow_state(app, sid)
        if str(predicate.get("check")) == "exists":
            sp = StatePredicate(
                field_path=str(predicate.get("field_path")),
                kind="exists",
                exists=bool(predicate.get("exists", True)),
            )
        else:
            sp = StatePredicate(
                field_path=str(predicate.get("field_path")),
                kind="equals",
                equals=predicate.get("equals"),
            )
        return _state_predicate_satisfied(sp, state)
    if kind == "file_exists":
        return Path(str(predicate.get("path"))).expanduser().exists()
    raise GoalError(f"unknown goal predicate kind {kind!r}", reason="goal_bad_predicate")


# LLM first-pass judge (bounded, cheap, read-only — it does NOT act).
#: The read-only judge instructions (functional-API signature, no nested class — the
#: ``module_variants`` reward-signature pattern that keeps the ratchet class-in-function
#: guard green).
_JUDGE_INSTRUCTIONS = (
    "Judge whether a stated goal condition is satisfied by the transcript so far. You are a "
    "READ-ONLY evaluator: you CANNOT act, call tools, or run anything — judge only what the "
    "transcript already shows. Answer conservatively: say the goal is met ONLY when the "
    "transcript demonstrates the condition is genuinely satisfied; if unsure, say it is not "
    "met and explain what is still missing."
)


def _judge_signature() -> Any:
    """Build the read-only goal-judge ``dspy.Signature`` via the functional API (lazy)."""

    import dspy  # noqa: PLC0415

    fields = {
        "goal_condition": (str, dspy.InputField(desc="The condition that must be satisfied.")),
        "transcript": (str, dspy.InputField(desc="The session transcript so far.")),
        "met": (bool, dspy.OutputField(desc="True ONLY if the transcript shows it satisfied.")),
        "reason": (
            str,
            dspy.OutputField(desc="One sentence: why (not) satisfied, what is needed."),
        ),
    }
    return dspy.Signature(fields, _JUDGE_INSTRUCTIONS)


def _judge_lm() -> Any:
    """Resolve the cheap judge LM (config-first), or ``None`` to use the ambient default.

    ``goal.judge_model`` / ``CLIO_GOAL_JUDGE_MODEL`` names a small/cheap model for the
    first-pass judge (the ``/goal`` Haiku pattern). When unset the judge runs under the
    session's ambient ``dspy.settings.lm`` — still a SEPARATE, non-acting judge call, never
    the acting model in the loop."""

    model = conf.resolve(
        "goal.judge_model", env="CLIO_GOAL_JUDGE_MODEL", default="", cast=conf.as_str
    )
    if not model:
        return None
    import dspy  # noqa: PLC0415

    return dspy.LM(model)


def _build_transcript(app: Any, sid: str, *, max_messages: int = 40) -> str:
    """Render the recent session transcript for the judge (text parts only, role-tagged)."""

    store = getattr(app.state, "messages", None)
    if store is None:
        return ""
    try:
        messages = store.get(sid, [])
    except Exception:  # noqa: BLE001 - a ledger read must never break the goal gate
        return ""
    lines: list[str] = []
    for message in list(messages)[-max_messages:]:
        role = str(getattr(message, "role", "") or "")
        for part in getattr(message, "parts", []) or []:
            if str(getattr(part, "type", "")) == "text":
                text = str(getattr(part, "text", "") or "").strip()
                if text:
                    lines.append(f"{role}: {text}")
    return "\n".join(lines)


def run_llm_judge(app: Any, sid: str, goal: Mapping[str, Any]) -> "GoalJudgement":
    """Run the bounded cheap-model FIRST PASS: propose whether the condition is satisfied.

    A separate judge model (``dspy.context``) that ONLY reads the transcript — it never acts.
    Its verdict is a PROPOSAL the deterministic gate overrides for a predicate-backed goal. A
    judge failure degrades to a not-met proposal with a typed reason (the gate stays authoritative)."""

    condition = str(goal.get("condition") or "")
    transcript = _build_transcript(app, sid)
    try:
        import dspy  # noqa: PLC0415

        judge = dspy.Predict(_judge_signature())
        lm = _judge_lm()
        if lm is not None:
            with dspy.context(lm=lm):
                pred = judge(goal_condition=condition, transcript=transcript)
        else:
            pred = judge(goal_condition=condition, transcript=transcript)
        return GoalJudgement(
            met=bool(getattr(pred, "met", False)),
            reason=str(getattr(pred, "reason", "") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - the judge is advisory; degrade to not-met
        logger.warning("goal LLM judge failed: %s", exc)
        return GoalJudgement(met=False, reason=f"judge unavailable ({exc})")


# Two-tier combination + bounded decision (PURE — no I/O, unit-testable).
@dataclass(frozen=True)
class GoalJudgement:
    """The LLM first-pass proposal (met + a reason the agent can act on)."""

    met: bool
    reason: str = ""


@dataclass(frozen=True)
class GoalDecision:
    """The bounded two-tier decision for one finalize-boundary goal evaluation.

    ``outcome`` is action-bearing: ``"met"`` settles + auto-clears, ``"capped"`` settles DONE
    on a tripped bound, ``"redrive"`` re-drives one more turn carrying ``guidance``. ``reason``
    is a :data:`GOAL_OUTCOME_REASONS` code; ``tier`` records who decided (``deterministic``
    authoritative, ``llm`` the weaker NL-only mode); ``llm_overridden`` marks a false LLM met
    the gate overrode; ``new_state`` is the goal snapshot to persist."""

    outcome: Literal["met", "redrive", "capped"]
    reason: str
    tier: Literal["deterministic", "llm", "none"] = "none"
    guidance: str = ""
    llm_overridden: bool = False
    met: bool = False
    new_state: dict[str, Any] = field(default_factory=dict)


def combine_tiers(
    *, det_result: Optional[bool], llm: GoalJudgement
) -> tuple[bool, Literal["deterministic", "llm", "none"], str, bool]:
    """Combine the deterministic (authoritative) + LLM tiers — PURE.

    Returns ``(met, tier, guidance, llm_overridden)``. When a predicate is present
    (``det_result is not None``) the deterministic verdict WINS and a disagreeing LLM
    "met" is flagged as an override. When NL-only (``det_result is None``) the LLM tier
    decides (the documented weaker mode, ``tier == "llm"``)."""

    if det_result is not None:
        overridden = bool(llm.met and not det_result)
        return det_result, "deterministic", llm.reason, overridden
    return llm.met, "llm", llm.reason, False


def evaluate_goal(
    goal: Mapping[str, Any],
    *,
    det_result: Optional[bool],
    llm: GoalJudgement,
    elapsed_s: float = 0.0,
    tokens_spent: int = 0,
) -> GoalDecision:
    """Decide met / re-drive / capped for one goal evaluation — PURE (no I/O).

    Deterministic tier authoritative (:func:`combine_tiers`). met -> settle + auto-clear
    (``goal_met``). Unmet -> typed bounds backstop the infinite loop: an exhausted
    ``max_goal_iters`` / ``max_wallclock_s`` / ``max_tokens`` settles DONE with a typed reason,
    else RE-DRIVE one more turn (bumping ``iters_elapsed``) carrying the judge guidance; a
    false-LLM-met the gate overrode re-drives under the distinct ``goal_llm_overridden`` code."""

    met, tier, guidance, overridden = combine_tiers(det_result=det_result, llm=llm)
    state = dict(goal)
    if met:
        state.update(
            {
                "active": False,
                "cleared": True,
                "clear_reason": "goal_met",
                "met": True,
                "satisfied_tier": tier,
            }
        )
        return GoalDecision(
            outcome="met",
            reason="goal_met",
            tier=tier,
            guidance=guidance,
            llm_overridden=overridden,
            met=True,
            new_state=state,
        )

    iters = int(goal.get("iters_elapsed", 0) or 0)
    max_iters = int(goal.get("max_goal_iters") or DEFAULT_MAX_GOAL_ITERS)
    max_wall = float(goal.get("max_wallclock_s") or 0.0)
    max_tokens = int(goal.get("max_tokens") or 0)

    capped_reason = ""
    if iters >= max_iters:
        capped_reason = "goal_max_iters"
    elif max_wall > 0 and elapsed_s >= max_wall:
        capped_reason = "goal_budget"
    elif max_tokens > 0 and tokens_spent >= max_tokens:
        capped_reason = "goal_budget"
    if capped_reason:
        state.update(
            {"active": False, "cleared": True, "clear_reason": capped_reason, "met": False}
        )
        return GoalDecision(
            outcome="capped",
            reason=capped_reason,
            tier=tier,
            guidance=guidance,
            llm_overridden=overridden,
            met=False,
            new_state=state,
        )

    state["iters_elapsed"] = iters + 1
    state["active"] = True  # a re-drive keeps the goal gating completion (still active)
    state["cleared"] = False
    return GoalDecision(
        outcome="redrive",
        reason="goal_llm_overridden" if overridden else "goal_redrive",
        tier=tier,
        guidance=guidance,
        llm_overridden=overridden,
        met=False,
        new_state=state,
    )


# Arm / clear — the ONLY doors, both non-model (user /goal or skill-effect).
def arm_goal(
    app: Any,
    sid: str,
    *,
    condition: str,
    predicate: Any = None,
    max_goal_iters: int = 0,
    max_wallclock_s: float = 0.0,
    max_tokens: int = 0,
) -> dict[str, Any]:
    """Arm a goal on ``session.metadata`` (the sanctioned, non-model arming entry).

    The ONE seam both arming doors route through — the ``/goal`` command and the declared
    ``set_goal`` skill-effect (#1082); there is deliberately NO model arming tool (⚑ RULE 1).
    An unset bound resolves to a finite default (never runs away); a validated ``predicate``
    makes the goal predicate-backed (deterministic authoritative), its absence the NL-only
    mode. Raises :class:`GoalError` on an empty condition or a malformed predicate."""

    text = (condition or "").strip()
    if not text:
        raise GoalError(
            "a goal needs a condition to gate completion on", reason="goal_missing_condition"
        )
    normalized_predicate = _validate_predicate(predicate) if predicate else None
    sess = app.state.sessions.get(sid)
    tokens_at_start = 0
    if sess is not None:
        tokens_at_start = int(getattr(sess, "tokens_input", 0)) + int(
            getattr(sess, "tokens_output", 0)
        )
    goal_id = "goal_" + uuid.uuid4().hex[:12]
    goal: dict[str, Any] = {
        "goal_id": goal_id,
        "active": True,
        "cleared": False,
        "condition": text,
        "predicate": normalized_predicate,
        "predicate_backed": normalized_predicate is not None,
        "max_goal_iters": int(max_goal_iters)
        if int(max_goal_iters or 0) > 0
        else DEFAULT_MAX_GOAL_ITERS,
        "max_wallclock_s": float(max_wallclock_s) if float(max_wallclock_s or 0.0) > 0 else 0.0,
        "max_tokens": int(max_tokens or 0),
        "iters_elapsed": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tokens_at_start": tokens_at_start,
        "clear_reason": "",
        "met": False,
    }
    _put_goal(app, sid, goal)
    logger.info(
        "goal armed goal_id=%s predicate_backed=%s max_goal_iters=%s",
        goal_id,
        normalized_predicate is not None,
        goal["max_goal_iters"],
    )
    trace.event(
        "GOAL", "goal %s armed (predicate_backed=%s)", goal_id, normalized_predicate is not None
    )
    return {
        "goal_id": goal_id,
        "condition": text,
        "predicate_backed": normalized_predicate is not None,
        "max_goal_iters": goal["max_goal_iters"],
        "max_wallclock_s": goal["max_wallclock_s"],
        "max_tokens": goal["max_tokens"],
    }


def clear_goal(app: Any, sid: str, *, reason: str = "goal_abandoned") -> bool:
    """Clear the active goal with a typed ``reason`` (idempotent; ``False`` when none).

    Used by ``/goal clear`` (user) and :func:`stop_session_goal` (session end). A cleared
    goal no longer gates completion — the ``reason`` is recorded (no silent stop)."""

    goal = _get_goal(app, sid)
    if not goal or not goal.get("active") or goal.get("cleared"):
        return False
    goal.update({"active": False, "cleared": True, "clear_reason": reason, "met": False})
    if app.state.sessions.get(sid) is not None:
        _put_goal(app, sid, goal)
    logger.info("goal cleared goal_id=%s reason=%s", goal.get("goal_id"), reason)
    trace.event("GOAL", "goal %s cleared reason=%s", goal.get("goal_id"), reason)
    return True


def stop_session_goal(app: Any, sid: str, *, reason: str = "goal_abandoned") -> None:
    """Cancel-both entry for session end / delete — abandon any active goal (idempotent)."""

    clear_goal(app, sid, reason=reason)


# Loop compose seam (#1079): the loop stops when a predicate-backed goal holds.
def loop_goal_satisfied(app: Any, sid: str) -> bool:
    """Whether an active predicate-backed goal's DETERMINISTIC gate holds (the loop seam).

    Wired into :func:`clio_agent.gact.autonomous_loop._loop_goal_met` so a satisfied goal ends
    the loop with the typed ``loop_goal_met`` reason. Deterministic-ONLY (cheap, no LLM) and
    authoritative; an NL-only goal returns ``False`` here (the goal's own finalize eval governs it)."""

    goal = _get_goal(app, sid)
    if not goal or not goal.get("active") or goal.get("cleared"):
        return False
    try:
        return run_deterministic_gate(app, sid, goal) is True
    except GoalError:
        return False


# Finalize-boundary dispatch — the goal RIDES the Stop-loop re-drive seam.
def _enqueue_goal_redrive(
    app: Any, sid: str, decision: GoalDecision, goal: Mapping[str, Any]
) -> None:
    """Enqueue ONE goal re-drive onto the session's loop-inbox (the #1031 seam).

    The SAME bounded seam the Stop-loop rides (``stop_loop._enqueue_redrive``): the idle hook
    drains it into exactly one new turn. The text is the judge guidance + the goal condition."""

    from clio_agent.gact.loop_inbox import InboxEvent, inbox_for  # noqa: PLC0415

    condition = str(goal.get("condition") or "")
    guidance = (decision.guidance or "").strip()
    if guidance:
        text = f"The goal is not yet satisfied: {guidance}\n\nContinue working toward: {condition}"
    else:
        text = f"The goal is not yet satisfied. Continue working toward: {condition}"
    inbox_for(app, sid).put(
        InboxEvent(
            kind="user_message",
            task_id="",
            text=text,
            metadata={
                "goal_redrive": True,
                "goal_id": str(goal.get("goal_id") or ""),
                "goal_iters": int(decision.new_state.get("iters_elapsed", 0) or 0),
                "goal_reason": decision.reason,
            },
        )
    )


def dispatch_goal_at_finalize(
    app: Any, *, session_id: str, turn_id: str = "", trace_id: str = ""
) -> "GoalDecision | None":
    """Evaluate the session's goal at the turn-finalize boundary (never raises).

    A no-op when no active goal is set. Otherwise runs the two-tier eval (LLM first-pass
    PROPOSAL + authoritative deterministic gate -> :func:`evaluate_goal`), persists the
    snapshot, and: **met / capped** -> auto-clear + settle DONE with a typed reason;
    **redrive** -> enqueue ONE re-drive on the #1031 loop-inbox seam (the same bounded
    mechanism the Stop-loop rides), carrying the judge reason as guidance. A dispatch error
    is swallowed (post-turn contract). Returns the decision, or ``None`` when no goal/failed."""

    try:
        goal = _get_goal(app, session_id)
        if not goal or not goal.get("active") or goal.get("cleared"):
            return None
        llm = run_llm_judge(app, session_id, goal)
        det = run_deterministic_gate(app, session_id, goal)
        elapsed_s, tokens_spent = _budget_spent(app, session_id, goal)
        decision = evaluate_goal(
            goal, det_result=det, llm=llm, elapsed_s=elapsed_s, tokens_spent=tokens_spent
        )
        if app.state.sessions.get(session_id) is not None:
            _put_goal(app, session_id, decision.new_state)
        if decision.outcome == "redrive":
            _enqueue_goal_redrive(app, session_id, decision, goal)
        _emit_goal_event(app, session_id, decision, turn_id=turn_id, trace_id=trace_id)
        logger.info(
            "goal eval goal_id=%s outcome=%s reason=%s tier=%s overridden=%s",
            goal.get("goal_id"),
            decision.outcome,
            decision.reason,
            decision.tier,
            decision.llm_overridden,
        )
        trace.event(
            "GOAL", "goal %s %s reason=%s", goal.get("goal_id"), decision.outcome, decision.reason
        )
        return decision
    except Exception:  # noqa: BLE001 - the finalize hook must never crash a turn
        logger.warning("goal finalize hook error", exc_info=True)
        return None


def _emit_goal_event(
    app: Any, sid: str, decision: GoalDecision, *, turn_id: str, trace_id: str
) -> None:
    """Emit a typed ``goal.<outcome>`` semantic event (best-effort; never fatal)."""

    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    event_type = f"goal.{decision.outcome}"
    status = "running" if decision.outcome == "redrive" else "completed"
    summary = f"Goal {decision.outcome} (reason={decision.reason}, tier={decision.tier})."
    try:
        _emit_semantic_event(
            app,
            sid,
            event_type,
            turn_id=turn_id,
            trace_id=trace_id,
            status=status,
            summary=summary,
            actor={"component": "goal"},
            payload={
                "reason": decision.reason,
                "tier": decision.tier,
                "llm_overridden": decision.llm_overridden,
                "iters_elapsed": int(decision.new_state.get("iters_elapsed", 0) or 0),
            },
        )
    except Exception:  # noqa: BLE001 - capture never breaks a settled turn
        logger.debug("goal event emit failed", exc_info=True)


# /goal command parsing (owner-module logic; the catalog route stays thin).
_CLEAR_TOKENS = frozenset({"clear", "stop", "reset", "cancel", "off"})


def parse_goal_command(
    request_body: Mapping[str, Any],
) -> tuple[str, dict[str, Any], Any, bool]:
    """Parse ``/goal <condition>`` (+ ``args`` bounds/predicate) or ``/goal clear``.

    Returns ``(condition, bounds, predicate, clear)``: a leading clear/stop/reset word (or
    ``args.clear``) clears the goal; typed bounds + an optional ``predicate`` come from ``args``."""

    text = str(
        request_body.get("input") or request_body.get("text") or request_body.get("prompt") or ""
    ).strip()
    raw_args = request_body.get("args")
    args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}

    if text.lower() in _CLEAR_TOKENS or bool(args.get("clear")):
        return "", {}, None, True

    condition = text or str(args.get("condition") or "").strip()
    bounds: dict[str, Any] = {}
    for key in ("max_goal_iters", "max_wallclock_s", "max_tokens"):
        if key in args:
            bounds[key] = args[key]
    predicate = args.get("predicate")
    if predicate is None and isinstance(args.get("when_state"), Mapping):
        predicate = {"kind": "state", **dict(args["when_state"])}
    return condition, bounds, predicate, False


def run_goal_command(app: Any, sid: str, request_body: Mapping[str, Any]) -> str:
    """Execute the ``/goal`` user command: arm (or clear) a goal, return the message body.

    Parse + arm + message live here so the catalog dispatch route stays a thin one-liner."""

    condition, bounds, predicate, clear = parse_goal_command(request_body)
    if clear:
        cleared = clear_goal(app, sid, reason="goal_abandoned")
        return "goal cleared" if cleared else "no active goal to clear"
    if not condition:
        return (
            "usage: /goal <condition> — a condition is required to gate completion "
            "(e.g. /goal all tests pass). Add args.predicate for a deterministic gate, "
            "or /goal clear to remove the active goal."
        )
    try:
        armed = arm_goal(app, sid, condition=condition, predicate=predicate, **bounds)
    except GoalError as exc:
        return f"/goal rejected: {exc} (reason={exc.reason})"
    gate = (
        "deterministic gate (authoritative)"
        if armed["predicate_backed"]
        else "LLM-only (weaker mode — no deterministic predicate)"
    )
    return (
        f"goal {armed['goal_id']} set — gating completion on: {armed['condition']}. "
        f"Evaluation: {gate}. Bounds max_goal_iters={armed['max_goal_iters']}, "
        f"max_wallclock_s={int(armed['max_wallclock_s'])}, max_tokens={armed['max_tokens']}. "
        "The goal re-drives while unmet and auto-clears when satisfied (or a bound trips). "
        "Only you can set or clear it (/goal clear); the agent cannot."
    )


# goal_status — the model's READ-ONLY surface (NO set_goal / goal_clear tool).
def build_goal_status_tool() -> Any:
    """Build the ``goal_status`` read-only dspy.Tool (auto-attached; mirrors ``cron_list``).

    The ONLY goal surface the acting model gets: it may READ the goal to PROPOSE readiness,
    but can NEVER set/clear/self-satisfy it (there is deliberately no ``set_goal`` /
    ``goal_clear`` tool — the self-grading anti-pattern). ``met`` reflects the DETERMINISTIC
    gate only (a cheap read-only readback; never runs the LLM judge, never settles)."""

    import dspy  # noqa: PLC0415

    def goal_status() -> dict:
        """Read THIS session's active goal condition + progress (READ-ONLY).

        Returns ``{active, condition, predicate_backed, iters_elapsed, max_goal_iters,
        budget_spent, met}``. ``met`` reflects the deterministic gate only (a readback — it does
        NOT settle the goal or end the turn). You CANNOT set or clear a goal (only the user /goal
        or a declared skill-effect can) — use this to see what you are working toward."""

        app = _ctx.active_app()
        sid = _ctx.active_session_id()
        if app is None or not sid:
            return {"active": False, "condition": "", "iters_elapsed": 0, "met": False}
        goal = _get_goal(app, sid)
        if not goal or not goal.get("active"):
            return {"active": False, "condition": "", "iters_elapsed": 0, "met": False}
        elapsed_s, tokens_spent = _budget_spent(app, sid, goal)
        met = False
        try:
            met = run_deterministic_gate(app, sid, goal) is True
        except GoalError:
            met = False
        return {
            "active": True,
            "condition": str(goal.get("condition") or ""),
            "predicate_backed": bool(goal.get("predicate_backed")),
            "iters_elapsed": int(goal.get("iters_elapsed", 0) or 0),
            "max_goal_iters": int(goal.get("max_goal_iters", 0) or 0),
            "budget_spent": {"wallclock_s": elapsed_s, "tokens": tokens_spent},
            "met": met,
        }

    return dspy.Tool(func=goal_status, name="goal_status", desc=goal_status.__doc__, args={})
