"""GOAL conditions — run-until-a-condition, LLM-judge-only evaluation (P4.2, #1080; A4 #1057).

A GOAL gates completion: when a turn would settle, the goal is evaluated at the finalize
boundary; if unsatisfied the session RE-DRIVES one more turn (the same #1031 loop-inbox
seam the bounded Stop-loop rides), and the goal AUTO-CLEARS when satisfied. The clio
analog of the Claude Code ``/goal`` session Stop-hook condition:

* **LLM-judge-only evaluation.** :func:`run_llm_judge` is a bounded cheap-model pass — a
  separate judge LM (via ``dspy.context``) that only *reads* the transcript and decides
  "is ``<condition>`` satisfied?"; it never acts. The cross-industry survey (Claude Code,
  Codex, Gemini CLI, Devin, OpenHands, Hermes, LangGraph, AutoGen, smolagents, CrewAI)
  settled the design: **nobody ships a deterministic predicate over model-authored state**
  as the halt authority — a predicate over ``workflow_state`` the acting model itself writes
  lets it *mark its own homework* (the self-grading anti-pattern ⚑ RULE 1 bans). The prior
  two-tier deterministic gate was therefore DELETED (A4). **The loop bounds are the hard
  stops.**

* **First-class typed bounds are the hard stops** against runaway (``max_goal_iters`` +
  ``max_wallclock_s`` / ``max_tokens``): a tripped bound settles DONE with a typed reason
  (:data:`GOAL_OUTCOME_REASONS`) — never an infinite loop, never a silent stop.

* **Injection-safe arming — the model can NEVER arm a goal.** There is NO ``set_goal`` /
  ``goal_clear`` tool (a self-armed halt is the self-grading anti-pattern). A goal is armed
  ONLY by the USER (``/goal``) or a declared skill-effect (#1082), both via :func:`arm_goal`;
  the model gets a READ-ONLY :func:`build_goal_status_tool` reporting armed state only (never
  a completion readback it could steer toward, and it never runs the judge).

* **Accepted residual (honest).** The judge reads the transcript, so it is *persuadable* by
  transcript content — a model that narrates "the condition is satisfied" can nudge a met
  verdict. This is bounded, not eliminated: arming stays USER-only (the model cannot arm a
  goal to grade itself against), the judge is a SEPARATE non-acting call, and the typed
  bounds still cap the loop no matter what the judge decides.

Goal state lives on ``session.metadata["goal"]`` (no fifth store, RULE 4). A leaf module:
it never imports :mod:`clio_agent.gact.app` / :mod:`~clio_agent.gact.autonomous_loop`; the
loop–goal compose (a judge-met goal stops the loop) lives in the ``turn_finalize`` glue,
one-directional.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from clio_agent import conf
from clio_agent.gact import context as _ctx
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

#: ``session.metadata`` key holding the goal record (no fifth store).
GOAL_METADATA_KEY = "goal"

#: Typed goal-outcome reasons (the ``stream_fallback`` catalog convention): every goal
#: settlement / re-drive records exactly one so audit + trace branch on a code, never on
#: prose.
GOAL_OUTCOME_REASONS = (
    "goal_met",  # the bounded LLM judge confirmed satisfaction -> settle + auto-clear
    "goal_max_iters",  # the max_goal_iters backstop tripped -> settle done
    "goal_budget",  # the wall-clock / token budget was exhausted -> settle done
    "goal_abandoned",  # cleared without satisfaction (user /goal clear or session end)
    "goal_redrive",  # unmet -> re-drive one more turn (the continue reason, not a stop)
)

#: Hard iteration ceiling when ``max_goal_iters`` is unset (0) — a goal always terminates.
DEFAULT_MAX_GOAL_ITERS = 25


class GoalError(ValueError):
    """A goal arm / command was rejected with a machine-readable ``reason``.

    Callers branch on ``reason`` (e.g. ``goal_missing_condition``) rather than
    string-matching the message — never a silent coercion."""

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


# LLM judge (bounded, cheap, read-only — it does NOT act; the sole completion decider).
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


def _judge_route(app: Any) -> tuple[Any, Any]:
    """Resolve the judge from the effective caller or explicit model override.

    ``goal.judge_model`` / ``CLIO_GOAL_JUDGE_MODEL`` names a small/cheap model for the
    first-pass judge. When unset, a bound expert/main LM wins. Calls outside a
    DSPy context use the app's accepted main identity explicitly; they never read
    the process boot default."""

    model = conf.resolve(
        "goal.judge_model", env="CLIO_GOAL_JUDGE_MODEL", default="", cast=conf.as_str
    )
    import dspy  # noqa: PLC0415

    from clio_agent.gact.runtime.ambient_lm import active_lm  # noqa: PLC0415

    caller, ambient = active_lm()
    owner = getattr(app.state, "agent", None)
    adapter = getattr(dspy.settings, "adapter", None)
    if ambient:
        caller = getattr(owner, "_main_lm", None)
        adapter = getattr(owner, "_dspy_adapter", None)
    if caller is None:
        raise RuntimeError("goal judge has no owning session LM")
    if not model:
        return caller, adapter
    from clio_agent.lm.secondary import (  # noqa: PLC0415
        SecondarySettings,
        resolve_secondary_lm,
    )

    route = resolve_secondary_lm(
        "summarizer",
        caller_lm=caller,
        caller_adapter=adapter,
        settings=SecondarySettings(model=model),
    )
    return route.lm, route.adapter


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
    """Run the bounded cheap-model judge: decide whether the condition is satisfied.

    A separate judge model (``dspy.context``) that ONLY reads the transcript — it never acts.
    Its verdict is the sole completion decision (the deterministic tier was deleted, A4); the
    typed loop bounds remain the hard stops. A judge failure degrades to a not-met verdict with
    a typed reason (so an unavailable judge re-drives until a bound trips, never falsely halts)."""

    condition = str(goal.get("condition") or "")
    transcript = _build_transcript(app, sid)
    try:
        import dspy  # noqa: PLC0415

        judge = dspy.Predict(_judge_signature())
        lm, adapter = _judge_route(app)
        with dspy.context(lm=lm, adapter=adapter):
            pred = judge(goal_condition=condition, transcript=transcript)
        return GoalJudgement(
            met=bool(getattr(pred, "met", False)),
            reason=str(getattr(pred, "reason", "") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - the judge is advisory; degrade to not-met
        logger.warning("goal LLM judge failed: %s", exc)
        return GoalJudgement(met=False, reason=f"judge unavailable ({exc})")


# Bounded decision (PURE — no I/O, unit-testable).
@dataclass(frozen=True)
class GoalJudgement:
    """The LLM judge verdict (met + a reason the agent can act on)."""

    met: bool
    reason: str = ""


@dataclass(frozen=True)
class GoalDecision:
    """The bounded decision for one finalize-boundary goal evaluation.

    ``outcome`` is action-bearing: ``"met"`` settles + auto-clears, ``"capped"`` settles DONE
    on a tripped bound, ``"redrive"`` re-drives one more turn carrying ``guidance``. ``reason``
    is a :data:`GOAL_OUTCOME_REASONS` code; ``new_state`` is the goal snapshot to persist."""

    outcome: Literal["met", "redrive", "capped"]
    reason: str
    guidance: str = ""
    met: bool = False
    new_state: dict[str, Any] = field(default_factory=dict)


def evaluate_goal(
    goal: Mapping[str, Any],
    *,
    llm: GoalJudgement,
    elapsed_s: float = 0.0,
    tokens_spent: int = 0,
) -> GoalDecision:
    """Decide met / re-drive / capped for one goal evaluation — PURE (no I/O).

    The bounded LLM judge decides ``met`` (the deterministic tier was deleted, A4). met ->
    settle + auto-clear (``goal_met``). Unmet -> the typed bounds are the hard stops: an
    exhausted ``max_goal_iters`` / ``max_wallclock_s`` / ``max_tokens`` settles DONE with a
    typed reason, else RE-DRIVE one more turn (bumping ``iters_elapsed``) carrying the judge
    guidance."""

    met = llm.met
    guidance = llm.reason
    state = dict(goal)
    if met:
        state.update(
            {
                "active": False,
                "cleared": True,
                "clear_reason": "goal_met",
                "met": True,
            }
        )
        return GoalDecision(
            outcome="met",
            reason="goal_met",
            guidance=guidance,
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
            guidance=guidance,
            met=False,
            new_state=state,
        )

    state["iters_elapsed"] = iters + 1
    state["active"] = True  # a re-drive keeps the goal gating completion (still active)
    state["cleared"] = False
    return GoalDecision(
        outcome="redrive",
        reason="goal_redrive",
        guidance=guidance,
        met=False,
        new_state=state,
    )


# Arm / clear — the ONLY doors, both non-model (user /goal or skill-effect).
def arm_goal(
    app: Any,
    sid: str,
    *,
    condition: str,
    max_goal_iters: int = 0,
    max_wallclock_s: float = 0.0,
    max_tokens: int = 0,
) -> dict[str, Any]:
    """Arm a goal on ``session.metadata`` (the sanctioned, non-model arming entry).

    The ONE seam both arming doors route through — the ``/goal`` command and the declared
    ``set_goal`` skill-effect (#1082); there is deliberately NO model arming tool (⚑ RULE 1).
    Completion is decided by the bounded LLM judge (the deterministic predicate tier was
    deleted, A4); an unset bound resolves to a finite default (the loop bounds are the hard
    stops, never runs away). Raises :class:`GoalError` on an empty condition."""

    text = (condition or "").strip()
    if not text:
        raise GoalError(
            "a goal needs a condition to gate completion on", reason="goal_missing_condition"
        )
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
    logger.info("goal armed goal_id=%s max_goal_iters=%s", goal_id, goal["max_goal_iters"])
    trace.event("GOAL", "goal %s armed (max_goal_iters=%s)", goal_id, goal["max_goal_iters"])
    return {
        "goal_id": goal_id,
        "condition": text,
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

    A no-op when no active goal is set. Otherwise runs the bounded LLM judge
    (-> :func:`evaluate_goal`), persists the snapshot, and: **met / capped** -> auto-clear +
    settle DONE with a typed reason; **redrive** -> enqueue ONE re-drive on the #1031
    loop-inbox seam (the same bounded mechanism the Stop-loop rides), carrying the judge reason
    as guidance. A dispatch error is swallowed (post-turn contract). Returns the decision, or
    ``None`` when no goal/failed. A judge-met decision also stops any armed loop with the typed
    ``loop_goal_met`` reason — that compose lives in the ``turn_finalize`` glue (goal is a leaf)."""

    try:
        goal = _get_goal(app, session_id)
        if not goal or not goal.get("active") or goal.get("cleared"):
            return None
        llm = run_llm_judge(app, session_id, goal)
        elapsed_s, tokens_spent = _budget_spent(app, session_id, goal)
        decision = evaluate_goal(goal, llm=llm, elapsed_s=elapsed_s, tokens_spent=tokens_spent)
        if app.state.sessions.get(session_id) is not None:
            _put_goal(app, session_id, decision.new_state)
        if decision.outcome == "redrive":
            _enqueue_goal_redrive(app, session_id, decision, goal)
        _emit_goal_event(app, session_id, decision, turn_id=turn_id, trace_id=trace_id)
        logger.info(
            "goal eval goal_id=%s outcome=%s reason=%s",
            goal.get("goal_id"),
            decision.outcome,
            decision.reason,
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
    summary = f"Goal {decision.outcome} (reason={decision.reason})."
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
                "iters_elapsed": int(decision.new_state.get("iters_elapsed", 0) or 0),
            },
        )
    except Exception:  # noqa: BLE001 - capture never breaks a settled turn
        logger.debug("goal event emit failed", exc_info=True)


# /goal command parsing (owner-module logic; the catalog route stays thin).
_CLEAR_TOKENS = frozenset({"clear", "stop", "reset", "cancel", "off"})


def parse_goal_command(
    request_body: Mapping[str, Any],
) -> tuple[str, dict[str, Any], bool]:
    """Parse ``/goal <condition>`` (+ ``args`` bounds) or ``/goal clear``.

    Returns ``(condition, bounds, clear)``: a leading clear/stop/reset word (or ``args.clear``)
    clears the goal; typed bounds come from ``args``. Completion is the bounded LLM judge only
    (the deterministic predicate tier was deleted, A4), so no predicate/``when_state`` is parsed."""

    text = str(
        request_body.get("input") or request_body.get("text") or request_body.get("prompt") or ""
    ).strip()
    raw_args = request_body.get("args")
    args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}

    if text.lower() in _CLEAR_TOKENS or bool(args.get("clear")):
        return "", {}, True

    condition = text or str(args.get("condition") or "").strip()
    bounds: dict[str, Any] = {}
    for key in ("max_goal_iters", "max_wallclock_s", "max_tokens"):
        if key in args:
            bounds[key] = args[key]
    return condition, bounds, False


def run_goal_command(app: Any, sid: str, request_body: Mapping[str, Any]) -> str:
    """Execute the ``/goal`` user command: arm (or clear) a goal, return the message body.

    Parse + arm + message live here so the catalog dispatch route stays a thin one-liner."""

    condition, bounds, clear = parse_goal_command(request_body)
    if clear:
        cleared = clear_goal(app, sid, reason="goal_abandoned")
        return "goal cleared" if cleared else "no active goal to clear"
    if not condition:
        return (
            "usage: /goal <condition> — a condition is required to gate completion "
            "(e.g. /goal all tests pass). Bounds via args (max_goal_iters/max_wallclock_s/"
            "max_tokens); /goal clear to remove the active goal."
        )
    try:
        armed = arm_goal(app, sid, condition=condition, **bounds)
    except GoalError as exc:
        return f"/goal rejected: {exc} (reason={exc.reason})"
    return (
        f"goal {armed['goal_id']} set — gating completion on: {armed['condition']}. "
        "Evaluation: LLM judge (bounded); bounds are the hard stops. "
        f"Bounds max_goal_iters={armed['max_goal_iters']}, "
        f"max_wallclock_s={int(armed['max_wallclock_s'])}, max_tokens={armed['max_tokens']}. "
        "The goal re-drives while unmet and auto-clears when satisfied (or a bound trips). "
        "Only you can set or clear it (/goal clear); the agent cannot."
    )


def _goal_status_result(result: dict[str, Any]) -> dict[str, Any]:
    """Declare ``goal_status``'s typed wire payload (P5 wire semantics — the
    ``wait_agent_tasks`` treatment) then return ``result`` unchanged (the
    model-facing shape is untouched). ``message`` is derived from the SAME
    armed-state facts, never a readback the model could steer toward."""

    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
    )

    if result.get("active"):
        message = (
            f"goal active: {result.get('condition', '')!r} "
            f"({result.get('iters_elapsed', 0)}/{result.get('max_goal_iters', 0)} iters)"
        )
    else:
        message = "no active goal"
    declare_structured_content({"message": message, **result})
    return result


# goal_status — the model's READ-ONLY surface (NO set_goal / goal_clear tool).
def build_goal_status_tool() -> Any:
    """Build the ``goal_status`` read-only dspy.Tool (auto-attached; mirrors ``cron_list``).

    The ONLY goal surface the acting model gets: it may READ the armed goal to see what it is
    working toward, but can NEVER set/clear it (there is deliberately no ``set_goal`` /
    ``goal_clear`` tool — the self-grading anti-pattern). It returns ARMED STATE ONLY: it never
    runs the judge and never exposes a ``met`` completion readback the model could steer toward
    (completion is decided at the finalize boundary by the bounded judge, A4)."""

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def goal_status() -> dict:
        """Read THIS session's active goal condition + progress (READ-ONLY).

        Returns ``{active, condition, iters_elapsed, max_goal_iters, budget_spent}``. This is
        armed-state only — it does NOT run any evaluation, settle the goal, or end the turn. You
        CANNOT set or clear a goal (only the user /goal or a declared skill-effect can) — use
        this to see what you are working toward."""

        app = _ctx.active_app()
        sid = _ctx.active_session_id()
        if app is None or not sid:
            return _goal_status_result({"active": False, "condition": "", "iters_elapsed": 0})
        goal = _get_goal(app, sid)
        if not goal or not goal.get("active"):
            return _goal_status_result({"active": False, "condition": "", "iters_elapsed": 0})
        elapsed_s, tokens_spent = _budget_spent(app, sid, goal)
        return _goal_status_result(
            {
                "active": True,
                "condition": str(goal.get("condition") or ""),
                "iters_elapsed": int(goal.get("iters_elapsed", 0) or 0),
                "max_goal_iters": int(goal.get("max_goal_iters", 0) or 0),
                "budget_spent": {"wallclock_s": elapsed_s, "tokens": tokens_spent},
            }
        )

    return native_tool(
        goal_status,
        name="goal_status",
        desc=goal_status.__doc__,
        title="Goal Status",
        args={},
    )
