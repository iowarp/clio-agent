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
}

__all__ = [
    "STATUS_OUTCOMES",
    "CallCancelled",
    "CallFailed",
    "CallInterrupted",
    "CallRefused",
    "FlowCheckpointLimit",
    "FlowLimitExceeded",
    "FlowOutcome",
    "FlowReplayMismatch",
    "FlowStall",
    "FlowTargetUnknown",
    "GatherFailed",
    "TaskStateUncertain",
]
