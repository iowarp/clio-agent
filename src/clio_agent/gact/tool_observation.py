"""Trusted local tool metadata; backend JSON cannot acknowledge recipient input."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PresentedToolResult:
    """SDK-decoded value and presentation to restore in the calling context."""

    value: Any
    structured_content: dict[str, Any]


class CollectedTaskResult(dict[str, Any]):
    """Trusted disclosure metadata; completion collection is optional and explicit."""

    def __init__(
        self,
        value: dict[str, Any],
        notification_ids: tuple[str, ...],
        read_handles: tuple[str, ...] = (),
        *,
        observe_handles: tuple[str, ...] = (),
        collect: bool = True,
    ) -> None:
        super().__init__(value)
        self.notification_ids = notification_ids
        self.read_handles = read_handles
        self.observe_handles = observe_handles
        self.collect = collect


class CollectedText(str):
    """The text the model reads of a reply that already carried task outcomes.

    A foreground shell call returns its finished task inline, but it reaches the model
    as MCP text, not as a :class:`CollectedTaskResult`. Carrying the same metadata on
    the text lets the step collect those outcomes' notices, so the result is not
    delivered a second time as a ``task_result`` input (it was, for 427 of 453 shell
    calls in the stored traces, 2026-11-03).
    """

    notification_ids: tuple[str, ...]
    read_handles: tuple[str, ...]
    observe_handles: tuple[str, ...]
    collect: bool

    def __new__(cls, text: str, carried: list[CollectedTaskResult]) -> CollectedText:
        made = super().__new__(cls, text)
        made.notification_ids = tuple(dict.fromkeys(i for c in carried for i in c.notification_ids))
        made.read_handles = tuple(dict.fromkeys(h for c in carried for h in c.read_handles))
        made.observe_handles = tuple(dict.fromkeys(h for c in carried for h in c.observe_handles))
        made.collect = all(c.collect for c in carried)
        return made


#: Observation values whose task metadata the step records and collects.
COLLECTED = (CollectedTaskResult, CollectedText)

#: One model tool call's slot for the task outcomes its reply carried inline
#: (``ClioReAct._call`` opens it; ``None`` outside a model call and in nested script calls).
INLINE_COLLECTION: ContextVar[list[CollectedTaskResult] | None] = ContextVar(
    "clio_inline_collection", default=None
)


def note_inline_collection(value: Any) -> None:
    """Record that the running model call's reply carries ``value``'s task outcomes."""
    slot = INLINE_COLLECTION.get()
    if slot is not None and isinstance(value, CollectedTaskResult):
        slot.append(value)


def collected_observation(value: Any, carried: list[CollectedTaskResult]) -> Any:
    """``value`` marked with the outcomes its reply carried, when it is plain text."""
    if not carried or isinstance(value, COLLECTED) or not isinstance(value, str):
        return value
    return CollectedText(value, carried)
