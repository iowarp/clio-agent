"""One-shot AI reviewer for the ai-review approval mode (#1044, epic #1031 Pillar 1).

When a session runs ``approval_mode == "ai-review"`` and a non-read tool call reaches
the interactive prompt boundary (after :func:`is_read_only`, the plan/architect lock
and any explicit policy have all declined to decide it), the permission gate asks a
**separate AI reviewer** whether to allow, deny, or escalate the call to the human.

Design (see ``docs`` slice #1044): the reviewer is an **in-process one-shot DSPy
call**, run on the bridge thread the gate already blocks (``evt.wait``). It runs NO
tools and spawns NO child session/turn — so it has ZERO permission-gate re-entrancy
or pool-starvation hazard (a tool-investigating child-turn reviewer is a documented
future enhancement). A tool-calling reviewer would re-enter the very gate that is
blocked on it and deadlock; this shape cannot.

Fail-safe (no-silent-fallback ground rule): the reviewer may only ever *relax* toward
an auto-decision it is confident about. If no LM resolves, or the reviewer call raises
or exceeds its bounded timeout, the verdict is ``"escalate"`` with a TYPED reason — the
gate then falls through to the existing human ``evt.wait``. The reviewer NEVER produces
a silent auto-allow on failure. is_read_only, the plan/architect lock and an explicit
deny policy all sit ABOVE the reviewer in the gate, so a review can never gate a read,
override the lock, or override an explicit deny.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

import dspy

from clio_agent import conf

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Verdicts the reviewer may return. ``allow``/``deny`` are auto-decisions the gate
#: acts on directly; ``escalate`` (the fail-safe) sends the call to the human prompt.
Verdict = Literal["allow", "deny", "escalate"]

#: Typed reviewer-outcome reasons stamped on the ledger row so every reviewer decision
#: AND every fail-safe escalation is attributable (no silent fallback). The gate records
#: these; nothing here is ever a bare ``except: pass``.
REASON_AI_REVIEW_ALLOW = "ai_review_allow"
REASON_AI_REVIEW_DENY = "ai_review_deny"
REASON_AI_REVIEW_ESCALATE = "ai_review_escalate"  # the reviewer itself chose to escalate
REASON_AI_REVIEW_NO_LM = "ai_review_no_lm"  # fail-safe: no LM resolved -> human decides
REASON_AI_REVIEW_ERROR = "ai_review_error"  # fail-safe: reviewer call raised -> human decides
REASON_AI_REVIEW_TIMEOUT = "ai_review_timeout"  # fail-safe: reviewer call timed out -> human

#: Map a fail-safe/decision verdict to its typed ledger reason.
_ESCALATE_REASONS = {
    "escalate": REASON_AI_REVIEW_ESCALATE,
    "no_lm": REASON_AI_REVIEW_NO_LM,
    "error": REASON_AI_REVIEW_ERROR,
    "timeout": REASON_AI_REVIEW_TIMEOUT,
}

#: Cap on the rendered args summary handed to the reviewer (bounded prompt — mirrors the
#: instrument-arg cap idiom so a huge tool input can never blow up the review prompt).
_ARGS_SUMMARY_MAX_CHARS = 2000

#: Bounded wall-clock budget for the one-shot reviewer LM call. On expiry the verdict is
#: ``escalate`` (fail-safe). Configurable so a slow reviewer model does not stall the
#: bridge thread longer than intended; the gate's own human wait remains the backstop.
_DEFAULT_TIMEOUT_S = 45.0


class _AiReviewSignature(dspy.Signature):
    """Decide whether a single non-read tool call should be auto-allowed, denied, or
    escalated to a human reviewer.

    You are a cautious security reviewer standing in for a human at a tool-permission
    boundary. The call has ALREADY passed the read-only fast-path, the plan/architect
    read-only lock, and any explicit allow/deny policy -- so it is a genuine, un-policied
    side-effecting action awaiting approval. Allow only calls that are clearly safe and
    consistent with the session's apparent intent; deny calls that are clearly dangerous
    or destructive; when uncertain, ESCALATE to a human rather than guessing.
    """

    tool_name: str = dspy.InputField(desc="The tool being called.")
    args_summary: str = dspy.InputField(desc="A bounded, truncated summary of the call arguments.")
    subject: str = dspy.InputField(
        desc="What kind of call this is (e.g. external MCP tool, filesystem/shell action)."
    )
    session_mode: str = dspy.InputField(desc="The session's operating mode (e.g. code, plan).")
    decision: Verdict = dspy.OutputField(
        desc="allow (clearly safe), deny (clearly dangerous), or escalate (uncertain -> human)."
    )
    rationale: str = dspy.OutputField(desc="One sentence explaining the decision.")


def _bounded_args_summary(args: Mapping[str, Any]) -> str:
    """Render ``args`` to a bounded string for the review prompt (never unbounded)."""

    try:
        text = ", ".join(f"{k}={v!r}" for k, v in args.items())
    except Exception:  # noqa: BLE001 - a weird repr must not break the review; degrade to keys
        text = ", ".join(str(k) for k in args)
    if len(text) > _ARGS_SUMMARY_MAX_CHARS:
        return text[:_ARGS_SUMMARY_MAX_CHARS] + "...(truncated)"
    return text


def _resolve_reviewer_lm(app: "FastAPI") -> tuple[Any, Any]:
    """Resolve the reviewer LM and matching adapter from its effective caller.

    A bound expert/main identity wins. Outside a DSPy context the app's accepted
    main identity is explicit fallback (the current repository has no independent
    per-session provider-profile map). ``permissions.ai_review_model`` changes only
    the model on that caller identity. Missing identity escalates fail-safe.
    """

    from clio_agent.gact.runtime.ambient_lm import active_lm  # noqa: PLC0415

    caller, ambient = active_lm()
    owner = getattr(app.state, "agent", None)
    adapter = getattr(dspy.settings, "adapter", None)
    if ambient:
        caller = getattr(owner, "_main_lm", None)
        adapter = getattr(owner, "_dspy_adapter", None)
    if caller is None:
        return None, None
    model = conf.resolve(
        "permissions.ai_review_model",
        env="CLIO_AI_REVIEW_MODEL",
        default="",
        cast=conf.as_str,
    ).strip()
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


def _run_reviewer(
    lm: Any, inputs: dict[str, str], timeout_s: float, adapter: Any = None
) -> tuple[str, str]:
    """Run the one-shot reviewer under a bounded timeout on a worker thread.

    Returns ``(verdict, escalation_key)``: for a clean ``allow``/``deny`` the key is
    empty; for any fail path it is ``"escalate"``/``"error"``/``"timeout"`` so the
    caller records the right typed reason. Never raises — every failure maps to a
    fail-safe escalation.
    """

    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    from concurrent.futures import TimeoutError as FuturesTimeout

    # Capture the caller-thread's effective DSPy adapter so the worker thread (fresh
    # thread-locals) inherits it — otherwise the reviewer Predict may use the wrong
    # adapter and always fail (→ escalate), defeating the auto-decision.
    parent_adapter = adapter or getattr(dspy.settings, "adapter", None)

    def _call() -> str:
        predict = dspy.Predict(_AiReviewSignature)
        ctx: dict[str, Any] = {"lm": lm}
        if parent_adapter is not None:
            ctx["adapter"] = parent_adapter
        with dspy.context(**ctx):
            result = predict(**inputs)
        return str(getattr(result, "decision", "") or "").strip().lower()

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-review")
    try:
        future = executor.submit(_call)
        try:
            decision = future.result(timeout=timeout_s)
        except FuturesTimeout:
            logger.warning("ai-review reviewer timed out after %.1fs -> escalate", timeout_s)
            return "escalate", "timeout"
        except Exception:  # noqa: BLE001 - reviewer error must fail safe, never auto-allow
            logger.warning("ai-review reviewer call failed -> escalate", exc_info=True)
            return "escalate", "error"
    finally:
        # Do not block the bridge thread waiting on a hung worker; let it drain in the
        # background (daemon-like) while the gate proceeds to the human wait.
        executor.shutdown(wait=False)
    if decision in {"allow", "deny"}:
        return decision, ""
    # Any other/unknown decision (including an explicit "escalate") -> human.
    return "escalate", "escalate"


def ai_review_verdict(
    app: "FastAPI",
    sid: str,
    name: str,
    args: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    *,
    subject: str = "tool",
) -> tuple[Verdict, str]:
    """Ask the one-shot AI reviewer to decide a single non-read tool call.

    Returns ``(verdict, reason)`` where ``verdict`` is ``"allow" | "deny" | "escalate"``
    and ``reason`` is the typed ledger reason for that outcome. Fail-safe: a missing LM,
    a reviewer error, or a timeout all return ``("escalate", <typed reason>)`` — never a
    silent auto-allow. The reviewer runs NO tools and spawns nothing, so it cannot
    re-enter the permission gate that is blocked on it.
    """

    _ = (app, sid, context)  # reserved for future context enrichment; not consulted today
    # Reviewer SETUP (LM resolution, session lookup, config) must also fail SAFE: any
    # exception here escalates to the human, never propagates (which would fail-CLOSED to a
    # deny at the gate) and never auto-allows.
    try:
        lm, adapter = _resolve_reviewer_lm(app)
        if lm is None:
            logger.warning("ai-review has no LM configured -> escalate to human (fail-safe)")
            return "escalate", REASON_AI_REVIEW_NO_LM
        session_mode = ""
        session = app.state.sessions.get(sid) if sid and hasattr(app.state, "sessions") else None
        if session is not None:
            session_mode = str(getattr(session, "mode", "") or "")
        inputs = {
            "tool_name": name,
            "args_summary": _bounded_args_summary(args),
            "subject": subject,
            "session_mode": session_mode,
        }
        timeout_s = conf.resolve(
            "permissions.ai_review_timeout_s",
            env="CLIO_AI_REVIEW_TIMEOUT_S",
            default=_DEFAULT_TIMEOUT_S,
            cast=float,
        )
    except Exception:  # noqa: BLE001 - reviewer setup failure fails safe to human escalation
        logger.warning("ai-review setup failed -> escalate to human (fail-safe)", exc_info=True)
        return "escalate", REASON_AI_REVIEW_ERROR
    verdict, escalate_key = _run_reviewer(lm, inputs, timeout_s, adapter=adapter)
    if verdict == "allow":
        return "allow", REASON_AI_REVIEW_ALLOW
    if verdict == "deny":
        return "deny", REASON_AI_REVIEW_DENY
    return "escalate", _ESCALATE_REASONS.get(escalate_key, REASON_AI_REVIEW_ESCALATE)


__all__ = [
    "REASON_AI_REVIEW_ALLOW",
    "REASON_AI_REVIEW_DENY",
    "REASON_AI_REVIEW_ERROR",
    "REASON_AI_REVIEW_ESCALATE",
    "REASON_AI_REVIEW_NO_LM",
    "REASON_AI_REVIEW_TIMEOUT",
    "Verdict",
    "ai_review_verdict",
]
