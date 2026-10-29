"""Typed outcomes a flow's ``ctx`` raises instead of guessing.

A ``ctx.call`` returns its task's result or raises one of these, carrying the
handle and the typed reason. The flow decides what to do with it; the runtime
never resubmits an uncertain call, and one gather member failing never cancels
the others.
"""

from __future__ import annotations

from typing import Any


class FlowOutcome(Exception):
    """Base of every typed outcome a flow can observe."""

    reason: str = "flow_outcome"

    def __init__(self, message: str, *, handle: str = "", **details: Any) -> None:
        super().__init__(message)
        self.handle = handle
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "message": str(self), "handle": self.handle, **self.details}


class CallFailed(FlowOutcome):
    """The called task settled as failed."""

    reason = "call_failed"


class CallCancelled(FlowOutcome):
    """The called task was cancelled (by a human, a parent or a deadline)."""

    reason = "call_cancelled"


class CallInterrupted(FlowOutcome):
    """The called task stopped without a result (interrupted, never started)."""

    reason = "call_interrupted"


class CallAwaitingInput(CallFailed):
    """The called agent ended its turn waiting for input that nothing will send.

    ``result`` carries its last answer; the child stays open, so a person can still
    message it. A flow that catches ``CallFailed`` treats it as a failed call.
    """

    reason = "call_awaiting_input"

    @property
    def answer(self) -> str:
        """The child's last answer, as ``CallResult.answer`` reads a completed one."""

        result = self.details.get("result")
        if isinstance(result, dict):
            return str(result.get("answer") or "")
        return "" if result is None else str(result)


class CallStopped(CallFailed):
    """The called agent's turn stopped (Stop, or an error such as a provider timeout).

    ``result`` carries what its turn recorded last, its error when it failed; the child
    stays open, so a person can still resume it. A flow that catches ``CallFailed``
    treats it as a failed call.
    """

    reason = "call_stopped"


class TaskStateUncertain(FlowOutcome):
    """The task's state cannot be confirmed; it is never resubmitted."""

    reason = "task_state_uncertain"


class CallRefused(FlowOutcome):
    """The task layer refused to accept the call (admission, evidence, strategy)."""

    reason = "call_refused"


class FlowTargetUnknown(FlowOutcome):
    """``ctx.call`` named nothing this agent can reach."""

    reason = "flow_target_unknown"


class FlowNotTrusted(FlowOutcome):
    """``ctx.call`` named a flow whose plugin is installed but not trusted. A running
    flow's agent is fixed, so the person is asked when a flow or workflow is started."""

    reason = "flow_not_trusted"


class FlowLimitExceeded(FlowOutcome):
    """A flow limit (depth, model calls, budget) refused the next call."""

    reason = "flow_limit_exceeded"


class FlowCheckpointLimit(FlowOutcome):
    """The run's checkpoint reached its entry cap; the run cannot record more work."""

    reason = "flow_checkpoint_limit"


class FlowReplayMismatch(FlowOutcome):
    """A replayed run met different code or input than it recorded."""

    reason = "flow_replay_mismatch"


class FlowStall(FlowOutcome):
    """Work cannot proceed: a typed stall with the step, predicate and observed state."""

    reason = "flow_stall"

    def __init__(
        self,
        stall: str,
        message: str,
        *,
        step: str = "",
        predicate: Any = None,
        observed: Any = None,
        handle: str = "",
    ) -> None:
        super().__init__(
            message, handle=handle, stall=stall, step=step, predicate=predicate, observed=observed
        )
        self.stall = stall


class GatherFailed(FlowOutcome):
    """At least one gathered call did not succeed; every member's outcome is attached.

    ``results`` lists each member in call order: a ``CallResult`` or the typed
    outcome it raised. No member was cancelled because another failed.
    """

    reason = "gather_failed"

    def __init__(self, message: str, results: list[Any]) -> None:
        super().__init__(message, failed=[i for i, r in enumerate(results) if _failed(r)])
        self.results = results


def _failed(value: Any) -> bool:
    return isinstance(value, BaseException)


#: Task status -> the outcome a call raises for it.
STATUS_OUTCOMES: dict[str, type[FlowOutcome]] = {
    "failed": CallFailed,
    "cancelled": CallCancelled,
    "interrupted": CallInterrupted,
    "never_started": CallInterrupted,
    "task_state_uncertain": TaskStateUncertain,
    "awaiting_input": CallAwaitingInput,
    "stopped": CallStopped,
}


def run_ending(error: Exception) -> tuple[str, dict[str, Any]]:
    """How a run that raised ``error`` ended: its record's status and result.

    A typed outcome ends it by its reason (a cancellation is not a fault in it); any
    other error is ``flow_error`` with its type and message, so the record never
    stays "running" after its task failed.
    """

    if isinstance(error, FlowOutcome):
        return ("cancelled" if isinstance(error, CallCancelled) else error.reason), error.to_dict()
    return "flow_error", {"error": type(error).__name__, "message": str(error)[:2000]}


__all__ = [
    "STATUS_OUTCOMES",
    "CallAwaitingInput",
    "CallCancelled",
    "CallFailed",
    "CallInterrupted",
    "CallRefused",
    "CallStopped",
    "FlowCheckpointLimit",
    "FlowLimitExceeded",
    "FlowOutcome",
    "FlowReplayMismatch",
    "FlowStall",
    "FlowTargetUnknown",
    "GatherFailed",
    "TaskStateUncertain",
    "run_ending",
]
